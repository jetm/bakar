"""Tests for ``bakar.steps.avocado_dispatch`` with every ssh, git and subprocess call mocked."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from bakar.release_workspace import sanitize_delivered_name
from bakar.steps import avocado_dispatch as ad
from bakar.steps import layer_delivery, remote_dispatch

pytestmark = pytest.mark.unit

HOST = "pc2"
COMMIT = "a" * 40
WORKSPACE = Path("/srv/ws/scarthgap")


class Recorder:
    """Install mocks for every external call and record them in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.preflight: tuple[bool, str | None] = (True, "bakar 1.0")
        self.local_version: str | None = "bakar 1.0"
        self.workspace_error: str | None = None
        self.snapshot = False
        self.push_rc = 0
        self.ensure_rc = 0
        self.stream: tuple[int, list[str], bool] | type[BaseException] = (0, ["ok\n"], True)
        self.ensure_script = ""
        self.remote_script = ""

        def preflight(host: str) -> tuple[bool, str | None]:
            self.calls.append(("preflight", host))
            return self.preflight

        def local_version() -> str | None:
            self.calls.append(("local_version", None))
            return self.local_version

        def check_ws(host: str, workspace: str, release: str) -> None:
            self.calls.append(("check_workspace", (host, workspace, release)))
            if self.workspace_error:
                raise remote_dispatch.DispatchError(self.workspace_error)

        def name(checkout: Path, worktree: Any) -> str:
            self.calls.append(("delivered_name", (checkout, worktree)))
            return "feature-x"

        def snapshot(checkout: Path, *, cap_mb: int) -> tuple[str, bool]:
            self.calls.append(("snapshot", (checkout, cap_mb)))
            return COMMIT, self.snapshot

        def run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append(("push", argv))
            return subprocess.CompletedProcess(argv, self.push_rc)

        def capture(host: str, script: str, **_kw: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append(("ensure", host))
            self.ensure_script = script
            return subprocess.CompletedProcess(["ssh"], self.ensure_rc, "", "boom")

        def stream(host: str, script: str) -> tuple[int, list[str], bool]:
            self.calls.append(("stream", host))
            self.remote_script = script
            if isinstance(self.stream, type):
                raise self.stream()
            return self.stream

        def surface(host: str, ws: Path, captured: list[str], rc: int) -> None:
            self.calls.append(("surface", (host, ws, rc)))

        def no_prompt(*_a: Any, **_kw: Any) -> bool:
            raise AssertionError("dispatch must not prompt")

        monkeypatch.setattr(remote_dispatch, "preflight_remote", preflight)
        monkeypatch.setattr(remote_dispatch, "_local_bakar_version", local_version)
        monkeypatch.setattr(remote_dispatch, "check_remote_workspace", check_ws)
        monkeypatch.setattr(layer_delivery, "delivered_name", name)
        monkeypatch.setattr(layer_delivery, "snapshot_commit", snapshot)
        monkeypatch.setattr(ad.subprocess, "run", run)
        monkeypatch.setattr(remote_dispatch, "_ssh_bash_capture", capture)
        monkeypatch.setattr(remote_dispatch, "_stream_remote_build", stream)
        monkeypatch.setattr(remote_dispatch, "_surface_run_id", surface)
        monkeypatch.setattr(ad.socket, "gethostname", lambda: "pc1")
        monkeypatch.setattr("typer.confirm", no_prompt)
        monkeypatch.setattr("builtins.input", no_prompt)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    return Recorder(monkeypatch)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    co = tmp_path / "wt" / "feature-x"
    (co / "kas" / "machine").mkdir(parents=True)
    (co / "kas" / "machine" / "m.yml").write_text("header: {version: 14}\n")
    (co / "kas" / "extra.yml").write_text("header: {version: 14}\n")
    return co


def _dispatch(checkout: Path, kas_arg: str, local_args: list[str], **overrides: Any) -> int:
    kwargs: dict[str, Any] = {
        "workspace": WORKSPACE,
        "checkout": checkout,
        "worktree": None,
        "kas_yaml": checkout / "kas" / "machine" / "m.yml",
        "kas_arg": kas_arg,
        "local_args": local_args,
        "release": "scarthgap",
        "sccache_dist": False,
        "assume_yes": False,
        "worktree_root": None,
        "build_dir_template": None,
        "cap_mb": 64,
        "invoking_cwd": checkout,
    }
    kwargs.update(overrides)
    return ad.dispatch_avocado_build(HOST, **kwargs)


def _remote_argv_line(script: str) -> str:
    return next(line for line in script.splitlines() if " bakar " in line and line.lstrip().startswith("exec flock"))


def test_call_order_and_success(rec: Recorder, checkout: Path) -> None:
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml", "--on", HOST])
    assert rc == 0
    assert rec.names() == [
        "preflight",
        "local_version",
        "check_workspace",
        "delivered_name",
        "snapshot",
        "push",
        "ensure",
        "stream",
        "surface",
    ]
    assert ("check_workspace", (HOST, str(WORKSPACE), "scarthgap")) in rec.calls
    assert ("surface", (HOST, WORKSPACE / "build-m-feature-x", 0)) in rec.calls


@pytest.mark.parametrize(
    "failure",
    ["preflight", "version", "workspace"],
)
def test_no_remote_write_when_a_check_fails(rec: Recorder, checkout: Path, failure: str) -> None:
    if failure == "preflight":
        rec.preflight = (False, "unreachable")
    elif failure == "version":
        rec.preflight = (True, "bakar 2.0")
    else:
        rec.workspace_error = "release mismatch"
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml", "--on", HOST])
    assert rc == 1
    assert "push" not in rec.names()
    assert "ensure" not in rec.names()
    assert "stream" not in rec.names()


def test_version_mismatch_proceeds_with_yes(rec: Recorder, checkout: Path) -> None:
    rec.preflight = (True, "bakar 2.0")
    rc = _dispatch(
        checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml", "--on", HOST, "--yes"], assume_yes=True
    )
    assert rc == 0
    assert "push" in rec.names()


def test_push_targets_remote_clone_and_ref(rec: Recorder, checkout: Path) -> None:
    _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    push = next(argv for name, argv in rec.calls if name == "push")
    assert push[:3] == ["git", "-C", str(checkout)]
    assert f"ssh://{HOST}{WORKSPACE}/meta-avocado" in push
    assert push[-1] == f"{COMMIT}:refs/bakar/feature-x/{COMMIT[:12]}"


def test_push_failure_stops_before_ensure(rec: Recorder, checkout: Path) -> None:
    rec.push_rc = 128
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    assert rc == 128
    assert "ensure" not in rec.names()
    assert "stream" not in rec.names()


@pytest.mark.parametrize("snapshot", [False, True])
def test_marker_inputs(rec: Recorder, checkout: Path, snapshot: bool) -> None:
    rec.snapshot = snapshot
    _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    script = rec.ensure_script
    assert "pc1" in script
    assert str(checkout) in script
    assert f'"snapshot": {"true" if snapshot else "false"}' in script
    assert f"{WORKSPACE}/.bakar/worktrees" in script


def test_ensure_failure_reports_reason(rec: Recorder, checkout: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rec.ensure_rc = 5
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    assert rc == 5
    assert "stream" not in rec.names()
    err = capsys.readouterr().err
    assert "another" in err.lower() or "marker" in err.lower()


def test_remote_argv_cwd_and_env(rec: Recorder, checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAS_WORK_DIR", "/local/ws")
    monkeypatch.setenv("BAKAR_FOO", "bar")
    local = [
        "build",
        str(checkout / "kas" / "machine" / "m.yml"),
        "-w",
        "/local/ws",
        "--workspace=/x",
        "--on",
        HOST,
        "-y",
    ]
    rc = _dispatch(
        checkout,
        str(checkout / "kas" / "machine" / "m.yml"),
        local,
        worktree_root=Path("/srv/wt"),
        build_dir_template="b-{worktree}-{stem}",
    )
    assert rc == 0
    script = rec.remote_script
    assert "cd /srv/wt/feature-x" in script
    assert "BAKAR_REPO_POLICY=reuse" in script
    assert "BAKAR_WORKTREE_BUILD_DIR=b-{worktree}-{stem}" in script
    assert "BAKAR_FOO=bar" in script
    assert "KAS_WORK_DIR" not in script
    assert "BAKAR_SCCACHE_DIST=0" in script
    line = _remote_argv_line(script)
    assert line.endswith("bakar build kas/machine/m.yml")
    assert str(checkout) not in line
    assert "-w" not in line.split()
    assert "--on" not in line


def test_colon_joined_relative_kas_arg(rec: Recorder, checkout: Path) -> None:
    cwd = checkout / "kas"
    kas_arg = "machine/m.yml:extra.yml"
    rc = _dispatch(checkout, kas_arg, ["build", kas_arg], invoking_cwd=cwd)
    assert rc == 0
    assert _remote_argv_line(rec.remote_script).endswith("bakar build kas/machine/m.yml:kas/extra.yml")


def test_kas_segment_outside_checkout_exits_2(rec: Recorder, checkout: Path, tmp_path: Path) -> None:
    outside = tmp_path / "other.yml"
    outside.write_text("header: {version: 14}\n")
    kas_arg = f"kas/machine/m.yml:{outside}"
    rc = _dispatch(checkout, kas_arg, ["build", kas_arg])
    assert rc == 2
    assert rec.calls == []


def test_plan_printed_without_prompt(rec: Recorder, checkout: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rec.snapshot = True
    _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    err = capsys.readouterr().err
    assert "dispatch plan" in err
    assert f"{WORKSPACE}/.bakar/worktrees/feature-x" in err
    assert f"refs/bakar/feature-x/{COMMIT[:12]}" in err
    assert f"{WORKSPACE}/build-m-feature-x" in err
    assert "snapshot of uncommitted changes: yes" in err
    assert "nothing is deleted" in err


def test_interrupted_stream_returns_130(rec: Recorder, checkout: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rec.stream = KeyboardInterrupt
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    assert rc == 130
    err = capsys.readouterr().err
    assert f"bakar stop --on {HOST}" in err
    assert "bakar triage" in err
    assert "surface" not in rec.names()


def test_lost_stream_skips_run_id(rec: Recorder, checkout: Path) -> None:
    rec.stream = (255, [], False)
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    assert rc == remote_dispatch._DISPATCH_LOST_EXIT
    assert "surface" not in rec.names()


def test_remote_failure_rc_propagates(rec: Recorder, checkout: Path) -> None:
    rec.stream = (1, ["Run `bakar triage 20260101-000000` for details.\n"], True)
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    assert rc == 1
    assert ("surface", (HOST, WORKSPACE / "build-m-feature-x", 1)) in rec.calls


def test_unusable_name_exits_2_before_any_remote_write(
    rec: Recorder, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unusable(_checkout: Path, _worktree: Any) -> str:
        return sanitize_delivered_name("...")

    monkeypatch.setattr(layer_delivery, "delivered_name", unusable)
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml", "--on", HOST])
    assert rc == 2
    assert "no usable characters" in capsys.readouterr().err
    assert not {"snapshot", "push", "ensure", "stream"} & set(rec.names())


def test_hyphen_host_rejected(rec: Recorder, checkout: Path) -> None:
    rc = ad.dispatch_avocado_build(
        "-oProxyCommand=x",
        workspace=WORKSPACE,
        checkout=checkout,
        worktree=None,
        kas_yaml=checkout / "kas" / "machine" / "m.yml",
        kas_arg="kas/machine/m.yml",
        local_args=["build", "kas/machine/m.yml"],
        release="scarthgap",
        sccache_dist=False,
        assume_yes=False,
        worktree_root=None,
        build_dir_template=None,
        cap_mb=64,
        invoking_cwd=checkout,
    )
    assert rc == 1
    assert rec.calls == []


def test_strip_workspace_option_forms() -> None:
    args = ["build", "-w", "/a", "--workspace", "/b", "--workspace=/c", "-w/d", "k.yml"]
    assert ad.strip_workspace_option(args) == ["build", "k.yml"]


def test_strip_workspace_option_in_short_clusters() -> None:
    assert ad.strip_workspace_option(["build", "-kw", "/a", "k.yml"]) == ["build", "-k", "k.yml"]
    assert ad.strip_workspace_option(["build", "-kw/a", "k.yml"]) == ["build", "-k", "k.yml"]
    assert ad.strip_workspace_option(["build", "-nk", "k.yml"]) == ["build", "-nk", "k.yml"]


def test_remote_env_always_names_the_build_dir_template() -> None:
    assert ad.remote_env(None)["BAKAR_WORKTREE_BUILD_DIR"] == "build-{stem}-{worktree}"
    assert ad.remote_env("b-{stem}")["BAKAR_WORKTREE_BUILD_DIR"] == "b-{stem}"


@pytest.mark.parametrize(
    "forbidden", ["rm ", "worktree remove", "worktree prune", "reset --hard", "clean -f", "--force"]
)
def test_dispatched_scripts_carry_no_destructive_command(rec: Recorder, checkout: Path, forbidden: str) -> None:
    assert _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml", "--on", HOST]) == 0
    assert forbidden not in rec.ensure_script
    assert forbidden not in rec.remote_script
