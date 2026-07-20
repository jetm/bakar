"""Tests for the QLI ``bitbake`` build step and its dispatch wiring.

``qcom_build.run`` sources the QLI ``setup-environment`` and runs ``bitbake``
in one bash subshell rooted at ``<ws>/qcom`` (the exported env does not survive
across processes). The ``subprocess.Popen`` is mocked at the module-qualified
path; assertions inspect the recorded bash command string, cwd, and env.

The dispatch tests drive ``_run_manifest_build`` directly with every
collaborator stubbed and assert that the qcom branch calls ``step_qcom_build``
and skips the kas path, while the nxp branch still runs the kas path.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from bakar.commands.build import _BuildCtx, _run_manifest_build
from bakar.config import (
    DEFAULT_QCOM_MANIFEST,
    DEFAULT_QCOM_REPO_BRANCH,
    DEFAULT_QCOM_REPO_URL,
    BuildConfig,
)
from bakar.steps import qcom_build as qcom_build_step

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _qcom_cfg(workspace: Path) -> BuildConfig:
    """Minimal qcom BuildConfig pointing at a tmp_path workspace."""
    return BuildConfig(
        workspace=workspace,
        bsp_family="qcom",
        machine="exmp-q911",
        distro="qcom-wayland",
        image="qcom-multimedia-image",
        manifest=DEFAULT_QCOM_MANIFEST,
        repo_url=DEFAULT_QCOM_REPO_URL,
        repo_branch=DEFAULT_QCOM_REPO_BRANCH,
        kas_container_image="jetm/kas-build-env:latest",
        # Scope off by default in these tests so wrap_build_command is a no-op
        # (no systemd-run probe, argv stays ``["bash", "-c", ...]``); the scoped
        # path is covered by test_qcom_build_wraps_in_systemd_scope.
        scope=False,
    )


class _FakePopen:
    """Stand-in for ``subprocess.Popen`` with an iterable ``stdout`` and ``wait``."""

    def __init__(self, argv: list[str], returncode: int, output_lines: list[str], **kwargs: object) -> None:
        self.args = argv
        self.kwargs = kwargs
        self._returncode = returncode
        self.stdout = iter(output_lines)

    def wait(self) -> int:
        return self._returncode


class _PopenRecorder:
    """Records the argv and kwargs of every ``subprocess.Popen`` call."""

    def __init__(self, returncode: int = 0, output_lines: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self._rc = returncode
        self._lines = list(output_lines)

    def __call__(self, argv: list[str], **kwargs: object) -> _FakePopen:
        self.calls.append((argv, kwargs))
        return _FakePopen(argv, self._rc, self._lines, **kwargs)

    @property
    def bash_command(self) -> str:
        """The command string from the recorded ``["bash", "-c", <cmd>]`` argv."""
        return str(self.calls[0][0][2])

    @property
    def cwd(self) -> object:
        return self.calls[0][1]["cwd"]

    @property
    def env(self) -> dict[str, str]:
        return self.calls[0][1]["env"]  # type: ignore[return-value]


class _FakeLogger:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.events: list[tuple[str, str, dict]] = []

    def step_start(self, step: str, **fields: object) -> None:
        self.events.append(("step_start", step, fields))

    def step_ok(self, step: str, **fields: object) -> None:
        self.events.append(("step_ok", step, fields))

    def step_fail(self, step: str, reason: str, **fields: object) -> None:
        self.events.append(("step_fail", step, {"reason": reason, **fields}))

    def info(self, msg: str, **fields: object) -> None:
        self.events.append(("info", msg, fields))

    def warn(self, msg: str, **fields: object) -> None:
        self.events.append(("warn", msg, fields))


def _no_buildtools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every buildtools detection source so detect_buildtools is absent."""
    from types import SimpleNamespace

    monkeypatch.delenv("OECORE_NATIVE_SYSROOT", raising=False)
    monkeypatch.delenv("BAKAR_BUILDTOOLS_DIR", raising=False)
    monkeypatch.setattr(
        "bakar.diagnostics.load_user_config",
        lambda: SimpleNamespace(buildtools_dir=None, buildtools_dirs={}),
    )


# ---------------------------------------------------------------------------
# qcom_build.run
# ---------------------------------------------------------------------------


def test_qcom_build_single_bash_invocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One bash -c invocation sources setup-environment and runs bitbake <target>."""
    _no_buildtools(monkeypatch)
    cfg = _qcom_cfg(tmp_path)
    log = _FakeLogger(tmp_path)
    recorder = _PopenRecorder(returncode=0)
    monkeypatch.setattr(qcom_build_step.subprocess, "Popen", recorder)

    rc = qcom_build_step.run(cfg, log, target="qcom-multimedia-image")

    assert rc == 0
    assert len(recorder.calls) == 1, f"expected one Popen call, got {recorder.calls!r}"
    argv = recorder.calls[0][0]
    assert argv[0] == "bash" and argv[1] == "-c", f"not a bash -c invocation: {argv!r}"
    cmd = recorder.bash_command
    assert ". ./setup-environment" in cmd, f"setup-environment not sourced: {cmd!r}"
    assert "bitbake qcom-multimedia-image" in cmd, f"bitbake target missing: {cmd!r}"
    assert recorder.cwd == tmp_path / "qcom", f"wrong cwd: {recorder.cwd!r}"
    env = recorder.env
    assert env["MACHINE"] == "exmp-q911"
    assert env["DISTRO"] == "qcom-wayland"
    assert env["QCOM_SELECTED_BSP"] == "custom"
    assert env["EXTRALAYERS"] == "meta-qcom-qim-product-sdk meta-innodisk-iq"
    assert any(ev[0] == "step_ok" for ev in log.events), f"expected step_ok, got {log.events!r}"


def test_qcom_build_emits_monitor_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The build points bitbake's eventlog at the run dir and streams to kas.log.

    ``bakar monitor``/``log``/``triage`` read ``<run>/bitbake_eventlog.json`` and
    ``<run>/kas.log``; the direct-bitbake path must produce both, matching the
    kas path, or the monitor shows an empty build for a qcom run.
    """
    _no_buildtools(monkeypatch)
    cfg = _qcom_cfg(tmp_path)
    run_dir = tmp_path / "qcom" / "build-qcom-wayland" / "runs" / "20260101-000000"
    run_dir.mkdir(parents=True)
    log = _FakeLogger(run_dir)
    recorder = _PopenRecorder(returncode=0)
    monkeypatch.setattr(qcom_build_step.subprocess, "Popen", recorder)

    qcom_build_step.run(cfg, log, target="qcom-multimedia-image")

    assert recorder.env["BB_DEFAULT_EVENTLOG"] == str(run_dir / "bitbake_eventlog.json")
    # The stream target is kas.log (bakar's conventional build-log name), not
    # bitbake.log; _stream_build opens it for writing, so it must exist after run.
    assert (run_dir / "kas.log").exists(), "stream log must be kas.log"
    assert not (run_dir / "bitbake.log").exists(), "stream must not write bitbake.log"


def test_qcom_build_sources_buildtools_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A BAKAR_BUILDTOOLS_DIR env script is sourced before setup-environment."""
    bt = tmp_path / "buildtools"
    bt.mkdir()
    env_script = bt / "environment-setup-foo"
    env_script.write_text("#!/bin/sh\n")

    monkeypatch.delenv("OECORE_NATIVE_SYSROOT", raising=False)
    monkeypatch.setenv("BAKAR_BUILDTOOLS_DIR", str(bt))

    cfg = _qcom_cfg(tmp_path)
    log = _FakeLogger(tmp_path)
    recorder = _PopenRecorder(returncode=0)
    monkeypatch.setattr(qcom_build_step.subprocess, "Popen", recorder)

    qcom_build_step.run(cfg, log, target="qcom-multimedia-image")

    cmd = recorder.bash_command
    assert str(env_script) in cmd, f"env script not sourced: {cmd!r}"
    assert cmd.index(str(env_script)) < cmd.index("setup-environment"), (
        f"buildtools env script must be sourced before setup-environment: {cmd!r}"
    )


def test_qcom_build_keep_going_adds_k(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """keep_going=True passes -k to bitbake."""
    _no_buildtools(monkeypatch)
    cfg = _qcom_cfg(tmp_path)
    log = _FakeLogger(tmp_path)
    recorder = _PopenRecorder(returncode=0)
    monkeypatch.setattr(qcom_build_step.subprocess, "Popen", recorder)

    qcom_build_step.run(cfg, log, target="qcom-multimedia-image", keep_going=True)

    assert "bitbake -k qcom-multimedia-image" in recorder.bash_command


def test_qcom_build_dry_run_skips_bitbake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dry_run=True never invokes bitbake and returns 0."""
    _no_buildtools(monkeypatch)
    cfg = _qcom_cfg(tmp_path)
    log = _FakeLogger(tmp_path)
    recorder = _PopenRecorder(returncode=0)
    monkeypatch.setattr(qcom_build_step.subprocess, "Popen", recorder)

    rc = qcom_build_step.run(cfg, log, target="qcom-multimedia-image", dry_run=True)

    assert rc == 0
    assert len(recorder.calls) == 0, f"dry-run must not invoke Popen: {recorder.calls!r}"


def test_qcom_build_returns_nonzero_rc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero bitbake exit code is returned, not raised, and step_fail is logged."""
    _no_buildtools(monkeypatch)
    cfg = _qcom_cfg(tmp_path)
    log = _FakeLogger(tmp_path)
    recorder = _PopenRecorder(returncode=2)
    monkeypatch.setattr(qcom_build_step.subprocess, "Popen", recorder)

    rc = qcom_build_step.run(cfg, log, target="qcom-multimedia-image")

    assert rc == 2
    assert any(ev[0] == "step_fail" for ev in log.events), f"expected step_fail, got {log.events!r}"


def test_qcom_build_wraps_in_systemd_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With scoping enabled and systemd-run available, the build is wrapped in a scope."""
    _no_buildtools(monkeypatch)
    monkeypatch.setattr("bakar.build_scope.systemd_run_available", lambda: True)
    cfg = replace(_qcom_cfg(tmp_path), scope=True)
    log = _FakeLogger(tmp_path)
    recorder = _PopenRecorder(returncode=0)
    monkeypatch.setattr(qcom_build_step.subprocess, "Popen", recorder)

    qcom_build_step.run(cfg, log, target="qcom-multimedia-image")

    argv = recorder.calls[0][0]
    assert argv[0] == "systemd-run", f"build not wrapped in a systemd scope: {argv!r}"
    assert "bash" in argv and "-c" in argv, f"bash invocation missing from scoped argv: {argv!r}"
    assert any("bitbake qcom-multimedia-image" in tok for tok in argv), f"bitbake target missing: {argv!r}"


# ---------------------------------------------------------------------------
# _run_manifest_build dispatch
# ---------------------------------------------------------------------------


def _synced_state() -> MagicMock:
    """A workspace state that skips both sync and setup-env."""
    state = MagicMock()
    state.needs_repo_sync = False
    state.needs_setup_env = False
    return state


def _dispatch_cfg(tmp_path: Path) -> MagicMock:
    cfg = MagicMock()
    cfg.machine = "exmp-q911"
    cfg.image = "qcom-multimedia-image"
    cfg.bsp_root = tmp_path
    return cfg


def _dispatch_ctx(family: str) -> _BuildCtx:
    return _BuildCtx(
        overlay_source=MagicMock(name="overlay_source"),
        extra_overlays=[],
        bsp=MagicMock(name="bsp"),
        family=family,
        effective_show_layers=False,
        dry_run=False,
        keep_going=False,
        skip_sync=False,
    )


def _run_dispatch(family: str, tmp_path: Path) -> MagicMock:
    """Drive ``_run_manifest_build`` for a family with every collaborator stubbed."""
    parent = MagicMock()
    parent.run_build.return_value = 0
    parent.qcom_run.return_value = 0

    cfg = _dispatch_cfg(tmp_path)
    log = MagicMock()

    with (
        patch("bakar.commands.build._run_doctor_gate", parent.run_doctor_gate),
        patch("bakar.commands.build.detect", return_value=_synced_state()),
        patch("bakar.commands.build.step_override.apply", parent.step_override_apply),
        patch("bakar.commands.build.step_kas.regenerate_yaml", parent.regenerate_yaml),
        patch("bakar.commands.build.step_kas.run_build", parent.run_build),
        patch("bakar.commands.build.step_qcom_build.run", parent.qcom_run),
        patch("bakar.commands.build._tuning_extra_overlays", return_value=[]),
        patch("bakar.commands.build._print_layer_hashes", parent.print_layer_hashes),
        patch("bakar.commands.build.console", parent.console),
    ):
        _run_manifest_build(cfg, log, _dispatch_ctx(family))

    return parent


def test_manifest_qcom_branch_calls_qcom_build_and_skips_kas(tmp_path: Path) -> None:
    """The qcom family runs step_qcom_build and skips override/regen/run_build."""
    parent = _run_dispatch("qcom", tmp_path)

    parent.qcom_run.assert_called_once()
    parent.step_override_apply.assert_not_called()
    parent.regenerate_yaml.assert_not_called()
    parent.run_build.assert_not_called()


def test_manifest_nxp_branch_still_runs_kas(tmp_path: Path) -> None:
    """The nxp family still runs the kas path and never calls step_qcom_build."""
    parent = _run_dispatch("nxp", tmp_path)

    parent.run_build.assert_called_once()
    parent.step_override_apply.assert_called_once()
    parent.regenerate_yaml.assert_called_once()
    parent.qcom_run.assert_not_called()
