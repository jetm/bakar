"""Refresh output: overlay stanzas, serve options and the opt-in avocado run."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from bakar.commands import _feed_refresh_output as out
from bakar.diagnostics import CheckResult, Severity, Status
from bakar.steps.avocado_cli import AvocadoRunResult

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _result(tmp_path: Path, repos: tuple[str, ...] = ("target/qemux86-64", "sdk/qemux86-64")) -> SimpleNamespace:
    channel_dir = tmp_path / "delta" / "2026" / "edge"
    for root in repos:
        rpm = channel_dir / root / "Packages" / "htop-3.5.0-r0.1.core2_64.rpm"
        rpm.parent.mkdir(parents=True)
        rpm.write_bytes(b"rpm")
    return SimpleNamespace(channel_dir=channel_dir, sync={"repos": list(repos)}, recipes=("htop",))


def _req(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(delta_root=tmp_path / "delta", release="2026", channel="edge")


def _opts(tmp_path: Path, **kw: object) -> out.OutputOptions:
    (tmp_path / "proj").mkdir(exist_ok=True)
    return out.OutputOptions(project_dir=tmp_path / "proj", **kw)  # type: ignore[arg-type]


class Spy:
    def __init__(self, *, pid: int | None = 4242, available: bool = True) -> None:
        self.pid = pid
        self.available = available
        self.serve_calls: list[tuple[Path, int, str]] = []
        self.port_calls: list[tuple[str, int]] = []

    def start_serving(self, root: Path, *, port: int, bind: str) -> int | None:
        self.serve_calls.append((root, port, bind))
        return self.pid

    def port_available(self, bind: str, port: int) -> bool:
        self.port_calls.append((bind, port))
        return self.available


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Spy:
    s = Spy()
    monkeypatch.setattr(out, "start_serving", s.start_serving)
    monkeypatch.setattr(out, "port_available", s.port_available)
    return s


def test_defaults_are_loopback_8080(tmp_path: Path) -> None:
    opts = _opts(tmp_path)
    assert (opts.serve, opts.bind, opts.port, opts.advertise, opts.snapshot, opts.run_avocado) == (
        False,
        "127.0.0.1",
        8080,
        None,
        False,
        False,
    )


def test_stanza_on_stdout_commands_and_notes_on_stderr(tmp_path: Path, spy: Spy, capsys: pytest.CaptureFixture) -> None:
    rc = out.present_refresh(_req(tmp_path), _result(tmp_path), _opts(tmp_path))  # type: ignore[arg-type]
    cap = capsys.readouterr()
    assert rc == 0
    assert cap.out.startswith("repos:\n")
    assert "  bakar-delta-target-qemux86-64:\n    path: ../delta/2026/edge/target/qemux86-64\n" in cap.out
    assert "  bakar-delta-sdk-qemux86-64:\n    path: ../delta/2026/edge/sdk/qemux86-64\n" in cap.out
    assert "stages: [sdk]" in cap.out
    assert cap.out.rstrip().endswith("avocado unlock\navocado install\navocado build")
    assert "url:" not in cap.out
    assert cap.err.strip()  # human text exists and is not on stdout
    assert "refreshed" in cap.err
    assert "refreshed" not in cap.out
    assert spy.serve_calls == []


def test_empty_repository_is_not_listed(tmp_path: Path, spy: Spy, capsys: pytest.CaptureFixture) -> None:
    res = _result(tmp_path, repos=("target/qemux86-64",))
    (tmp_path / "delta" / "2026" / "edge" / "sdk" / "qemux86-64").mkdir(parents=True)
    res.sync["repos"].append("sdk/qemux86-64")
    out.present_refresh(_req(tmp_path), res, _opts(tmp_path))  # type: ignore[arg-type]
    cap = capsys.readouterr()
    assert "bakar-delta-sdk-qemux86-64" not in cap.out
    assert "bakar-delta-target-qemux86-64" in cap.out


def test_no_serve_option_starts_no_server(tmp_path: Path, spy: Spy) -> None:
    out.present_refresh(_req(tmp_path), _result(tmp_path), _opts(tmp_path))  # type: ignore[arg-type]
    assert spy.serve_calls == []


def test_serve_binds_requested_address(tmp_path: Path, spy: Spy, capsys: pytest.CaptureFixture) -> None:
    opts = _opts(tmp_path, serve=True, bind="127.0.0.2", port=9191)
    rc = out.present_refresh(_req(tmp_path), _result(tmp_path), opts)  # type: ignore[arg-type]
    assert rc == 0
    assert spy.serve_calls == [(tmp_path / "delta", 9191, "127.0.0.2")]
    cap = capsys.readouterr()
    assert "url: http://127.0.0.2:9191/2026/edge/target/qemux86-64" in cap.out


def test_serve_defaults_bind_loopback_8080(tmp_path: Path, spy: Spy) -> None:
    out.present_refresh(_req(tmp_path), _result(tmp_path), _opts(tmp_path, serve=True))  # type: ignore[arg-type]
    assert spy.serve_calls == [(tmp_path / "delta", 8080, "127.0.0.1")]


def test_advertise_overrides_url_host_not_bind(tmp_path: Path, spy: Spy, capsys: pytest.CaptureFixture) -> None:
    opts = _opts(tmp_path, serve=True, advertise="192.0.2.7")
    out.present_refresh(_req(tmp_path), _result(tmp_path), opts)  # type: ignore[arg-type]
    cap = capsys.readouterr()
    assert spy.serve_calls[0][2] == "127.0.0.1"
    assert "url: http://192.0.2.7:8080/2026/edge/target/qemux86-64" in cap.out
    assert "127.0.0.1:8080" not in cap.out
    assert "path: ../delta/2026/edge/target/qemux86-64" in cap.out  # directory stanza still printed


def test_serve_failure_exits_nonzero_and_advertises_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    s = Spy(pid=None)
    monkeypatch.setattr(out, "start_serving", s.start_serving)
    rc = out.present_refresh(_req(tmp_path), _result(tmp_path), _opts(tmp_path, serve=True))  # type: ignore[arg-type]
    cap = capsys.readouterr()
    assert rc == 1
    assert "url:" not in cap.out
    assert "8080" in cap.err


def test_check_port_refuses_taken_port_only_when_serving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Spy(available=False)
    monkeypatch.setattr(out, "port_available", s.port_available)
    assert out.check_port(_opts(tmp_path)) is None
    assert s.port_calls == []
    msg = out.check_port(_opts(tmp_path, serve=True, bind="127.0.0.3", port=9000))
    assert msg is not None
    assert "127.0.0.3:9000" in msg
    assert s.port_calls == [("127.0.0.3", 9000)]


def test_ipv6_advertise_is_bracketed(tmp_path: Path, spy: Spy, capsys: pytest.CaptureFixture) -> None:
    opts = _opts(tmp_path, serve=True, bind="::1")
    out.present_refresh(_req(tmp_path), _result(tmp_path), opts)  # type: ignore[arg-type]
    assert "url: http://[::1]:8080/2026/edge/target/qemux86-64" in capsys.readouterr().out


def test_ipv6_advertise_literal_is_bracketed(tmp_path: Path, spy: Spy, capsys: pytest.CaptureFixture) -> None:
    opts = _opts(tmp_path, serve=True, advertise="2001:db8::7")
    out.present_refresh(_req(tmp_path), _result(tmp_path), opts)  # type: ignore[arg-type]
    assert "url: http://[2001:db8::7]:8080/2026/edge/target/qemux86-64" in capsys.readouterr().out


def test_hostname_advertise_is_not_bracketed(tmp_path: Path, spy: Spy, capsys: pytest.CaptureFixture) -> None:
    opts = _opts(tmp_path, serve=True, advertise="builder.example")
    out.present_refresh(_req(tmp_path), _result(tmp_path), opts)  # type: ignore[arg-type]
    assert "url: http://builder.example:8080/2026/edge/target/qemux86-64" in capsys.readouterr().out


def test_host_port_advertise_is_rejected_before_serving(
    tmp_path: Path, spy: Spy, capsys: pytest.CaptureFixture
) -> None:
    opts = _opts(tmp_path, serve=True, advertise="myhost:9000")
    rc = out.present_refresh(_req(tmp_path), _result(tmp_path), opts)  # type: ignore[arg-type]
    cap = capsys.readouterr()
    assert rc == 1
    assert spy.serve_calls == []
    assert "--advertise takes a host only" in cap.err
    assert "[myhost:9000]" not in cap.out


def test_run_avocado_off_runs_nothing(tmp_path: Path, spy: Spy, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(out, "run_avocado_commands", lambda p: calls.append(p) or AvocadoRunResult(ok=True))
    out.present_refresh(_req(tmp_path), _result(tmp_path), _opts(tmp_path))  # type: ignore[arg-type]
    assert calls == []


def test_run_avocado_runs_in_project_dir(tmp_path: Path, spy: Spy, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(out, "run_avocado_commands", lambda p: calls.append(p) or AvocadoRunResult(ok=True))
    opts = _opts(tmp_path, run_avocado=True)
    assert out.present_refresh(_req(tmp_path), _result(tmp_path), opts) == 0  # type: ignore[arg-type]
    assert calls == [opts.project_dir]


def test_run_avocado_failure_names_command(
    tmp_path: Path, spy: Spy, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(
        out, "run_avocado_commands", lambda p: AvocadoRunResult(ok=False, failed_command="install", returncode=3)
    )
    rc = out.present_refresh(_req(tmp_path), _result(tmp_path), _opts(tmp_path, run_avocado=True))  # type: ignore[arg-type]
    assert rc == 1
    assert "avocado install" in capsys.readouterr().err


def test_run_avocado_timeout_is_reported(
    tmp_path: Path, spy: Spy, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(
        out,
        "run_avocado_commands",
        lambda p: AvocadoRunResult(ok=False, failed_command="build", returncode=-15, timed_out=True),
    )
    rc = out.present_refresh(_req(tmp_path), _result(tmp_path), _opts(tmp_path, run_avocado=True))  # type: ignore[arg-type]
    assert rc == 1
    assert "timed out" in capsys.readouterr().err


def test_serve_failure_skips_avocado_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Spy(pid=None)
    monkeypatch.setattr(out, "start_serving", s.start_serving)
    calls: list[Path] = []
    monkeypatch.setattr(out, "run_avocado_commands", lambda p: calls.append(p) or AvocadoRunResult(ok=True))
    opts = _opts(tmp_path, serve=True, run_avocado=True)
    assert out.present_refresh(_req(tmp_path), _result(tmp_path), opts) == 1  # type: ignore[arg-type]
    assert calls == []


def test_apply_snapshot_sets_request_field(tmp_path: Path) -> None:
    import dataclasses

    @dataclasses.dataclass(frozen=True)
    class Req:
        snapshot: bool = False

    assert out.apply_snapshot(Req(), _opts(tmp_path, snapshot=True)).snapshot is True  # type: ignore[arg-type]
    assert out.apply_snapshot(Req(), _opts(tmp_path)).snapshot is False  # type: ignore[arg-type]


def _check(status: Status, name: str = "scripts") -> CheckResult:
    return CheckResult(name=name, severity=Severity.BLOCK, status=status, message=f"{name} broken", fix_hint="fix it")


def test_preflight_runs_without_deploy_dir_and_reports_blockers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    seen: dict[str, object] = {}

    def fake(**kw: object) -> list[CheckResult]:
        seen.update(kw)
        return [_check(Status.FAIL)]

    monkeypatch.setattr(out._refresh, "preflight_results", fake)
    blocked = out.preflight_blocks(scripts=tmp_path, roots=None, release="2026", channel="edge")
    assert blocked is True
    assert seen["deploy_dir"] is None
    err = capsys.readouterr().err
    assert "scripts broken" in err
    assert "fix it" in err


def test_preflight_clean_does_not_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(out._refresh, "preflight_results", lambda **kw: [_check(Status.PASS)])
    assert out.preflight_blocks(scripts=tmp_path, roots=None, release="2026", channel="edge") is False
