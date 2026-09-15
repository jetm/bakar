"""Tests for the ``bakar ps`` command.

``bakar ps`` is directory-independent by design: it performs no workspace
resolution of any kind, so every test here monkeypatches the host-wide
discovery calls on ``bakar.commands.ps.build_stop`` directly rather than
building a ``.bakar.toml``-marked workspace fixture the way every other
CLI-command test suite does.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

import bakar.commands.ps as ps_cmd
from bakar.build_stop import ContainerCandidate, RunCandidate, RunRoot
from bakar.cli import app
from tests.conftest import make_build_config

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


def _no_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every host-wide discovery call to report nothing found.

    Shared setup for tests that do not care about discovery itself - without
    this, ``bakar ps`` would scan the real host's ``/proc`` and query the real
    container runtime, which is neither hermetic nor guaranteed to find
    nothing on a developer machine that happens to have a build running.
    """
    monkeypatch.setattr(ps_cmd.build_stop, "_discover_host_cookers", dict)
    monkeypatch.setattr(ps_cmd.build_stop, "correlate_host_discoveries", lambda _discovered, **_kw: [])
    monkeypatch.setattr(ps_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(ps_cmd.build_stop, "discover_running_containers_or_warn", lambda _runtime: ([], None))


def test_ps_empty_result_prints_plain_message(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """No live builds anywhere -> the documented plain-text line, not silence."""
    _no_discovery(monkeypatch)

    result = runner.invoke(app, ["ps"])

    assert result.exit_code == 0, result.output
    assert "no bakar builds running" in result.output


def test_ps_runs_outside_any_workspace(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`bakar ps` succeeds from a directory with no workspace markers at all.

    This is the required verify test: a bare tmpdir carries no
    ``.bakar.toml``, no ``nxp``/``ti``/``build-*`` subdirectory - nothing any
    workspace-resolution helper could key off. `_workspace_from_cwd` is
    patched to raise if it is ever called, so a regression that makes `ps`
    perform workspace resolution fails loudly here instead of only when run
    from a genuinely markerless directory in practice.
    """
    _no_discovery(monkeypatch)

    def _must_not_be_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("bakar ps must not perform workspace resolution")

    monkeypatch.setattr("bakar.commands._helpers._workspace_from_cwd", _must_not_be_called)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["ps"])

    assert result.exit_code == 0, result.output
    assert "no bakar builds running" in result.output


def test_ps_renders_host_mode_row(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A host-mode candidate renders its run id, mode, family, and machine.

    Family/machine come the same way group 9's correlation already resolves
    them: ``candidate.root.family`` and ``candidate.cfg.machine``.
    """
    run_dir = tmp_path / "nxp" / "build" / "runs" / "20260618-120000-111"
    run_dir.mkdir(parents=True)
    root = RunRoot(bsp_root=tmp_path / "nxp", family="nxp", resolve_workspace=tmp_path, resolve_family="nxp")
    cfg = make_build_config(workspace=tmp_path / "nxp", machine="imx8mp-var-dart")
    candidate = RunCandidate(run_dir=run_dir, root=root, cfg=cfg)

    monkeypatch.setattr(ps_cmd.build_stop, "_discover_host_cookers", lambda: {"fake": frozenset()})
    monkeypatch.setattr(ps_cmd.build_stop, "correlate_host_discoveries", lambda _discovered, **_kw: [candidate])
    monkeypatch.setattr(ps_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(ps_cmd.build_stop, "discover_running_containers_or_warn", lambda _runtime: ([], None))

    result = runner.invoke(app, ["ps"])

    assert result.exit_code == 0, result.output
    assert "20260618-120000-111" in result.output
    assert "mode=host" in result.output
    assert "family=nxp" in result.output
    assert "machine=imx8mp-var-dart" in result.output


def test_ps_container_row_falls_back_to_unknown_when_mount_unrecoverable(
    runner: _CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container row whose bind-mount source cannot be recovered falls back
    to "unknown" for family and machine instead of raising."""
    candidate = ContainerCandidate(run_id="20260618-130000-222", container_id="abc123")

    monkeypatch.setattr(ps_cmd.build_stop, "_discover_host_cookers", dict)
    monkeypatch.setattr(ps_cmd.build_stop, "correlate_host_discoveries", lambda _discovered, **_kw: [])
    monkeypatch.setattr(ps_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(ps_cmd.build_stop, "discover_running_containers_or_warn", lambda _runtime: ([candidate], None))
    # Simulate an unrecoverable mount path (runtime inspect failed/timed out/
    # no matching /work mount) without shelling out to a real container runtime.
    monkeypatch.setattr(ps_cmd, "_container_mount_source", lambda _runtime, _cid: None)

    result = runner.invoke(app, ["ps"])

    assert result.exit_code == 0, result.output
    assert "20260618-130000-222" in result.output
    assert "mode=container" in result.output
    assert "family=unknown" in result.output
    assert "machine=unknown" in result.output


def test_ps_container_row_falls_back_to_unknown_when_run_record_unreadable(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recovered mount path that yields no matching run record also falls
    back to "unknown" rather than raising - the run id in the container label
    has no corresponding run dir under the recovered mount source."""
    candidate = ContainerCandidate(run_id="20260618-140000-333", container_id="def456")
    # A real, empty directory: enumerate_workspace_runs succeeds but finds no
    # candidate whose run_dir.name matches this container's run id.
    empty_mount_source = tmp_path / "workspace"
    empty_mount_source.mkdir()

    monkeypatch.setattr(ps_cmd.build_stop, "_discover_host_cookers", dict)
    monkeypatch.setattr(ps_cmd.build_stop, "correlate_host_discoveries", lambda _discovered, **_kw: [])
    monkeypatch.setattr(ps_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(ps_cmd.build_stop, "discover_running_containers_or_warn", lambda _runtime: ([candidate], None))
    monkeypatch.setattr(ps_cmd, "_container_mount_source", lambda _runtime, _cid: str(empty_mount_source))

    result = runner.invoke(app, ["ps"])

    assert result.exit_code == 0, result.output
    assert "family=unknown" in result.output
    assert "machine=unknown" in result.output


def test_ps_container_row_resolves_nxp_family_through_real_mount_seam(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container-mode row whose bind-mount source is an nxp/ti bsp_root
    resolves its real family and machine, not the generic/bbsetup
    placeholder a workspace-scan of that same root would produce.

    Only `_container_mount_source` is mocked here - the rest of the row's
    resolution goes through the real `enumerate_workspace_runs` seam
    `_container_row_info` actually calls, so a regression that reintroduces
    the workspace-vs-bsp_root confusion this test guards against is caught
    through the same code path `bakar ps` exercises in production, not
    around it.
    """
    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: False)

    workspace = tmp_path / "ws"
    nxp_run_id = "20260618-170000-nxp"
    run_dir = workspace / "nxp" / "build" / "runs" / nxp_run_id
    run_dir.mkdir(parents=True)

    candidate = ContainerCandidate(run_id=nxp_run_id, container_id="nxp-container")

    monkeypatch.setattr(ps_cmd.build_stop, "_discover_host_cookers", dict)
    monkeypatch.setattr(ps_cmd.build_stop, "correlate_host_discoveries", lambda _discovered, **_kw: [])
    monkeypatch.setattr(ps_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(ps_cmd.build_stop, "discover_running_containers_or_warn", lambda _runtime: ([candidate], None))
    # This is what KAS_WORK_DIR bind-mounts for an nxp build: the nxp
    # bsp_root itself, not the workspace above it.
    monkeypatch.setattr(ps_cmd, "_container_mount_source", lambda _runtime, _cid: str(workspace / "nxp"))

    result = runner.invoke(app, ["ps"])

    assert result.exit_code == 0, result.output
    assert "family=nxp" in result.output
    assert "family=unknown" not in result.output
    assert "family=generic" not in result.output

    result_json = runner.invoke(app, ["ps", "--json"])
    payload = json.loads(result_json.output)
    assert len(payload) == 1
    assert payload[0]["family"] == "nxp"


def test_ps_container_row_resolves_meta_avocado_build_star_root(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container-mode row whose bind-mount source is the top-level
    WORKSPACE (a meta-avocado build's KAS_WORK_DIR, per
    steps/kas_build.py's ``_build_env`` - ``cfg.workspace``, not
    ``cfg.bsp_root``, when ``cfg.is_meta_avocado``) still resolves its real
    run record under the workspace's ``build-<stem>`` fanout root.

    This is the sibling of test_ps_container_row_resolves_nxp_family_through_real_mount_seam:
    that test proves an nxp/ti bsp_root mount is routed through the
    bare-runs-path branch; this one proves a WORKSPACE mount (meta-avocado's
    shape) is left to the workspace-scan branch instead, since routing it
    through the bare-runs-path branch too would look for
    ``<workspace>/build/runs`` - which does not exist for a meta-avocado
    build - instead of the real ``<workspace>/build-<stem>/build/runs``.
    """
    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: False)

    workspace = tmp_path / "ws"
    avocado_run_id = "20260618-180000-avocado"
    run_dir = workspace / "build-imx93-frdm" / "build" / "runs" / avocado_run_id
    run_dir.mkdir(parents=True)

    candidate = ContainerCandidate(run_id=avocado_run_id, container_id="avocado-container")

    monkeypatch.setattr(ps_cmd.build_stop, "_discover_host_cookers", dict)
    monkeypatch.setattr(ps_cmd.build_stop, "correlate_host_discoveries", lambda _discovered, **_kw: [])
    monkeypatch.setattr(ps_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(ps_cmd.build_stop, "discover_running_containers_or_warn", lambda _runtime: ([candidate], None))
    # This is what KAS_WORK_DIR bind-mounts for a meta-avocado build: the
    # workspace itself, not the build-<stem> bsp_root underneath it.
    monkeypatch.setattr(ps_cmd, "_container_mount_source", lambda _runtime, _cid: str(workspace))

    result = runner.invoke(app, ["ps"])

    assert result.exit_code == 0, result.output
    assert avocado_run_id in result.output
    assert "family=unknown" not in result.output

    result_json = runner.invoke(app, ["ps", "--json"])
    payload = json.loads(result_json.output)
    assert len(payload) == 1
    assert payload[0]["run_id"] == avocado_run_id
    assert payload[0]["family"] != "unknown"


def test_ps_json_empty_result_emits_empty_array(runner: _CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """No live builds anywhere with ``--json`` emits exactly ``[]``, not a message."""
    _no_discovery(monkeypatch)

    result = runner.invoke(app, ["ps", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_ps_json_schema_has_no_omitted_fields(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every row emits exactly the five frozen fields, all present and non-null.

    ``elapsed_seconds`` must be a JSON integer, not a float or string.
    """
    run_dir = tmp_path / "nxp" / "build" / "runs" / "20260618-120000-111"
    run_dir.mkdir(parents=True)
    root = RunRoot(bsp_root=tmp_path / "nxp", family="nxp", resolve_workspace=tmp_path, resolve_family="nxp")
    cfg = make_build_config(workspace=tmp_path / "nxp", machine="imx8mp-var-dart")
    candidate = RunCandidate(run_dir=run_dir, root=root, cfg=cfg)

    monkeypatch.setattr(ps_cmd.build_stop, "_discover_host_cookers", lambda: {"fake": frozenset()})
    monkeypatch.setattr(ps_cmd.build_stop, "correlate_host_discoveries", lambda _discovered, **_kw: [candidate])
    monkeypatch.setattr(ps_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(ps_cmd.build_stop, "discover_running_containers_or_warn", lambda _runtime: ([], None))

    result = runner.invoke(app, ["ps", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 1
    row = payload[0]
    assert set(row.keys()) == {"run_id", "mode", "family", "machine", "elapsed_seconds"}
    assert row["run_id"] == "20260618-120000-111"
    assert row["mode"] == "host"
    assert row["family"] == "nxp"
    assert row["machine"] == "imx8mp-var-dart"
    assert isinstance(row["elapsed_seconds"], int)
    assert not isinstance(row["elapsed_seconds"], bool)
    for value in row.values():
        assert value is not None


def test_ps_json_container_row_unknown_placeholder_is_a_string(
    runner: _CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container row whose mount is unrecoverable emits string "unknown"
    placeholders for family and machine under ``--json``, never null."""
    candidate = ContainerCandidate(run_id="20260618-130000-222", container_id="abc123")

    monkeypatch.setattr(ps_cmd.build_stop, "_discover_host_cookers", dict)
    monkeypatch.setattr(ps_cmd.build_stop, "correlate_host_discoveries", lambda _discovered, **_kw: [])
    monkeypatch.setattr(ps_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(ps_cmd.build_stop, "discover_running_containers_or_warn", lambda _runtime: ([candidate], None))
    monkeypatch.setattr(ps_cmd, "_container_mount_source", lambda _runtime, _cid: None)

    result = runner.invoke(app, ["ps", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 1
    row = payload[0]
    assert row["family"] == "unknown"
    assert row["machine"] == "unknown"
    assert isinstance(row["family"], str)
    assert isinstance(row["machine"], str)


def test_ps_end_to_end_fixture_scenarios(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end `bakar ps` coverage across 0, 1, and 2+ live-build fixtures,
    asserting both the human-readable table and ``--json`` output for each.

    The 2+ fixture uses distinct run ids for a host-mode and two host-mode
    plus one container-mode build and asserts each row's own content
    (mode/family/machine), not just row count, so a bug that assigned one
    row's run id to another row would be caught. A separate fixture with two
    running containers sharing one run-id label (group 12's within-source
    dedup) confirms `bakar ps` still reports exactly one row for it, in both
    output modes.
    """

    # Captured before `_no_discovery` stubs it, so the "1 build" section
    # below can restore the real function - `monkeypatch.setattr` calls
    # accumulate across one test rather than reverting mid-test, so without
    # this restoration the "0 builds" stub stays active for every later
    # section too.
    real_correlate_host_discoveries = ps_cmd.build_stop.correlate_host_discoveries

    # --- 0 builds ----------------------------------------------------
    _no_discovery(monkeypatch)

    result = runner.invoke(app, ["ps"])
    assert result.exit_code == 0, result.output
    assert "no bakar builds running" in result.output

    result_json = runner.invoke(app, ["ps", "--json"])
    assert result_json.exit_code == 0, result_json.output
    assert json.loads(result_json.output) == []

    # --- 1 build (host mode) ------------------------------------------
    monkeypatch.setattr(ps_cmd.build_stop, "correlate_host_discoveries", real_correlate_host_discoveries)
    # Only `_discover_host_cookers` is mocked here (the real /proc walk) -
    # `correlate_host_discoveries` itself runs for real, exercising the same
    # enumerate_workspace_runs/live_workspace_runs/resolve() seam production
    # traffic goes through, rather than being bypassed with a pre-built
    # RunCandidate the way this fixture used to be. This is what let the
    # host-mode family-resolution bug this change fixed earlier ship
    # unnoticed - every existing "integration" fixture replaced the exact
    # function that bug lived in.
    run_dir = tmp_path / "solo" / "nxp" / "build" / "runs" / "20260701-090000-solo"
    run_dir.mkdir(parents=True)
    ps_cmd.build_stop.write_launch_record(run_dir, pgid=1, mode="host")
    solo_topdir = run_dir.parent.parent  # tmp_path/solo/nxp/build

    monkeypatch.setattr("bakar.diagnostics.is_path_on_nfs", lambda _p: False)
    monkeypatch.setattr(ps_cmd.build_stop, "is_build_running", lambda _rd: (True, 1, True))
    monkeypatch.setattr(ps_cmd.build_stop, "_discover_host_cookers", lambda: {solo_topdir: frozenset({1})})
    monkeypatch.setattr(ps_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(ps_cmd.build_stop, "discover_running_containers_or_warn", lambda _runtime: ([], None))

    result = runner.invoke(app, ["ps"])
    assert result.exit_code == 0, result.output
    assert "20260701-090000-solo" in result.output
    assert "mode=host" in result.output
    assert "family=nxp" in result.output
    assert "machine=imx8mp-var-dart" in result.output

    result_json = runner.invoke(app, ["ps", "--json"])
    assert result_json.exit_code == 0, result_json.output
    payload = json.loads(result_json.output)
    assert len(payload) == 1
    assert payload[0]["run_id"] == "20260701-090000-solo"
    assert payload[0]["mode"] == "host"
    assert payload[0]["family"] == "nxp"
    assert payload[0]["machine"] == "imx8mp-var-dart"

    # --- 2+ builds, mixed host and container mode ---------------------
    host_run_dir_a = tmp_path / "mix-a" / "nxp" / "build" / "runs" / "20260701-100000-aaa"
    host_run_dir_a.mkdir(parents=True)
    host_root_a = RunRoot(
        bsp_root=tmp_path / "mix-a" / "nxp",
        family="nxp",
        resolve_workspace=tmp_path / "mix-a",
        resolve_family="nxp",
    )
    host_cfg_a = make_build_config(workspace=tmp_path / "mix-a" / "nxp", machine="imx8mp-var-dart")
    host_candidate_a = RunCandidate(run_dir=host_run_dir_a, root=host_root_a, cfg=host_cfg_a)

    host_run_dir_b = tmp_path / "mix-b" / "ti" / "build" / "runs" / "20260701-110000-bbb"
    host_run_dir_b.mkdir(parents=True)
    host_root_b = RunRoot(
        bsp_root=tmp_path / "mix-b" / "ti",
        family="ti",
        resolve_workspace=tmp_path / "mix-b",
        resolve_family="ti",
    )
    host_cfg_b = make_build_config(workspace=tmp_path / "mix-b" / "ti", machine="am62x-sk")
    host_candidate_b = RunCandidate(run_dir=host_run_dir_b, root=host_root_b, cfg=host_cfg_b)

    container_candidate = ContainerCandidate(run_id="20260701-120000-ccc", container_id="cid-mix")

    monkeypatch.setattr(ps_cmd.build_stop, "_discover_host_cookers", lambda: {"fake": frozenset()})
    monkeypatch.setattr(
        ps_cmd.build_stop,
        "correlate_host_discoveries",
        lambda _discovered, **_kw: [host_candidate_a, host_candidate_b],
    )
    monkeypatch.setattr(ps_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(
        ps_cmd.build_stop,
        "discover_running_containers_or_warn",
        lambda _runtime: ([container_candidate], None),
    )
    monkeypatch.setattr(ps_cmd, "_container_mount_source", lambda _runtime, _cid: None)

    result = runner.invoke(app, ["ps"])
    assert result.exit_code == 0, result.output
    # Rich may wrap a long row across terminal lines under the test runner's
    # narrow default width, so row boundaries are located by "mode=" markers
    # in the un-wrapped output rather than by splitting on newlines.
    flat_output = " ".join(result.output.split())
    assert flat_output.count("mode=") == 3

    def _row_for(run_id: str) -> str:
        idx = flat_output.index(run_id)
        next_idx = flat_output.find(run_id, idx + 1)
        assert next_idx == -1, f"run id {run_id} appears more than once in output"
        end = flat_output.find(" 202", idx + len(run_id))
        return flat_output[idx : end if end != -1 else len(flat_output)]

    row_a = _row_for("20260701-100000-aaa")
    assert "mode=host" in row_a
    assert "family=nxp" in row_a
    assert "machine=imx8mp-var-dart" in row_a

    row_b = _row_for("20260701-110000-bbb")
    assert "mode=host" in row_b
    assert "family=ti" in row_b
    assert "machine=am62x-sk" in row_b

    row_c = _row_for("20260701-120000-ccc")
    assert "mode=container" in row_c
    assert "family=unknown" in row_c
    assert "machine=unknown" in row_c

    result_json = runner.invoke(app, ["ps", "--json"])
    assert result_json.exit_code == 0, result_json.output
    payload = json.loads(result_json.output)
    assert len(payload) == 3
    by_run_id = {row["run_id"]: row for row in payload}
    assert set(by_run_id) == {"20260701-100000-aaa", "20260701-110000-bbb", "20260701-120000-ccc"}
    assert by_run_id["20260701-100000-aaa"]["mode"] == "host"
    assert by_run_id["20260701-100000-aaa"]["family"] == "nxp"
    assert by_run_id["20260701-100000-aaa"]["machine"] == "imx8mp-var-dart"
    assert by_run_id["20260701-110000-bbb"]["mode"] == "host"
    assert by_run_id["20260701-110000-bbb"]["family"] == "ti"
    assert by_run_id["20260701-110000-bbb"]["machine"] == "am62x-sk"
    assert by_run_id["20260701-120000-ccc"]["mode"] == "container"
    assert by_run_id["20260701-120000-ccc"]["family"] == "unknown"
    assert by_run_id["20260701-120000-ccc"]["machine"] == "unknown"

    # --- two containers sharing one run-id label (group 12 dedup) -----
    main_container = ContainerCandidate(run_id="20260701-130000-dedup", container_id="cid-main")
    aux_container = ContainerCandidate(run_id="20260701-130000-dedup", container_id="cid-aux")

    monkeypatch.setattr(ps_cmd.build_stop, "_discover_host_cookers", dict)
    monkeypatch.setattr(ps_cmd.build_stop, "correlate_host_discoveries", lambda _discovered, **_kw: [])
    monkeypatch.setattr(ps_cmd.build_stop, "detect_runtime", lambda: "docker")
    monkeypatch.setattr(
        ps_cmd.build_stop,
        "discover_running_containers_or_warn",
        lambda _runtime: ([main_container, aux_container], None),
    )
    monkeypatch.setattr(ps_cmd, "_container_mount_source", lambda _runtime, _cid: None)

    result = runner.invoke(app, ["ps"])
    assert result.exit_code == 0, result.output
    flat_output = " ".join(result.output.split())
    assert flat_output.count("mode=") == 1
    assert "20260701-130000-dedup" in flat_output
    assert "mode=container" in flat_output

    result_json = runner.invoke(app, ["ps", "--json"])
    assert result_json.exit_code == 0, result_json.output
    payload = json.loads(result_json.output)
    assert len(payload) == 1
    assert payload[0]["run_id"] == "20260701-130000-dedup"
    assert payload[0]["mode"] == "container"


def test_ps_never_modifies_discovered_build_state(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`bakar ps` itself is read-only: running it twice calls every
    discovery/runtime seam with identical arguments and touches neither the
    run directories those seams report nor any destructive `build_stop`
    function.

    With one host-mode and one container-mode build present: every
    discovery/runtime call `bakar ps` makes is wrapped in a call-recording
    mock, and both invocations produce identical call args/counts - proving
    `bakar ps`'s own code path queries each seam the same way both times and
    never passes different arguments a second call (which would indicate it
    is reacting to, rather than merely observing, some state). This does NOT
    independently prove the mocked functions (`is_build_running`, the
    container-runtime `inspect` call, etc.) are themselves read-only in their
    real implementations - that is covered by their own dedicated tests in
    `tests/test_build_stop.py`. No destructive `build_stop` function
    (``stop_run``, ``stop_build``, ``escalate_process_tree``, ``_kill_pid``,
    ``_killpg``, ``remove_pid``, ``_stop_container``, ``_escalate_container``)
    is ever called; and both run directories' file contents and mtimes are
    byte-identical before and after both invocations.
    """
    host_run_id = "20260701-140000-host"
    host_run_dir = tmp_path / "host-ws" / "nxp" / "build" / "runs" / host_run_id
    host_run_dir.mkdir(parents=True)
    host_marker = host_run_dir / "launch.json"
    host_marker.write_text('{"mode": "host"}')

    host_root = RunRoot(
        bsp_root=tmp_path / "host-ws" / "nxp",
        family="nxp",
        resolve_workspace=tmp_path / "host-ws",
        resolve_family="nxp",
    )
    host_cfg = make_build_config(workspace=tmp_path / "host-ws" / "nxp", machine="imx8mp-var-dart")
    host_candidate = RunCandidate(run_dir=host_run_dir, root=host_root, cfg=host_cfg)

    container_run_id = "20260701-150000-container"
    container_run_dir = tmp_path / "container-mount" / "build" / "runs" / container_run_id
    container_run_dir.mkdir(parents=True)
    container_marker = container_run_dir / "launch.json"
    container_marker.write_text('{"mode": "container"}')

    container_root = RunRoot(
        bsp_root=tmp_path / "container-mount",
        family="generic",
        resolve_workspace=tmp_path / "container-mount",
        resolve_family="bbsetup",
    )
    container_cfg = make_build_config(workspace=tmp_path / "container-mount", machine="am62x-sk")
    container_scan_candidate = RunCandidate(run_dir=container_run_dir, root=container_root, cfg=container_cfg)
    container_candidate = ContainerCandidate(run_id=container_run_id, container_id="cid-readonly")

    discover_hosts_mock = MagicMock(side_effect=lambda: {"fake": frozenset()})
    correlate_mock = MagicMock(side_effect=lambda _discovered, **_kw: [host_candidate])
    detect_runtime_mock = MagicMock(side_effect=lambda: "docker")
    discover_containers_mock = MagicMock(side_effect=lambda _runtime: ([container_candidate], None))
    mount_source_mock = MagicMock(side_effect=lambda _runtime, _cid: str(tmp_path / "container-mount"))
    enumerate_runs_mock = MagicMock(
        side_effect=lambda _path, **_kw: ps_cmd.build_stop.RunScan(candidates=[container_scan_candidate], skipped=[])
    )

    monkeypatch.setattr(ps_cmd.build_stop, "_discover_host_cookers", discover_hosts_mock)
    monkeypatch.setattr(ps_cmd.build_stop, "correlate_host_discoveries", correlate_mock)
    monkeypatch.setattr(ps_cmd.build_stop, "detect_runtime", detect_runtime_mock)
    monkeypatch.setattr(ps_cmd.build_stop, "discover_running_containers_or_warn", discover_containers_mock)
    monkeypatch.setattr(ps_cmd, "_container_mount_source", mount_source_mock)
    monkeypatch.setattr(ps_cmd.build_stop, "enumerate_workspace_runs", enumerate_runs_mock)

    def _forbid(name: str):
        def _raise(*_args: object, **_kwargs: object) -> None:
            raise AssertionError(f"bakar ps must never call build_stop.{name}")

        return _raise

    for destructive_name in (
        "stop_run",
        "stop_build",
        "escalate_process_tree",
        "_kill_pid",
        "_killpg",
        "remove_pid",
        "_stop_container",
        "_escalate_container",
    ):
        monkeypatch.setattr(ps_cmd.build_stop, destructive_name, _forbid(destructive_name))

    def _snapshot() -> dict[str, tuple[str, float]]:
        return {
            "host": (host_marker.read_text(), host_marker.stat().st_mtime),
            "container": (container_marker.read_text(), container_marker.stat().st_mtime),
        }

    before = _snapshot()

    result_one = runner.invoke(app, ["ps"])
    assert result_one.exit_code == 0, result_one.output
    assert host_run_id in result_one.output
    assert container_run_id in result_one.output
    assert _snapshot() == before

    result_two = runner.invoke(app, ["ps"])
    assert result_two.exit_code == 0, result_two.output
    assert host_run_id in result_two.output
    assert container_run_id in result_two.output
    assert _snapshot() == before

    # Every discovery/runtime call is read-only, and both invocations
    # queried it identically - proving nothing mutated state between them.
    assert discover_hosts_mock.call_count == 2
    assert correlate_mock.call_count == 2
    assert detect_runtime_mock.call_count == 2
    assert discover_containers_mock.call_count == 2
    assert mount_source_mock.call_count == 2
    assert enumerate_runs_mock.call_count == 2
    assert discover_containers_mock.call_args_list[0] == discover_containers_mock.call_args_list[1]
    assert mount_source_mock.call_args_list[0] == mount_source_mock.call_args_list[1]
