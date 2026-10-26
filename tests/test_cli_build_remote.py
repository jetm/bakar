"""CLI wiring tests for ``bakar build --on <host>`` remote dispatch.

These exercise the ``build()`` command's early ``--on`` interception with the
ssh/rsync subprocess mocked (``bakar.steps.remote_dispatch.subprocess``) and no
live host. The forwarded argv is ``sys.argv[1:]`` (design D2), so the ``--on``
tests monkeypatch ``sys.argv`` to a realistic invocation.

Asserted:
- no ``--on`` spawns neither the ssh nor the rsync mock (local build path);
- an unreachable host exits non-zero with NO rsync call;
- the default remote script carries ``BAKAR_SCCACHE_DIST=0`` while
  ``--sccache-dist`` omits it;
- a non-zero remote exit propagates to the CLI exit code;
- the run-id + ``ssh <host> bakar triage <id>`` line is printed;
- dispatch never prompts, with or without ``--yes``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bakar.cli import app
from bakar.commands import build as build_cmd
from bakar.steps import remote_dispatch as rd

if TYPE_CHECKING:
    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

HOST = "pc2"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_doctor_checks():
    """Doctor runs on every build; stub ``run_all`` to all-pass so the local
    build path in the no-``--on`` test stays host-independent."""
    from unittest.mock import patch

    with patch("bakar.commands._helpers.run_all", return_value=[]):
        yield


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp workspace with a ``.bakar.toml`` marker; chdir into it so
    ``_workspace_from_cwd()`` resolves to it."""
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def generic_yaml(tmp_path: Path) -> Path:
    """A minimal generic kas YAML (qemu machine, no NXP/TI markers)."""
    yaml_path = tmp_path / "my.yml"
    yaml_path.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    return yaml_path


class _FakeStdin:
    def __init__(self) -> None:
        self.buffer = ""

    def write(self, s: str) -> None:
        self.buffer += s

    def close(self) -> None:
        pass


class _FakeProc:
    def __init__(self, lines: list[str], rc: int) -> None:
        self.stdin = _FakeStdin()
        self.stdout = list(lines)
        self._rc = rc

    def wait(self) -> int:
        return self._rc


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class FakeSubprocess:
    """Records every run/Popen call and dispatches a canned result per argv."""

    PIPE = "PIPE"
    STDOUT = "STDOUT"

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.reachable_rc = 0
        self.remote_version = ""
        self.local_version = ""
        self.rsync_rc = 0
        self.find_stdout = ""
        self.popen_lines: list[str] = []
        self.popen_rc = 0
        self.last_proc: _FakeProc | None = None

    def run(self, argv, **kwargs) -> _Result:
        argv = list(argv)
        self.calls.append(("run", argv))
        # Preflight probe: ssh -o BatchMode=yes <host> bash -s.
        if argv[0] == "ssh" and argv[-1] == "-s":
            return _Result(self.reachable_rc, stdout=self.remote_version)
        if argv[0] == "bakar" and "--version" in argv:
            return _Result(0, stdout=self.local_version)
        if argv[0] == "rsync" and "-n" in argv:
            return _Result(0, stdout="itemized preview line\n")
        if argv[0] == "rsync":
            return _Result(self.rsync_rc)
        if argv[0] == "ssh" and "find" in argv[-1]:
            return _Result(0, stdout=self.find_stdout)
        return _Result(0)

    def Popen(self, argv, **kwargs) -> _FakeProc:  # noqa: N802
        self.calls.append(("Popen", list(argv)))
        self.last_proc = _FakeProc(self.popen_lines, self.popen_rc)
        return self.last_proc


@pytest.fixture
def fake_sp(monkeypatch: pytest.MonkeyPatch) -> FakeSubprocess:
    fake = FakeSubprocess()
    monkeypatch.setattr(rd, "subprocess", fake)
    return fake


@pytest.fixture(autouse=True)
def _clean_bakar_kas_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear ambient BAKAR_*/KAS_* env so the forwarded-env tokens stay deterministic."""
    import os

    for key in list(os.environ):
        if key.startswith(("BAKAR_", "KAS_")):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# (a) No --on: neither ssh nor rsync mock is touched (local build path).
# ---------------------------------------------------------------------------


def test_no_on_option_does_not_dispatch(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_cmd.step_kas, "run_build", lambda ctx, **kw: 0)

    result = runner.invoke(app, ["build", str(generic_yaml)])

    assert result.exit_code == 0, result.output
    # The remote dispatch subprocess was never invoked: no ssh, no rsync.
    assert fake_sp.calls == []


# ---------------------------------------------------------------------------
# (b) Unreachable host: exit non-zero, NO rsync spawned.
# ---------------------------------------------------------------------------


def test_unreachable_host_exits_nonzero_no_rsync(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["bakar", "build", str(generic_yaml), "--on", HOST])
    fake_sp.reachable_rc = 255

    result = runner.invoke(app, ["build", str(generic_yaml), "--on", HOST])

    assert result.exit_code != 0
    assert not any(argv[0] == "rsync" for _, argv in fake_sp.calls)
    assert not any(kind == "Popen" for kind, _ in fake_sp.calls)


# ---------------------------------------------------------------------------
# (c) sccache-dist off by default; --sccache-dist opts back in.
# ---------------------------------------------------------------------------


def test_default_remote_script_carries_sccache_off(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["bakar", "build", str(generic_yaml), "--on", HOST, "--yes"])
    fake_sp.find_stdout = f"1.0 {workspace}/build/runs/20260716-000000\n"

    result = runner.invoke(app, ["build", str(generic_yaml), "--on", HOST, "--yes"])

    assert result.exit_code == 0, result.output
    assert fake_sp.last_proc is not None
    assert "BAKAR_SCCACHE_DIST=0" in fake_sp.last_proc.stdin.buffer


def test_sccache_dist_optin_omits_env_token(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["bakar", "--sccache-dist", "build", str(generic_yaml), "--on", HOST, "--yes"])
    fake_sp.find_stdout = f"1.0 {workspace}/build/runs/20260716-000000\n"

    result = runner.invoke(app, ["--sccache-dist", "build", str(generic_yaml), "--on", HOST, "--yes"])

    assert result.exit_code == 0, result.output
    assert fake_sp.last_proc is not None
    assert "BAKAR_SCCACHE_DIST=0" not in fake_sp.last_proc.stdin.buffer


# ---------------------------------------------------------------------------
# (d) Non-zero remote exit propagates to the CLI exit code.
# ---------------------------------------------------------------------------


def test_remote_exit_code_propagates(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["bakar", "build", str(generic_yaml), "--on", HOST, "--yes"])
    fake_sp.popen_rc = 7
    fake_sp.popen_lines = ["Run `bakar triage 20260716-120000` for details.\n"]

    result = runner.invoke(app, ["build", str(generic_yaml), "--on", HOST, "--yes"])

    assert result.exit_code == 7


# ---------------------------------------------------------------------------
# (e) run-id + triage command line surfaced.
# ---------------------------------------------------------------------------


def test_run_id_and_triage_line_printed(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["bakar", "build", str(generic_yaml), "--on", HOST, "--yes"])
    fake_sp.popen_rc = 1
    fake_sp.popen_lines = ["some output\n", "Run `bakar triage 20260716-120000` for details.\n"]

    result = runner.invoke(app, ["build", str(generic_yaml), "--on", HOST, "--yes"])

    assert "20260716-120000" in result.output
    assert f"ssh {HOST} bakar triage 20260716-120000" in result.output


# ---------------------------------------------------------------------------
# (f) dispatch never prompts, with or without --yes.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("workspace")
@pytest.mark.parametrize("yes", [True, False], ids=["with-yes", "without-yes"])
def test_dispatch_never_prompts(
    runner: _CliRunner,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
    yes: bool,
) -> None:
    import typer

    def _boom(*a, **k):
        raise AssertionError("dispatch must not prompt")

    monkeypatch.setattr(typer, "confirm", _boom)
    args = ["build", str(generic_yaml), "--on", HOST, *(["--yes"] if yes else [])]
    monkeypatch.setattr(sys, "argv", ["bakar", *args])
    fake_sp.find_stdout = f"1.0 {generic_yaml.parent}/build/runs/20260716-000000\n"

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert any(argv[0] == "rsync" for _, argv in fake_sp.calls)


# ---------------------------------------------------------------------------
# (g) C9: --on + --dry-run for a build that is not a meta-avocado kas build is refused.
# ---------------------------------------------------------------------------


def test_dry_run_with_on_refused_no_ssh_rsync(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["bakar", "build", str(generic_yaml), "--on", HOST, "--dry-run"])

    result = runner.invoke(app, ["build", str(generic_yaml), "--on", HOST, "--dry-run"])

    assert result.exit_code == 2
    assert "the remote dry run supports meta-avocado kas builds only" in " ".join(result.output.split())
    # Refused before the destructive mirror: no ssh, no rsync.
    assert fake_sp.calls == []


def test_clean_with_on_refused_no_ssh_rsync(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # --clean would ride to the remote build and delete a build directory there.
    monkeypatch.setattr(sys, "argv", ["bakar", "build", str(generic_yaml), "--on", HOST, "--clean"])

    result = runner.invoke(app, ["build", str(generic_yaml), "--on", HOST, "--clean"])

    assert result.exit_code == 2
    assert "--clean cannot be combined with --on" in result.output
    assert fake_sp.calls == []


# ---------------------------------------------------------------------------
# (h) C2: a generic BYO YAML from a non-workspace cwd resolves ws_root (no exit 2).
# ---------------------------------------------------------------------------


def test_generic_yaml_from_non_workspace_resolves_ws_root(
    runner: _CliRunner,
    tmp_path: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A generic BYO YAML dispatched from a directory that is NOT a workspace must
    # resolve ws_root from the YAML (mirroring the local build), not exit 2 the
    # way the old _workspace_from_cwd() path would from outside a workspace.
    non_ws = tmp_path / "elsewhere"
    non_ws.mkdir()
    monkeypatch.chdir(non_ws)
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    yml = standalone / "qemu.yml"
    yml.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    fake_sp.find_stdout = f"1.0 {standalone}/build/runs/20260716-000000\n"
    monkeypatch.setattr(sys, "argv", ["bakar", "build", str(yml), "--on", HOST, "--yes"])

    result = runner.invoke(app, ["build", str(yml), "--on", HOST, "--yes"])

    assert result.exit_code == 0, result.output
    # A real rsync ran, so ws_root resolved (no exit 2 before dispatch).
    assert any(argv[0] == "rsync" and "-n" not in argv for _, argv in fake_sp.calls)


# ---------------------------------------------------------------------------
# (i) C1: -w chdirs eagerly; the remote script cd's into the ORIGINAL cwd.
# ---------------------------------------------------------------------------


def test_relative_workspace_script_uses_invoking_cwd(
    runner: _CliRunner,
    tmp_path: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = tmp_path / "ws"
    sub = ws / "nxp"
    sub.mkdir(parents=True)
    (ws / ".bakar.toml").write_text("")
    (ws / "my.yml").write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    monkeypatch.chdir(sub)
    fake_sp.find_stdout = f"1.0 {ws}/build/runs/20260716-000000\n"
    monkeypatch.setattr(sys, "argv", ["bakar", "build", "my.yml", "-w", "..", "--on", HOST, "--yes"])

    result = runner.invoke(app, ["build", "my.yml", "-w", "..", "--on", HOST, "--yes"])

    assert result.exit_code == 0, result.output
    assert fake_sp.last_proc is not None
    script = fake_sp.last_proc.stdin.buffer
    # cd into the pre-chdir invoking cwd (nxp/), not the resolved workspace root.
    assert f"cd {sub} ||" in script
    assert f"cd {ws} ||" not in script


# ---------------------------------------------------------------------------
# (j) A meta-avocado build routes to its release workspace; other forms mirror.
# ---------------------------------------------------------------------------


def _record(monkeypatch: pytest.MonkeyPatch, module, name: str) -> list[tuple[tuple, dict]]:
    calls: list[tuple[tuple, dict]] = []

    def fake(*args, **kwargs) -> int:
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(module, name, fake)
    return calls


def _user_config(monkeypatch: pytest.MonkeyPatch, **fields) -> None:
    """Pin the user config the app callback loads, so the real ~/.config never leaks in."""
    from bakar.user_config import UserConfig

    cfg = UserConfig(**fields)
    monkeypatch.setattr("bakar.commands._app.load_user_config", lambda *a, **k: cfg)


def _probe(monkeypatch: pytest.MonkeyPatch, release: str = "wrynose") -> list[Path]:
    from bakar.steps import kas_pins

    probed: list[Path] = []

    def fake(kas_yaml: Path, checkout: Path) -> tuple[str, dict]:
        probed.append(checkout)
        return release, {}

    monkeypatch.setattr(kas_pins, "probe_pins", fake)
    return probed


def _invoke_on(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch, *args: str):
    argv = ["build", *args, "--on", HOST, "--yes"]
    monkeypatch.setattr(sys, "argv", ["bakar", *argv])
    return runner.invoke(app, argv)


def test_meta_avocado_without_workspaces_config_exits_2_before_any_ssh(
    runner: _CliRunner,
    meta_avocado_worktree_layout,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bakar.steps import avocado_dispatch

    lay = meta_avocado_worktree_layout
    _user_config(monkeypatch)
    probed = _probe(monkeypatch)
    avocado = _record(monkeypatch, avocado_dispatch, "dispatch_avocado_build")
    mirror = _record(monkeypatch, rd, "dispatch_remote_build")
    monkeypatch.chdir(lay.worktree)

    result = _invoke_on(runner, monkeypatch, "kas/machine/m.yml")

    assert result.exit_code == 2, result.output
    assert "[workspaces] template" in " ".join(result.output.split())
    assert fake_sp.calls == []
    assert avocado == [] and mirror == [] and probed == []


def test_meta_avocado_unmapped_release_exits_2_before_any_ssh(
    runner: _CliRunner,
    meta_avocado_worktree_layout,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from bakar.steps import avocado_dispatch

    lay = meta_avocado_worktree_layout
    _user_config(monkeypatch, workspaces_releases={"scarthgap": str(tmp_path / "sg")})
    _probe(monkeypatch, "wrynose")
    avocado = _record(monkeypatch, avocado_dispatch, "dispatch_avocado_build")
    monkeypatch.chdir(lay.worktree)

    result = _invoke_on(runner, monkeypatch, "kas/machine/m.yml")

    assert result.exit_code == 2, result.output
    out = " ".join(result.output.split())
    assert "[workspaces] template" in out and "'wrynose'" in out
    assert fake_sp.calls == []
    assert avocado == []


def test_meta_avocado_routing_error_exits_2(
    runner: _CliRunner,
    meta_avocado_worktree_layout,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bakar.release_workspace import ReleaseRoutingError
    from bakar.steps import avocado_dispatch, kas_pins

    lay = meta_avocado_worktree_layout
    _user_config(monkeypatch, workspaces_template="/srv/peridio-{release}")

    def boom(kas_yaml: Path, checkout: Path) -> tuple[str, dict]:
        raise ReleaseRoutingError("no openembedded-core pin")

    monkeypatch.setattr(kas_pins, "probe_pins", boom)
    avocado = _record(monkeypatch, avocado_dispatch, "dispatch_avocado_build")
    monkeypatch.chdir(lay.worktree)

    result = _invoke_on(runner, monkeypatch, "kas/machine/m.yml")

    assert result.exit_code == 2, result.output
    assert "no openembedded-core pin" in " ".join(result.output.split())
    assert fake_sp.calls == []
    assert avocado == []


def test_meta_avocado_mapping_dispatches_to_the_routed_workspace(
    runner: _CliRunner,
    meta_avocado_worktree_layout,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The routed workspace need not exist locally: only the remote copy matters."""
    from bakar.steps import avocado_dispatch

    lay = meta_avocado_worktree_layout
    _user_config(
        monkeypatch,
        workspaces_template=str(tmp_path / "absent-{release}"),
        dispatch_worktree_root="/srv/wt",
        dispatch_snapshot_untracked_cap_mb=7,
        build_worktree_build_dir="build-{stem}-{worktree}",
    )
    probed = _probe(monkeypatch, "wrynose")
    avocado = _record(monkeypatch, avocado_dispatch, "dispatch_avocado_build")
    mirror = _record(monkeypatch, rd, "dispatch_remote_build")
    monkeypatch.chdir(lay.worktree)

    result = _invoke_on(runner, monkeypatch, "kas/machine/m.yml")

    assert result.exit_code == 0, result.output
    assert mirror == []
    assert probed == [lay.worktree]
    (args, kwargs) = avocado[0]
    assert kwargs == {}
    host, request, flags = args
    assert host == HOST
    assert request.workspace == tmp_path / "absent-wrynose"
    assert request.checkout == lay.worktree
    assert request.worktree.checkout == lay.worktree
    assert request.worktree.main_checkout == lay.main
    assert request.kas_yaml == lay.kas_yaml
    assert request.kas_arg == "kas/machine/m.yml"
    assert request.invoking_cwd == lay.worktree
    assert request.release == "wrynose"
    assert request.local_args == ["build", "kas/machine/m.yml", "--on", HOST, "--yes"]
    assert request.worktree_root == Path("/srv/wt")
    assert request.build_dir_template == "build-{stem}-{worktree}"
    assert request.cap_mb == 7
    assert flags.assume_yes is True
    assert flags.sccache_dist is False


def test_meta_avocado_dispatch_exit_code_propagates(
    runner: _CliRunner,
    meta_avocado_worktree_layout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bakar.steps import avocado_dispatch

    lay = meta_avocado_worktree_layout
    _user_config(monkeypatch, workspaces_template="/srv/peridio-{release}")
    _probe(monkeypatch)
    monkeypatch.setattr(avocado_dispatch, "dispatch_avocado_build", lambda *a, **k: 17)
    monkeypatch.chdir(lay.worktree)

    result = _invoke_on(runner, monkeypatch, "kas/machine/m.yml")

    assert result.exit_code == 17, result.output


def test_meta_avocado_on_dry_run_passes_dry_run_flag(
    runner: _CliRunner,
    meta_avocado_worktree_layout,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bakar.steps import avocado_dispatch

    lay = meta_avocado_worktree_layout
    _user_config(monkeypatch, workspaces_template="/srv/peridio-{release}")
    _probe(monkeypatch)
    avocado = _record(monkeypatch, avocado_dispatch, "dispatch_avocado_build")
    monkeypatch.chdir(lay.worktree)
    argv = ["build", "kas/machine/m.yml", "--dry-run", "--on", HOST]
    monkeypatch.setattr(sys, "argv", ["bakar", *argv])

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert fake_sp.calls == []
    assert avocado[0][0][2].dry_run is True


def test_on_dry_run_script_refused(
    runner: _CliRunner,
    meta_avocado_worktree_layout,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lay = meta_avocado_worktree_layout
    monkeypatch.chdir(lay.worktree)
    argv = ["build", "kas/machine/m.yml", "--dry-run-script", "-", "--on", HOST]
    monkeypatch.setattr(sys, "argv", ["bakar", *argv])

    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    assert "--dry-run-script" in " ".join(result.output.split())
    assert fake_sp.calls == []


def test_on_dry_run_detach_refused(
    runner: _CliRunner,
    meta_avocado_worktree_layout,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lay = meta_avocado_worktree_layout
    monkeypatch.chdir(lay.worktree)
    argv = ["build", "kas/machine/m.yml", "--dry-run", "--detach", "--on", HOST]
    monkeypatch.setattr(sys, "argv", ["bakar", *argv])

    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    assert "--detach cannot be combined with --dry-run" in " ".join(result.output.split())
    assert fake_sp.calls == []


def test_dispatch_request_carries_pinned_repos_and_source(
    runner: _CliRunner,
    meta_avocado_worktree_layout,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from bakar.steps import avocado_dispatch, kas_pins

    lay = meta_avocado_worktree_layout
    pins = {"meta-avocado": {"commit": "a" * 40}}
    monkeypatch.setattr(kas_pins, "probe_pins", lambda _y, _c: ("wrynose", pins))
    avocado = _record(monkeypatch, avocado_dispatch, "dispatch_avocado_build")
    monkeypatch.chdir(lay.worktree)

    _user_config(monkeypatch, workspaces_releases={"wrynose": str(tmp_path / "w")})
    assert _invoke_on(runner, monkeypatch, "kas/machine/m.yml").exit_code == 0
    request = avocado[0][0][1]
    assert request.pinned_repos == pins
    assert request.workspace_source == "[workspaces.releases] entry for 'wrynose'"

    _user_config(monkeypatch, workspaces_template=str(tmp_path / "t-{release}"))
    assert _invoke_on(runner, monkeypatch, "kas/machine/m.yml").exit_code == 0
    assert avocado[1][0][1].workspace_source == "[workspaces] template"


def test_nxp_manifest_build_uses_the_additive_mirror(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bakar.steps import avocado_dispatch

    _user_config(monkeypatch, workspaces_template="/srv/peridio-{release}")
    avocado = _record(monkeypatch, avocado_dispatch, "dispatch_avocado_build")
    mirror = _record(monkeypatch, rd, "dispatch_remote_build")

    result = _invoke_on(runner, monkeypatch, "-f", "imx-6.12.49-2.2.0.xml", "-m", "imx95-var-dart")

    assert result.exit_code == 0, result.output
    assert avocado == []
    assert len(mirror) == 1
    args, kwargs = mirror[0]
    assert args[0] == HOST
    assert args[1].ws_root == workspace
    assert "extra_roots" not in kwargs


# ---------------------------------------------------------------------------
# (k) A release workspace never reaches the mirror, whatever shape the build takes.
# ---------------------------------------------------------------------------

_RELEASE_YAML = (
    "header:\n  version: 16\nmachine: raspberrypi5\nrepos:\n  meta-avocado:\n    path: meta-avocado\n"
    "  openembedded-core:\n    url: https://example.invalid/oe.git\n    branch: wrynose-avocado\n"
)


@pytest.fixture
def release_ws(tmp_path: Path) -> Path:
    """A release workspace: meta-avocado at the top, a sibling layer, a generated build dir."""
    ws = tmp_path / "peridio-wrynose"
    (ws / "meta-avocado" / "kas" / "machine").mkdir(parents=True)
    (ws / "meta-peridio" / "kas").mkdir(parents=True)
    (ws / "build-raspberrypi5").mkdir()
    (ws / ".bakar.toml").write_text("")
    for yml in (
        ws / "build-raspberrypi5" / "avocado-bakar.yml",
        ws / "local-rpi5.yml",
        ws / "meta-peridio" / "kas" / "x.yml",
        ws / "meta-avocado" / "kas" / "machine" / "raspberrypi5.yml",
    ):
        yml.write_text(_RELEASE_YAML)
    return ws


def _squash(text: str) -> str:
    """``text`` without whitespace: Rich wraps a long tmp path mid-component."""
    return "".join(text.split())


_CONFIGURED_REASON = "it is a configured release workspace, which bakar builds through the layer-delivery flow"
_HOLDS_REASON = "it holds a meta-avocado repository whose clone on the host the mirror would overwrite"


def _assert_refused_before_any_remote_call(
    result, fake_sp: FakeSubprocess, mirror: list, ws: Path, reason: str = _HOLDS_REASON
) -> None:
    assert result.exit_code == 2, result.output
    out = _squash(result.output)
    assert _squash(f"cannot mirror {ws} to {HOST}: {reason}") in out
    assert _squash(f"bakar build <path to a kas file inside a meta-avocado checkout> --on {HOST}") in out
    assert fake_sp.calls == []
    assert mirror == []


@pytest.mark.parametrize(
    "yml",
    ["build-raspberrypi5/avocado-bakar.yml", "local-rpi5.yml", "meta-peridio/kas/x.yml"],
    ids=["generated-build-yml", "kas-at-workspace-root", "sibling-repo-kas"],
)
def test_release_workspace_kas_shapes_are_refused(
    runner: _CliRunner,
    release_ws: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
    yml: str,
) -> None:
    _user_config(monkeypatch)
    mirror = _record(monkeypatch, rd, "dispatch_remote_build")
    monkeypatch.chdir(release_ws.parent)

    result = _invoke_on(runner, monkeypatch, str(release_ws / yml))

    _assert_refused_before_any_remote_call(result, fake_sp, mirror, release_ws)


@pytest.mark.parametrize("args", [(), ("--preset", "rpi")], ids=["bare", "preset"])
def test_release_workspace_bare_and_preset_builds_are_refused(
    runner: _CliRunner,
    release_ws: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
) -> None:
    # A preset resolves after the --on branch, so it reaches the mirror the way
    # a bare build from inside the workspace does.
    _user_config(monkeypatch)
    mirror = _record(monkeypatch, rd, "dispatch_remote_build")
    monkeypatch.chdir(release_ws)

    result = _invoke_on(runner, monkeypatch, *args)

    _assert_refused_before_any_remote_call(result, fake_sp, mirror, release_ws)


def test_workspace_named_by_the_template_is_refused_without_a_meta_avocado(
    runner: _CliRunner,
    tmp_path: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = tmp_path / "peridio-scarthgap"
    ws.mkdir()
    (ws / ".bakar.toml").write_text("")
    _user_config(monkeypatch, workspaces_template=str(tmp_path / "peridio-{release}"))
    mirror = _record(monkeypatch, rd, "dispatch_remote_build")
    monkeypatch.chdir(ws)

    result = _invoke_on(runner, monkeypatch)

    _assert_refused_before_any_remote_call(result, fake_sp, mirror, ws, _CONFIGURED_REASON)


def test_configured_release_workspace_holding_meta_avocado_says_configured(
    runner: _CliRunner,
    release_ws: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both signals hold; the configured mapping is the stronger statement.
    _user_config(monkeypatch, workspaces_template=str(release_ws.parent / "peridio-{release}"))
    mirror = _record(monkeypatch, rd, "dispatch_remote_build")
    monkeypatch.chdir(release_ws)

    result = _invoke_on(runner, monkeypatch)

    _assert_refused_before_any_remote_call(result, fake_sp, mirror, release_ws, _CONFIGURED_REASON)


def test_layer_repo_holding_meta_avocado_is_refused_without_release_wording(
    runner: _CliRunner,
    tmp_path: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The meta-avocado-nonpublic shape: a layer repository whose layer directory
    # is meta-avocado. It is no release workspace, but the mirror would still
    # overwrite the host's clone of it.
    repo = tmp_path / "meta-avocado-nonpublic"
    (repo / "meta-avocado").mkdir(parents=True)
    (repo / ".bakar.toml").write_text("")
    _user_config(monkeypatch, workspaces_template=str(tmp_path / "peridio-{release}"))
    mirror = _record(monkeypatch, rd, "dispatch_remote_build")
    monkeypatch.chdir(repo)

    result = _invoke_on(runner, monkeypatch)

    _assert_refused_before_any_remote_call(result, fake_sp, mirror, repo, _HOLDS_REASON)
    assert "release workspace" not in " ".join(result.output.split())


def test_meta_avocado_kas_in_a_release_workspace_still_routes(
    runner: _CliRunner,
    release_ws: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bakar.steps import avocado_dispatch

    _user_config(monkeypatch, workspaces_template=str(release_ws.parent / "peridio-{release}"))
    _probe(monkeypatch, "wrynose")
    avocado = _record(monkeypatch, avocado_dispatch, "dispatch_avocado_build")
    mirror = _record(monkeypatch, rd, "dispatch_remote_build")
    monkeypatch.chdir(release_ws.parent)

    result = _invoke_on(runner, monkeypatch, str(release_ws / "meta-avocado" / "kas" / "machine" / "raspberrypi5.yml"))

    assert result.exit_code == 0, result.output
    assert mirror == []
    assert len(avocado) == 1


# ---------------------------------------------------------------------------
# (l) -w with a meta-avocado --on build must name the routed workspace.
# ---------------------------------------------------------------------------


def test_meta_avocado_with_a_different_workspace_exits_2(
    runner: _CliRunner,
    meta_avocado_worktree_layout,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from bakar.steps import avocado_dispatch

    lay = meta_avocado_worktree_layout
    other = tmp_path / "peridio-test"
    other.mkdir()
    _user_config(monkeypatch, workspaces_template=str(tmp_path / "peridio-{release}"))
    _probe(monkeypatch, "wrynose")
    avocado = _record(monkeypatch, avocado_dispatch, "dispatch_avocado_build")
    monkeypatch.chdir(lay.worktree)

    result = _invoke_on(runner, monkeypatch, "-w", str(other), str(lay.kas_yaml))

    assert result.exit_code == 2, result.output
    out = _squash(result.output)
    routed = tmp_path / "peridio-wrynose"
    assert _squash(f"-w {other} does not match the release workspace {routed}") in out
    # -w must exist locally, so the advice cannot be a bare "pass -w <routed>".
    assert _squash(f"--on builds in the workspace the release maps to on {HOST}") in out
    assert _squash(f"Drop -w (or point it at {routed} if that path exists locally)") in out
    assert _squash(f"pass -w {routed}") not in out
    assert avocado == []
    assert fake_sp.calls == []


def test_meta_avocado_with_the_routed_workspace_dispatches(
    runner: _CliRunner,
    meta_avocado_worktree_layout,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from bakar.steps import avocado_dispatch

    lay = meta_avocado_worktree_layout
    routed = tmp_path / "peridio-wrynose"
    routed.mkdir()
    _user_config(monkeypatch, workspaces_template=str(tmp_path / "peridio-{release}"))
    _probe(monkeypatch, "wrynose")
    avocado = _record(monkeypatch, avocado_dispatch, "dispatch_avocado_build")
    monkeypatch.chdir(lay.worktree)

    result = _invoke_on(runner, monkeypatch, "-w", str(routed), str(lay.kas_yaml))

    assert result.exit_code == 0, result.output
    assert avocado[0][0][1].workspace == routed


# ---------------------------------------------------------------------------
# (m) Requests that run a clean task on the host are refused before any ssh.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "env", "named"),
    [
        ((), {"KAS_TASK": "cleanall"}, "KAS_TASK=cleanall"),
        (("-t", "busybox:do_cleanall"), {}, "--target busybox:do_cleanall"),
        (("--target", "busybox:cleansstate"), {}, "--target busybox:cleansstate"),
        ((), {"KAS_TARGET": "core-image-minimal busybox:do_clean"}, "KAS_TARGET busybox:do_clean"),
        (("-i", "core-image-minimal:do_cleanall"), {}, "--image core-image-minimal:do_cleanall"),
        (("--image", "core-image-minimal:cleansstate"), {}, "--image core-image-minimal:cleansstate"),
        ((), {"BAKAR_IMAGE": "core-image-minimal:do_cleanall"}, "BAKAR_IMAGE core-image-minimal:do_cleanall"),
    ],
    ids=["kas-task", "short-target", "long-target", "kas-target", "short-image", "long-image", "bakar-image"],
)
def test_deleting_requests_with_on_are_refused(  # noqa: PLR0913 - pytest fixtures plus the three parameters
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    fake_sp: FakeSubprocess,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
    env: dict[str, str],
    named: str,
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _user_config(monkeypatch)
    mirror = _record(monkeypatch, rd, "dispatch_remote_build")

    result = _invoke_on(runner, monkeypatch, str(generic_yaml), *args)

    assert result.exit_code == 2, result.output
    assert f"{named} cannot be combined with --on" in " ".join(result.output.split())
    assert fake_sp.calls == []
    assert mirror == []


@pytest.mark.parametrize(
    ("args", "env"),
    [
        (("-t", "busybox"), {}),
        (("-t", "busybox:do_build"), {}),
        (("-t", "core-image-minimal"), {}),
        ((), {"KAS_TASK": "build"}),
    ],
    ids=["recipe", "do-build", "image", "kas-task-build"],
)
def test_plain_build_requests_with_on_are_dispatched(  # noqa: PLR0913 - pytest fixtures plus the two parameters
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
    env: dict[str, str],
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _user_config(monkeypatch)
    mirror = _record(monkeypatch, rd, "dispatch_remote_build")

    result = _invoke_on(runner, monkeypatch, str(generic_yaml), *args)

    assert result.exit_code == 0, result.output
    assert len(mirror) == 1
