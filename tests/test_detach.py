"""Tests for the detached-build launcher (fake runner and clock; no real unit)."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bakar import build_scope, detach, detach_records


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    rt = tmp_path / "run"
    rt.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(rt))
    monkeypatch.setattr(detach.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(build_scope, "systemd_run_available", lambda: True)
    return rt


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, secs):
        self.now += secs


def _request(tmp_path, argv=("build", "k.yml"), environ=None):
    return detach.LaunchRequest(argv=list(argv), cwd=tmp_path, environ=environ or {})


def _systemd_unescape(payload):
    """Simulate systemd collapsing ``$$`` before the shell sees the payload."""
    return payload.replace("$$", "$")


def _ok_runner(calls):
    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return runner


def test_refuses_when_systemd_run_unavailable(runtime, monkeypatch, tmp_path):
    monkeypatch.setattr(build_scope, "systemd_run_available", lambda: False)
    calls: list = []
    with pytest.raises(detach.DetachUnavailable, match="user manager unreachable"):
        detach.launch(_request(tmp_path), runner=_ok_runner(calls))
    assert calls == []


def test_refuses_without_binary(runtime, monkeypatch, tmp_path):
    monkeypatch.setattr(detach.shutil, "which", lambda name: None)
    with pytest.raises(detach.DetachUnavailable, match="systemd-run not found"):
        detach.launch(_request(tmp_path), runner=_ok_runner([]))


def test_refuses_without_runtime_dir(runtime, monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    with pytest.raises(detach.DetachUnavailable, match="XDG_RUNTIME_DIR unset"):
        detach.launch(_request(tmp_path), runner=_ok_runner([]))


def test_strip_detach_drops_every_occurrence():
    assert detach.strip_detach(["build", "--detach", "k.yml", "--detach"]) == ["build", "k.yml"]


def test_forwarded_env_allowlist():
    env = {
        "SSH_AUTH_SOCK": "/s",
        "DISPLAY": ":0",
        "BAKAR_X": "1",
        "KAS_Y": "2",
        "BAKAR_DETACH_UNIT": "bakar-detach-20260101-000000-abcdef",
        "PATH": "/bin",
        "XDG_RUNTIME_DIR": "/r",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/b",
        "HOME": "/home/x",
    }
    out = detach.forwarded_env(env)
    assert out == {
        "SSH_AUTH_SOCK": "/s",
        "BAKAR_X": "1",
        "KAS_Y": "2",
        "PATH": "/bin",
        "XDG_RUNTIME_DIR": "/r",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/b",
    }


def test_build_unit_argv_uses_cwd_and_quotes_log(tmp_path):
    nasty = tmp_path / "a dir $(touch /tmp/pwned)"
    paths = detach_records.UnitPaths(log=nasty / "u.log", sentinel=nasty / "u.log.rc", registry=nasty / "u.runs")
    unit = "bakar-detach-20260101-000000-abcdef"
    req = _request(tmp_path / "cwd", argv=("build", "it's here.yml"), environ={"SSH_AUTH_SOCK": "/s", "DISPLAY": ":0"})
    argv = detach.build_unit_argv(unit, req, paths)
    assert argv[:7] == [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        "--collect",
        "--quiet",
        "--property=Type=exec",
        f"--working-directory={tmp_path / 'cwd'}",
    ]
    assert "--setenv=SSH_AUTH_SOCK=/s" in argv
    assert f"--setenv=BAKAR_DETACH_UNIT={unit}" in argv
    assert not any("DISPLAY" in a for a in argv)
    assert argv[-3:-1] == ["bash", "-c"]
    script = _systemd_unescape(argv[-1])
    subprocess.run(["bash", "-n", "-c", script], check=True)
    tokens = shlex.split(script)
    assert ">" + str(nasty / "u.log") in tokens
    assert "it's here.yml" in tokens
    # sentinel is written after the log redirection
    assert script.index("2>&1") < script.index(".log.rc")


def test_launch_running_after_registration(runtime, tmp_path):
    calls: list = []
    clock = FakeClock()

    def runner(argv, **kwargs):
        calls.append(argv)
        unit = next(a for a in argv if a.startswith("--unit=")).split("=", 1)[1]
        detach_records.append_registration(unit, "20260101-000000-1", tmp_path, 1)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    h = detach.launch(_request(tmp_path), runner=runner, clock=clock, sleep=clock.sleep)
    assert h.state == "running"
    assert h.run_id == "20260101-000000-1"
    assert h.host is None
    assert set(h.to_json()) == {"run_id", "unit", "host", "log", "state"}
    assert calls[0][0] == "systemd-run"


def test_launch_failed_when_sentinel_precedes_registration(runtime, tmp_path):
    clock = FakeClock()

    def runner(argv, **kwargs):
        unit = next(a for a in argv if a.startswith("--unit=")).split("=", 1)[1]
        detach_records.unit_paths(unit).sentinel.write_text("2\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    h = detach.launch(_request(tmp_path), runner=runner, clock=clock, sleep=clock.sleep)
    assert (h.state, h.exit_code, h.run_id) == ("failed", 2, None)


def test_launch_succeeded_sentinel_zero(runtime, tmp_path):
    clock = FakeClock()

    def runner(argv, **kwargs):
        unit = next(a for a in argv if a.startswith("--unit=")).split("=", 1)[1]
        detach_records.unit_paths(unit).sentinel.write_text("0\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    h = detach.launch(_request(tmp_path), runner=runner, clock=clock, sleep=clock.sleep)
    assert (h.state, h.exit_code) == ("succeeded", 0)


def test_launch_starting_when_bound_expires(runtime, tmp_path):
    clock = FakeClock()
    h = detach.launch(_request(tmp_path), runner=_ok_runner([]), clock=clock, sleep=clock.sleep, bound=3.0)
    assert (h.state, h.run_id, h.exit_code) == ("starting", None, None)
    assert clock.now >= 3.0


def test_launch_nonzero_systemd_run_is_unavailable(runtime, tmp_path):
    def runner(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    with pytest.raises(detach.DetachUnavailable, match="boom"):
        detach.launch(_request(tmp_path), runner=runner)


def test_launch_prunes_first(runtime, tmp_path, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(detach_records, "prune", lambda *a, **k: order.append("prune") or [])
    monkeypatch.setattr(detach, "check_available", lambda: order.append("check"))

    def runner(argv, **kwargs):
        order.append("run")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    clock = FakeClock()
    detach.launch(_request(tmp_path), runner=runner, clock=clock, sleep=clock.sleep, bound=0.0)
    assert order == ["prune", "check", "run"]


def test_launch_prunes_old_finished_record(runtime, tmp_path):
    import os
    import time

    old = "bakar-detach-20200101-000000-aaaaaa"
    paths = detach_records.unit_paths(old)
    paths.log.write_text("x")
    paths.sentinel.write_text("0\n")
    stale = time.time() - 8 * 86400
    os.utime(paths.sentinel, (stale, stale))
    clock = FakeClock()
    detach.launch(_request(tmp_path), runner=_ok_runner([]), clock=clock, sleep=clock.sleep, bound=0.0)
    assert not Path(paths.log).exists()


def test_build_unit_argv_runs_python_without_cwd_on_path(tmp_path):
    paths = detach_records.UnitPaths(
        log=tmp_path / "u.log", sentinel=tmp_path / "u.log.rc", registry=tmp_path / "u.runs"
    )
    argv = detach.build_unit_argv("bakar-detach-20260101-000000-abcdef", _request(tmp_path), paths)
    tokens = shlex.split(_systemd_unescape(argv[-1]))
    assert tokens[0] == sys.executable
    assert tokens[1:4] == ["-P", "-m", "bakar"]
    assert tokens.index("-P") < tokens.index("-m")


def test_launch_sentinel_and_registration_use_registered_run_id(runtime, tmp_path):
    clock = FakeClock()

    def runner(argv, **kwargs):
        unit = next(a for a in argv if a.startswith("--unit=")).split("=", 1)[1]
        detach_records.append_registration(unit, "20260101-000000-9", tmp_path, 1)
        detach_records.unit_paths(unit).sentinel.write_text("4\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    h = detach.launch(_request(tmp_path), runner=runner, clock=clock, sleep=clock.sleep)
    assert (h.state, h.exit_code, h.run_id) == ("failed", 4, "20260101-000000-9")


@pytest.mark.parametrize("exc", [OSError("no exec"), subprocess.TimeoutExpired(["systemd-run"], 30)])
def test_launch_runner_failure_is_unavailable(runtime, tmp_path, exc):
    def runner(argv, **kwargs):
        raise exc

    with pytest.raises(detach.DetachUnavailable, match="failed to start the unit"):
        detach.launch(_request(tmp_path), runner=runner)


def test_build_unit_argv_escapes_dollars_for_systemd(tmp_path):
    paths = detach_records.UnitPaths(
        log=tmp_path / "u.log", sentinel=tmp_path / "u.log.rc", registry=tmp_path / "u.runs"
    )
    tokens_in = ["build", "${X}$$", "k.yml"]
    argv = detach.build_unit_argv("bakar-detach-20260101-000000-abcdef", _request(tmp_path, argv=tokens_in), paths)
    payload = argv[-1]
    # systemd rewrites `${VAR}` and `$$` unless every `$` is doubled.
    assert "$${X}$$$$" in payload
    assert "$$rc" in payload
    unescaped = _systemd_unescape(payload)
    subprocess.run(["bash", "-n", "-c", unescaped], check=True)
    front_end = unescaped.split(" >", 1)[0]
    assert shlex.split(front_end) == [sys.executable, "-P", "-m", "bakar", *tokens_in]


def test_build_unit_argv_requests_type_exec(tmp_path):
    paths = detach_records.UnitPaths(
        log=tmp_path / "u.log", sentinel=tmp_path / "u.log.rc", registry=tmp_path / "u.runs"
    )
    argv = detach.build_unit_argv("bakar-detach-20260101-000000-abcdef", _request(tmp_path), paths)
    assert "--property=Type=exec" in argv
    assert argv.index("--property=Type=exec") < argv.index("--")


def test_launch_systemd_run_failure_carries_its_stderr(runtime, tmp_path):
    def runner(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="Failed to chdir to /nonexistent\n")

    with pytest.raises(detach.DetachUnavailable, match="Failed to chdir to /nonexistent"):
        detach.launch(_request(tmp_path), runner=runner)


@pytest.mark.parametrize("registrations", [[{}], [{"run_id": 123}], [{"run_id": ""}], [{"run_id": None}]])
def test_launch_ignores_registrations_without_a_string_run_id(runtime, tmp_path, monkeypatch, registrations):
    monkeypatch.setattr(detach_records, "read_registrations", lambda unit: registrations)
    clock = FakeClock()
    h = detach.launch(_request(tmp_path), runner=_ok_runner([]), clock=clock, sleep=clock.sleep, bound=2.0)
    assert (h.state, h.run_id) == ("starting", None)


def test_launch_takes_the_first_valid_run_id(runtime, tmp_path, monkeypatch):
    regs = [{}, {"run_id": 5}, {"run_id": "20260101-000000-7"}, {"run_id": "later"}]
    monkeypatch.setattr(detach_records, "read_registrations", lambda unit: regs)
    h = detach.launch(_request(tmp_path), runner=_ok_runner([]), clock=FakeClock(), sleep=lambda s: None)
    assert (h.state, h.run_id) == ("running", "20260101-000000-7")
