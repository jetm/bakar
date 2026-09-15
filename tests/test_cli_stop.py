"""Tests for the ``bakar stop`` command.

Each test sets up a tmp workspace with a ``.bakar.toml`` marker so
``_workspace_from_cwd`` finds the workspace, then monkeypatches
``build_stop.stop_build`` on the command module with a recording function
so no real build is signaled. The ``stop`` command resolves the BSP family
via ``_bsp_from_cwd``, which keys off cwd being inside ``workspace/nxp/`` -
so the fixture chdirs into ``<workspace>/nxp/`` and ``cfg.bsp_root`` is
``<workspace>/nxp``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bakar.commands.stop as stop_cmd
from bakar.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp workspace with a ``.bakar.toml`` marker; chdir into ``nxp/``.

    The marker file is what ``_workspace_from_cwd`` keys off first (walking up
    from cwd). Chdir-ing into the ``nxp/`` subdirectory makes ``_bsp_from_cwd``
    auto-detect the NXP family, so the resolved ``cfg.bsp_root`` points at
    ``<workspace>/nxp/``.
    """
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp").mkdir()
    (tmp_path / "nxp" / "build" / "runs" / "20260617-120000").mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "nxp")
    return tmp_path


def test_stop_no_args(runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop`` with no args exits 0 and calls stop_build once with force=False."""
    calls: list[tuple[Path, bool]] = []

    def _rec(bsp_root: Path, cfg: object = None, *, force: bool = False, grace_seconds: float = 0) -> bool:
        calls.append((bsp_root, force))
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _rec)

    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0] == (workspace / "nxp", False)


def test_stop_force(runner: _CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop --force`` calls stop_build with force=True."""
    calls: list[tuple[Path, bool]] = []

    def _rec(bsp_root: Path, cfg: object = None, *, force: bool = False, grace_seconds: float = 0) -> bool:
        calls.append((bsp_root, force))
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _rec)

    result = runner.invoke(app, ["stop", "--force"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0] == (workspace / "nxp", True)


def test_stop_explicit_workspace(
    runner: _CliRunner,
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stop --workspace WS`` resolves the bsp_root from the flag, not cwd.

    Cwd sits outside any workspace; an NXP ``--manifest`` drives the family so
    ``_bsp_from_cwd`` is bypassed, and the resolved ``cfg.bsp_root`` must be
    derived from the explicit ``--workspace`` (``<workspace>/nxp``).
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    calls: list[tuple[Path, bool]] = []

    def _rec(bsp_root: Path, cfg: object = None, *, force: bool = False, grace_seconds: float = 0) -> bool:
        calls.append((bsp_root, force))
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _rec)

    result = runner.invoke(
        app,
        ["stop", "--workspace", str(workspace), "--manifest", "imx-6.6.52-2.2.2.xml"],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0] == (workspace / "nxp", False)


def test_stop_byo_positional_yaml(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bakar stop my.yml`` resolves bsp_root from the YAML's dir (BYO/generic).

    A generic kas YAML (no NXP/TI markers) is dispatched via
    ``_dispatch_from_yaml`` -> ``generic``, and the workspace is the YAML's
    parent dir - cwd is irrelevant, so this runs from outside any workspace.
    """
    yaml = tmp_path / "kas-generic.yml"
    yaml.write_text("header:\n  version: 21\nmachine: qemux86-64\ndistro: nodistro\ntarget: core-image-minimal\n")
    (tmp_path / "build" / "runs" / "20260617-120000").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    calls: list[tuple[Path, bool]] = []

    def _rec(bsp_root: Path, cfg: object = None, *, force: bool = False, grace_seconds: float = 0) -> bool:
        calls.append((bsp_root, force))
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _rec)

    result = runner.invoke(app, ["stop", str(yaml)])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0] == (tmp_path, False)


def test_stop_yaml_and_manifest_conflict(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing both a positional YAML and ``--manifest`` is rejected with exit 2."""
    yaml = tmp_path / "kas-generic.yml"
    yaml.write_text("header:\n  version: 21\nmachine: qemux86-64\n")

    calls: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        stop_cmd.build_stop,
        "stop_build",
        lambda bsp_root, cfg=None, *, force=False, grace_seconds=0: calls.append((bsp_root, force)),
    )

    result = runner.invoke(app, ["stop", str(yaml), "--manifest", "imx-6.6.52-2.2.2.xml"])

    assert result.exit_code == 2
    assert calls == []


def test_stop_returns_false_exits_nonzero(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``stop_build`` returns False (nothing to stop), the command exits 1."""
    yaml = tmp_path / "kas-generic.yml"
    yaml.write_text("header:\n  version: 21\nmachine: qemux86-64\ndistro: nodistro\ntarget: core-image-minimal\n")
    (tmp_path / "build" / "runs" / "20260617-120000").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    monkeypatch.setattr(
        stop_cmd.build_stop, "stop_build", lambda bsp_root, cfg=None, *, force=False, grace_seconds=0: False
    )

    result = runner.invoke(app, ["stop", str(yaml)])

    assert result.exit_code == 1, result.output


def test_stop_returns_true_exits_zero(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``stop_build`` returns True (a build was signaled), the command exits 0."""
    yaml = tmp_path / "kas-generic.yml"
    yaml.write_text("header:\n  version: 21\nmachine: qemux86-64\ndistro: nodistro\ntarget: core-image-minimal\n")
    (tmp_path / "build" / "runs" / "20260617-120000").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    monkeypatch.setattr(
        stop_cmd.build_stop, "stop_build", lambda bsp_root, cfg=None, *, force=False, grace_seconds=0: True
    )

    result = runner.invoke(app, ["stop", str(yaml)])

    assert result.exit_code == 0, result.output


def test_stop_force_returns_false_exits_nonzero(
    runner: _CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stop --force`` still exits 1 when ``stop_build`` finds nothing to stop.

    ``--force`` skips the grace wait but does not manufacture a target: when no
    live/targetable build exists ``stop_build`` returns False and the command
    must still exit 1. Also proves ``force=True`` is threaded through to
    ``stop_build``.
    """
    yaml = tmp_path / "kas-generic.yml"
    yaml.write_text("header:\n  version: 21\nmachine: qemux86-64\ndistro: nodistro\ntarget: core-image-minimal\n")
    (tmp_path / "build" / "runs" / "20260617-120000").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    calls: list[tuple[Path, bool]] = []

    def _rec(bsp_root: Path, cfg: object = None, *, force: bool = False, grace_seconds: float = 0) -> bool:
        calls.append((bsp_root, force))
        return False

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _rec)

    result = runner.invoke(app, ["stop", "--force", str(yaml)])

    assert result.exit_code == 1, result.output
    assert calls == [(tmp_path, True)]


def test_stop_timeout_overrides_config_default(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stop --timeout 45`` threads grace_seconds=45 to stop_build regardless of config."""
    calls: list[tuple[Path, bool, float]] = []

    def _rec(bsp_root: Path, cfg: object = None, *, force: bool = False, grace_seconds: float = 0) -> bool:
        calls.append((bsp_root, force, grace_seconds))
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _rec)

    result = runner.invoke(app, ["stop", "--timeout", "45"])

    assert result.exit_code == 0, result.output
    assert calls == [(workspace / "nxp", False, 45.0)]


def test_stop_no_timeout_falls_back_to_config_stop_grace_seconds(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--timeout``, grace_seconds comes from [build] stop_grace_seconds.

    The app callback writes ``_state._USER_CONFIG = _load_user_config_safe()``
    on every invocation, so monkeypatching the loader (not the module-level
    variable directly) is the only stable way to plant a fixed value before
    the CLI reaches the stop subcommand.
    """
    import bakar.commands._app as _state
    from bakar.user_config import UserConfig

    monkeypatch.setattr(_state, "_load_user_config_safe", lambda: UserConfig(stop_grace_seconds=30))

    calls: list[tuple[Path, bool, float]] = []

    def _rec(bsp_root: Path, cfg: object = None, *, force: bool = False, grace_seconds: float = 0) -> bool:
        calls.append((bsp_root, force, grace_seconds))
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _rec)

    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0, result.output
    assert calls == [(workspace / "nxp", False, 30)]


def test_stop_on_host_stops_the_remote_dispatch(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop --on <host>`` stops the detached build on that host, not a local one."""
    calls: list[tuple[str, bool, float]] = []

    def _rec(host: str, *, force: bool = False, grace_seconds: float = 0, stop_all: bool = False) -> bool:
        calls.append((host, force, grace_seconds))
        return True

    monkeypatch.setattr(stop_cmd.remote_dispatch, "stop_remote_dispatch", _rec)

    def _boom(*a: object, **k: object) -> bool:
        raise AssertionError("--on must not signal a local build")

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _boom)

    result = runner.invoke(app, ["stop", "--on", "pc2", "--timeout", "45"])

    assert result.exit_code == 0, result.output
    assert calls == [("pc2", False, 45.0)]


def test_stop_on_host_needs_no_workspace(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A remote stop resolves nothing locally: the build is on the other host.

    Run from a directory that is not a bakar workspace at all - which is exactly
    where someone reaches for it, having lost the terminal that dispatched.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(stop_cmd.remote_dispatch, "stop_remote_dispatch", lambda host, **k: True)

    result = runner.invoke(app, ["stop", "--on", "pc2"])

    assert result.exit_code == 0, result.output


def test_stop_on_host_exits_nonzero_when_nothing_was_running(
    runner: _CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(stop_cmd.remote_dispatch, "stop_remote_dispatch", lambda host, **k: False)

    result = runner.invoke(app, ["stop", "--on", "pc2"])

    assert result.exit_code == 1


def test_stop_on_host_forwards_force(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, bool, float]] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        stop_cmd.remote_dispatch,
        "stop_remote_dispatch",
        lambda host, *, force=False, grace_seconds=0, stop_all=False: (
            calls.append((host, force, grace_seconds)),
            True,
        )[1],
    )

    result = runner.invoke(app, ["stop", "--on", "pc2", "--force"])

    assert result.exit_code == 0, result.output
    assert calls == [("pc2", True, 30)]


def test_stop_on_host_defaults_to_a_single_build(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``--all`` the remote stop must not opt into killing every build.

    On a shared builder the other running dispatch units are someone else's
    in-flight build, so the default has to be the conservative one.
    """
    seen: list[bool] = []
    monkeypatch.setattr(
        stop_cmd.remote_dispatch,
        "stop_remote_dispatch",
        lambda host, *, force=False, grace_seconds=0, stop_all=False: (seen.append(stop_all), True)[1],
    )

    assert runner.invoke(app, ["stop", "--on", "pc2"]).exit_code == 0
    assert runner.invoke(app, ["stop", "--on", "pc2", "--all"]).exit_code == 0
    assert seen == [False, True]


def test_run_option_errors_on_unmatched_id(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stop --run <id>`` with no matching run directory anywhere exits nonzero.

    Nothing under any family root is named ``does-not-exist``, so this must be
    told apart from a match that simply is not live.
    """
    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: False)

    result = runner.invoke(app, ["stop", "--run", "does-not-exist"])

    assert result.exit_code != 0
    assert "does-not-exist" in result.output


def test_run_option_unmatched_id_surfaces_peer_held_root(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stop --run <id>`` with no matching run ANYWHERE also surfaces any
    root the NFS lock-ownership gate refused - the id may live on exactly
    that root, which "no match" alone cannot tell apart from "no such id
    anywhere in this workspace"."""
    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: False)

    ti_root = stop_cmd.build_stop.RunRoot(
        bsp_root=workspace / "ti", family="ti", resolve_workspace=workspace, resolve_family="ti"
    )
    refusal = stop_cmd.build_stop.LockRefusal(reason="peer-held", host="pc2")
    skipped_root = stop_cmd.build_stop.SkippedRoot(root=ti_root, refusal=refusal)

    monkeypatch.setattr(
        stop_cmd.build_stop,
        "enumerate_workspace_runs",
        lambda _path, **_kw: stop_cmd.build_stop.RunScan(candidates=[], skipped=[skipped_root]),
    )

    result = runner.invoke(app, ["stop", "--run", "maybe-on-ti"])

    assert result.exit_code != 0
    flat_output = " ".join(result.output.split())
    assert "owned by pc2" in flat_output
    assert str(workspace / "ti") in flat_output
    assert "no run matching" in flat_output


def test_run_option_errors_on_matched_but_not_live_run(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``--run`` id matching a real, non-live run directory must be told

    apart from "no match anywhere": it exits nonzero, states the run is not
    currently live, and must not send any signal.
    """
    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: False)
    calls: list[Path] = []
    monkeypatch.setattr(
        stop_cmd.build_stop,
        "stop_run",
        lambda run_dir, cfg=None, *, force=False, grace_seconds=0: (calls.append(run_dir), True)[1],
    )

    # `workspace` fixture already created nxp/build/runs/20260617-120000 with
    # no launch record, so it is a candidate but never live.
    result = runner.invoke(app, ["stop", "--run", "20260617-120000"])

    assert result.exit_code != 0
    assert "not currently live" in result.output
    assert calls == []


def test_run_option_errors_on_matched_container_run_whose_container_already_exited(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``--run`` id matching a container-mode run whose container has
    already exited must report "not currently live", not silently succeed.

    ``live_workspace_runs``' container-mode check queries the runtime via
    ``_container_id_status`` and only excludes a candidate on a CONFIRMED
    ``_DEAD`` verdict - a launch record with a label alone is not enough,
    and neither is a query failure (see the sibling stale-records test for
    that distinction). This fixture simulates a confirmed-dead container: a
    real query that ran and found nothing. That must dispatch to
    stop_run, whose idempotent stale-cleanup path returns True for a
    container the runtime no longer has - so --run must perform its own
    real liveness query before ever reaching stop_run.
    """
    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: False)

    run_dir = workspace / "nxp" / "build" / "runs" / "20260617-140000-container"
    run_dir.mkdir(parents=True)
    stop_cmd.build_stop.write_launch_record(
        run_dir, pgid=0, mode="container", runtime="docker", container_label="bakar.run_id=20260617-140000-container"
    )

    monkeypatch.setattr(stop_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(
        stop_cmd.build_stop, "_container_id_status", lambda _runtime, _label: (stop_cmd.build_stop._DEAD, None)
    )

    calls: list[Path] = []
    monkeypatch.setattr(
        stop_cmd.build_stop,
        "stop_run",
        lambda run_dir, cfg=None, *, force=False, grace_seconds=0: (calls.append(run_dir), True)[1],
    )

    result = runner.invoke(app, ["stop", "--run", "20260617-140000-container"])

    assert result.exit_code != 0
    assert "not currently live" in result.output
    assert calls == []


def test_run_option_stops_matched_live_run_directly(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``--run`` id matching a live run calls ``stop_run`` directly on it,

    without ever constructing the multi-build listing.
    """
    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: False)

    run_dir = workspace / "nxp" / "build" / "runs" / "20260617-120000"
    stop_cmd.build_stop.write_launch_record(run_dir, pgid=4242, mode="host")
    monkeypatch.setattr(stop_cmd.build_stop, "is_build_running", lambda _rd: (True, 4242, True))

    calls: list[tuple[Path, bool, float]] = []

    def _rec(run_dir: Path, cfg: object = None, *, force: bool = False, grace_seconds: float = 0) -> bool:
        calls.append((run_dir, force, grace_seconds))
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_run", _rec)

    result = runner.invoke(app, ["stop", "--run", "20260617-120000"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0] == run_dir


def test_stop_no_args_stale_container_records_fall_through_to_zero_live(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two or more container-mode run records whose containers have already
    exited (a launch record survives on disk after a runtime restart, a
    manual ``docker kill``, or a host crash - anything that bypasses bakar
    stop's own cleanup) must not be counted as "two or more live builds".

    Before container-mode liveness was runtime-verified, ANY recorded
    container label counted as live, so a workspace accumulating stale
    records could get stuck permanently: the no-argument path saw "2+ live"
    and refused with instructions to pass --run, while --run on either one
    reported "not currently live" - no path ever reached stop_build's own
    stale-lock cleanup. With real liveness verification confirming both
    records are genuinely _DEAD (not merely unqueryable - see the
    query-failure-fails-conservative test for that distinction), zero of
    these records are live, so the command falls through to the existing
    single-root stop_build call and its stale-cleanup path, exactly as it
    would for a workspace with no container records at all.
    """
    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: False)

    nxp_dir = workspace / "nxp" / "build" / "runs" / "20260617-150000-nxp-dead"
    nxp_dir.mkdir(parents=True)
    stop_cmd.build_stop.write_launch_record(
        nxp_dir, pgid=0, mode="container", runtime="docker", container_label="bakar.run_id=nxp-dead"
    )
    ti_dir = workspace / "ti" / "build" / "runs" / "20260617-160000-ti-dead"
    ti_dir.mkdir(parents=True)
    stop_cmd.build_stop.write_launch_record(
        ti_dir, pgid=0, mode="container", runtime="docker", container_label="bakar.run_id=ti-dead"
    )

    monkeypatch.setattr(stop_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(
        stop_cmd.build_stop, "_container_id_status", lambda _runtime, _label: (stop_cmd.build_stop._DEAD, None)
    )

    calls: list[Path] = []
    monkeypatch.setattr(
        stop_cmd.build_stop,
        "stop_build",
        lambda bsp_root, cfg=None, *, force=False, grace_seconds=0: (calls.append(bsp_root), True)[1],
    )

    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0, result.output
    assert "live builds are running" not in result.output
    assert len(calls) == 1


def test_stop_no_args_container_query_failure_keeps_candidate_live(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container-mode run whose runtime query FAILS (daemon unreachable,
    binary missing, timeout) must stay counted as live, not be dropped.

    _container_id_status's _ERROR verdict means the query could not be
    trusted, not that the container is confirmed gone - collapsing it to
    "not live" would silently drop a genuinely running build out of
    discovery the moment the runtime has a hiccup. Only a confirmed _DEAD
    excludes a candidate; _ERROR fails conservative and keeps it, so with
    exactly one such record present the command stops it directly instead
    of falling through to stop_build as though nothing were running.
    """
    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: False)

    run_dir = workspace / "nxp" / "build" / "runs" / "20260617-170000-unreachable"
    run_dir.mkdir(parents=True)
    stop_cmd.build_stop.write_launch_record(
        run_dir, pgid=0, mode="container", runtime="docker", container_label="bakar.run_id=unreachable"
    )

    monkeypatch.setattr(stop_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(
        stop_cmd.build_stop, "_container_id_status", lambda _runtime, _label: (stop_cmd.build_stop._ERROR, None)
    )

    calls: list[Path] = []

    def _rec(run_dir: Path, cfg: object = None, *, force: bool = False, grace_seconds: float = 0) -> bool:
        calls.append(run_dir)
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_run", _rec)

    def _boom(*a: object, **k: object) -> bool:
        raise AssertionError("a run kept live by a failed query must not fall back to stop_build")

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _boom)

    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0] == run_dir


def test_stop_no_args_single_live_build_stops_it_directly(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With exactly one live build, a bare ``stop`` calls ``stop_run`` on it

    directly - no listing, no prompt. This is the common case and must be a
    no-op change from the caller's perspective.
    """
    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: False)

    run_dir = workspace / "nxp" / "build" / "runs" / "20260617-120000"
    stop_cmd.build_stop.write_launch_record(run_dir, pgid=4242, mode="host")
    monkeypatch.setattr(stop_cmd.build_stop, "is_build_running", lambda _rd: (True, 4242, True))

    calls: list[tuple[Path, bool, float]] = []

    def _rec(run_dir: Path, cfg: object = None, *, force: bool = False, grace_seconds: float = 0) -> bool:
        calls.append((run_dir, force, grace_seconds))
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_run", _rec)

    def _boom(*a: object, **k: object) -> bool:
        raise AssertionError("a single live build must not fall back to stop_build")

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _boom)

    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0] == run_dir
    assert "live builds are running" not in result.output


def test_stop_no_args_single_live_build_honors_user_config_stop_grace_seconds(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workspace-wide discovery path resolves each root's BuildConfig
    with the CLI layer's user_config, exactly like the legacy single-root
    path already does - so a configured [build] stop_grace_seconds is
    honored by the no-argument single-live-build fast path too, not
    silently replaced by the hardcoded 30s default."""
    import bakar.commands._app as _state
    from bakar.user_config import UserConfig

    monkeypatch.setattr(_state, "_load_user_config_safe", lambda: UserConfig(stop_grace_seconds=300))
    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: False)

    run_dir = workspace / "nxp" / "build" / "runs" / "20260617-120000"
    stop_cmd.build_stop.write_launch_record(run_dir, pgid=4242, mode="host")
    monkeypatch.setattr(stop_cmd.build_stop, "is_build_running", lambda _rd: (True, 4242, True))

    calls: list[tuple[Path, float]] = []

    def _rec(run_dir: Path, cfg: object = None, *, force: bool = False, grace_seconds: float = 0) -> bool:
        calls.append((run_dir, grace_seconds))
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_run", _rec)

    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0, result.output
    assert calls == [(run_dir, 300)]


def test_stop_no_args_peer_held_root_surfaced_before_falling_through(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A root the NFS lock-ownership gate refuses is surfaced by name and
    reason, even when discovery finds zero live builds elsewhere - so a
    peer-held root's build is never indistinguishable from a workspace with
    genuinely nothing running."""
    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: False)

    ti_root = stop_cmd.build_stop.RunRoot(
        bsp_root=workspace / "ti", family="ti", resolve_workspace=workspace, resolve_family="ti"
    )
    refusal = stop_cmd.build_stop.LockRefusal(reason="peer-held", host="pc2")
    skipped_root = stop_cmd.build_stop.SkippedRoot(root=ti_root, refusal=refusal)

    monkeypatch.setattr(
        stop_cmd.build_stop,
        "enumerate_workspace_runs",
        lambda _path, **_kw: stop_cmd.build_stop.RunScan(candidates=[], skipped=[skipped_root]),
    )
    monkeypatch.setattr(stop_cmd.build_stop, "live_workspace_runs", lambda _path, **_kw: [])
    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", lambda *_a, **_k: True)

    result = runner.invoke(app, ["stop"])

    # Rich may wrap the message across lines under the test runner's narrow
    # default width, so match against whitespace-normalized output.
    flat_output = " ".join(result.output.split())
    assert "owned by pc2" in flat_output
    assert str(workspace / "ti") in flat_output


def test_stop_no_args_two_live_builds_refuses_and_lists(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two or more live builds with no ``--run`` refuse to stop anything.

    Every live build is listed by run id, family, machine, and elapsed time,
    and the operator is told to pass ``--run <id>`` - mirroring
    ``stop_remote_dispatch``'s multi-unit refusal shape.
    """
    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: False)

    run_a = workspace / "nxp" / "build" / "runs" / "20260617-120000"
    run_b = workspace / "nxp" / "build" / "runs" / "20260617-130000"
    run_b.mkdir(parents=True)
    stop_cmd.build_stop.write_launch_record(run_a, pgid=111, mode="host")
    stop_cmd.build_stop.write_launch_record(run_b, pgid=222, mode="host")
    monkeypatch.setattr(stop_cmd.build_stop, "is_build_running", lambda _rd: (True, 111, True))

    def _boom(*a: object, **k: object) -> bool:
        raise AssertionError("two or more live builds must not be auto-stopped")

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _boom)
    monkeypatch.setattr(stop_cmd.build_stop, "stop_run", _boom)

    result = runner.invoke(app, ["stop"])

    assert result.exit_code != 0
    assert "20260617-120000" in result.output
    assert "20260617-130000" in result.output
    assert "nxp" in result.output
    assert "--run" in result.output


def test_stop_no_args_two_live_builds_tty_prompts_and_stops_chosen(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On an interactive terminal, two or more live builds get a numbered

    prompt instead of a refusal. Picking an index dispatches through
    ``stop_run`` on the chosen candidate - the same call the non-interactive
    ``--run`` path makes, not a second stop-dispatch code path.
    """
    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: False)
    monkeypatch.setattr(stop_cmd, "_is_tty", lambda: True)

    run_a = workspace / "nxp" / "build" / "runs" / "20260617-120000"
    run_b = workspace / "nxp" / "build" / "runs" / "20260617-130000"
    run_b.mkdir(parents=True)
    stop_cmd.build_stop.write_launch_record(run_a, pgid=111, mode="host")
    stop_cmd.build_stop.write_launch_record(run_b, pgid=222, mode="host")
    monkeypatch.setattr(stop_cmd.build_stop, "is_build_running", lambda _rd: (True, 111, True))

    calls: list[tuple[Path, bool, float]] = []

    def _rec(run_dir: Path, cfg: object = None, *, force: bool = False, grace_seconds: float = 0) -> bool:
        calls.append((run_dir, force, grace_seconds))
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_run", _rec)

    def _boom(*a: object, **k: object) -> bool:
        raise AssertionError("interactive pick must not fall back to stop_build")

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _boom)
    monkeypatch.setattr(stop_cmd.typer, "prompt", lambda *a, **k: 2)

    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0, result.output
    assert "[1]" in result.output
    assert "[2]" in result.output
    assert len(calls) == 1
    assert calls[0][0] == run_b


def test_stop_no_args_two_live_builds_tty_invalid_choice_exits_nonzero(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An out-of-range interactive pick exits nonzero without stopping anything."""
    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: False)
    monkeypatch.setattr(stop_cmd, "_is_tty", lambda: True)

    run_a = workspace / "nxp" / "build" / "runs" / "20260617-120000"
    run_b = workspace / "nxp" / "build" / "runs" / "20260617-130000"
    run_b.mkdir(parents=True)
    stop_cmd.build_stop.write_launch_record(run_a, pgid=111, mode="host")
    stop_cmd.build_stop.write_launch_record(run_b, pgid=222, mode="host")
    monkeypatch.setattr(stop_cmd.build_stop, "is_build_running", lambda _rd: (True, 111, True))

    def _boom(*a: object, **k: object) -> bool:
        raise AssertionError("an invalid choice must not stop any build")

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _boom)
    monkeypatch.setattr(stop_cmd.build_stop, "stop_run", _boom)
    monkeypatch.setattr(stop_cmd.typer, "prompt", lambda *a, **k: 5)

    result = runner.invoke(app, ["stop"])

    assert result.exit_code != 0
    assert "not a valid choice" in result.output


def _snapshot_run_dir(run_dir: Path) -> dict[str, bytes]:
    """Map every file under ``run_dir`` (relative path -> bytes) for later comparison."""
    return {str(p.relative_to(run_dir)): p.read_bytes() for p in sorted(run_dir.rglob("*")) if p.is_file()}


def test_no_bulk_stop_and_isolation_between_concurrent_builds(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two negative tests the design deliberately keeps out of scope.

    1. ``--all`` is a flag Typer attaches to the whole ``stop`` command (it is
       only meaningful alongside ``--on``), so it is not rejected as an unknown
       option on the local path - but nothing on the local path reads it either,
       so passing it with 2+ live builds present must still hit the same
       refuse-and-list path as a bare ``stop``, never a bulk stop. ``stop_build``
       and ``stop_run`` are both wired to raise, proving neither is reached.
    2. With three live builds and one targeted via ``--run``, the other two
       must be provably untouched: their launch records/pid files are
       byte-identical before and after, and ``stop_run`` is never called with
       their run directories.
    """
    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: False)

    def _boom(*a: object, **k: object) -> bool:
        raise AssertionError("no invocation form may stop more than one live build")

    # --- Part 1: `--all` without `--on` must not become a bulk-stop path. ---
    run_a = workspace / "nxp" / "build" / "runs" / "20260617-120000"
    run_b = workspace / "nxp" / "build" / "runs" / "20260617-130000"
    run_b.mkdir(parents=True)
    stop_cmd.build_stop.write_launch_record(run_a, pgid=111, mode="host")
    stop_cmd.build_stop.write_launch_record(run_b, pgid=222, mode="host")
    monkeypatch.setattr(stop_cmd.build_stop, "is_build_running", lambda _rd: (True, 111, True))
    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _boom)
    monkeypatch.setattr(stop_cmd.build_stop, "stop_run", _boom)

    result = runner.invoke(app, ["stop", "--all"])

    assert result.exit_code != 0, result.output
    assert "20260617-120000" in result.output
    assert "20260617-130000" in result.output
    assert "--run" in result.output

    # --- Part 2: stopping one of three live builds leaves the other two alone. ---
    run_c = workspace / "nxp" / "build" / "runs" / "20260617-140000"
    run_c.mkdir(parents=True)
    stop_cmd.build_stop.write_launch_record(run_c, pgid=333, mode="host")
    monkeypatch.setattr(stop_cmd.build_stop, "is_build_running", lambda _rd: (True, 111, True))

    before_b = _snapshot_run_dir(run_b)
    before_c = _snapshot_run_dir(run_c)

    calls: list[Path] = []

    def _rec(run_dir: Path, cfg: object = None, *, force: bool = False, grace_seconds: float = 0) -> bool:
        calls.append(run_dir)
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_run", _rec)
    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _boom)

    result = runner.invoke(app, ["stop", "--run", "20260617-120000"])

    assert result.exit_code == 0, result.output
    assert calls == [run_a]

    after_b = _snapshot_run_dir(run_b)
    after_c = _snapshot_run_dir(run_c)
    assert after_b == before_b
    assert after_c == before_c
