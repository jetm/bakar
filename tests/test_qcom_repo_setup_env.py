"""Unit tests for the qcom sync + setup-env steps.

Mirrors ``tests/test_repo_setup_env.py`` structure and rigor for the
Qualcomm QLI path: ``repo.init_and_sync`` under a ``qcom`` subdir with
no ``--config-name`` flag, and ``qcom_setup_env.run`` sourcing the
repo-sync-produced ``setup-environment`` script with the QLI env set.

The bare ``subprocess.run`` calls are mocked at the module-qualified
path so the surrounding logic executes hermetically under ``tmp_path``.
Assertions inspect recorded argv tokens, the dispatched env dict, raised
exceptions, and on-disk side effects - never call counts alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from bakar.config import (
    DEFAULT_QCOM_MANIFEST,
    DEFAULT_QCOM_REPO_BRANCH,
    DEFAULT_QCOM_REPO_URL,
    BuildConfig,
)
from bakar.steps import qcom_setup_env as qcom_setup_env_step
from bakar.steps import repo as repo_step

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
    )


def _nxp_cfg(workspace: Path) -> BuildConfig:
    """Minimal NXP BuildConfig - used for the --config-name regression guard."""
    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        repo_url="https://example.invalid/variscite-bsp.git",
        repo_branch="scarthgap",
        kas_container_image="jetm/kas-build-env:latest",
    )


def _fake_completed(returncode: int = 0) -> MagicMock:
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = ""
    cp.stderr = ""
    return cp


class _Recorder:
    """Records the argv (and kwargs) of every ``subprocess.run`` call."""

    def __init__(self, side_effect: object = None, returncode: int = 0) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self._side_effect = side_effect
        self._returncode = returncode

    def __call__(self, *args: object, **kwargs: object) -> MagicMock:
        self.calls.append((args, kwargs))
        if callable(self._side_effect):
            self._side_effect(*args, **kwargs)
        return _fake_completed(self._returncode)

    @property
    def argv_tokens(self) -> list[str]:
        flat: list[str] = []
        for args, _ in self.calls:
            if args and isinstance(args[0], list):
                flat.extend(str(tok) for tok in args[0])
        return flat


class _FakeLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def step_start(self, step: str, **fields: object) -> None:
        self.events.append(("step_start", step, fields))

    def step_ok(self, step: str, **fields: object) -> None:
        self.events.append(("step_ok", step, fields))

    def step_fail(self, step: str, reason: str, **fields: object) -> None:
        self.events.append(("step_fail", step, {"reason": reason, **fields}))


# ---------------------------------------------------------------------------
# repo.init_and_sync (qcom)
# ---------------------------------------------------------------------------


def test_qcom_repo_init_omits_config_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """qcom ``repo init`` must NOT carry ``--config-name`` (NXP/Variscite only).

    The qcom init argv is ``repo init -u <url> -b <branch> -m <manifest>``
    with the manifest and branch threaded through from cfg.
    """
    qcom = tmp_path / "qcom"
    qcom.mkdir()

    cfg = _qcom_cfg(tmp_path)
    log = _FakeLogger()
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(repo_step.subprocess, "run", recorder)

    repo_step.init_and_sync(cfg, log, force_init=True)

    argv_subcmds = [call[0][0][1] for call in recorder.calls]
    assert argv_subcmds == ["init", "sync"], f"unexpected subcommand order: {argv_subcmds!r}"
    init_argv = recorder.calls[0][0][0]
    assert "--config-name" not in init_argv, f"qcom init must not carry --config-name: {init_argv!r}"
    assert cfg.manifest in init_argv, f"manifest missing from qcom init argv: {init_argv!r}"
    assert cfg.repo_branch in init_argv, f"branch missing from qcom init argv: {init_argv!r}"
    assert cfg.repo_url in init_argv, f"repo url missing from qcom init argv: {init_argv!r}"


def test_qcom_repo_uses_qcom_subdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``init_and_sync`` must run under ``<workspace>/qcom`` for the qcom family."""
    qcom = tmp_path / "qcom"
    qcom.mkdir()
    (qcom / ".repo").mkdir()

    cfg = _qcom_cfg(tmp_path)
    log = _FakeLogger()
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(repo_step.subprocess, "run", recorder)

    repo_step.init_and_sync(cfg, log, force_init=False)

    # Only sync fires (`.repo/` exists, no force); it must run under qcom/.
    assert len(recorder.calls) == 1, f"expected sync-only, got {recorder.calls!r}"
    sync_cwd = recorder.calls[0][1]["cwd"]
    assert sync_cwd == qcom, f"expected sync cwd {qcom!r}, got {sync_cwd!r}"


def test_qcom_repo_sync_wipes_existing_build_conf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing ``qcom/build/conf/`` must be removed before sync."""
    qcom = tmp_path / "qcom"
    qcom.mkdir()
    (qcom / ".repo").mkdir()
    build_conf = qcom / "build" / "conf"
    build_conf.mkdir(parents=True)
    (build_conf / "bblayers.conf").write_text("# stale\n", encoding="utf-8")

    cfg = _qcom_cfg(tmp_path)
    log = _FakeLogger()
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(repo_step.subprocess, "run", recorder)

    repo_step.init_and_sync(cfg, log, force_init=False)

    assert not build_conf.exists(), "expected qcom build/conf/ wiped, but it survived"


def test_nxp_repo_init_still_includes_config_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: the generalization must NOT drop ``--config-name`` for NXP."""
    nxp = tmp_path / "nxp"
    nxp.mkdir()

    cfg = _nxp_cfg(tmp_path)
    log = _FakeLogger()
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(repo_step.subprocess, "run", recorder)

    repo_step.init_and_sync(cfg, log, force_init=True)

    init_argv = recorder.calls[0][0][0]
    assert "--config-name" in init_argv, f"NXP init must keep --config-name: {init_argv!r}"
    # And it must run under nxp/.
    assert recorder.calls[0][1]["cwd"] == nxp, f"expected NXP cwd {nxp!r}, got {recorder.calls[0][1]['cwd']!r}"


# ---------------------------------------------------------------------------
# qcom_setup_env.run
# ---------------------------------------------------------------------------


def test_qcom_setup_env_missing_script_raises_filenotfound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``qcom_setup_env.run`` raises ``FileNotFoundError`` when the script is absent."""
    qcom = tmp_path / "qcom"
    qcom.mkdir()
    # Intentionally no setup-environment script.

    cfg = _qcom_cfg(tmp_path)
    log = _FakeLogger()
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(qcom_setup_env_step.subprocess, "run", recorder)

    with pytest.raises(FileNotFoundError) as exc_info:
        qcom_setup_env_step.run(cfg, log)

    assert "setup-environment" in str(exc_info.value), (
        f"FileNotFoundError should reference the missing script, got: {exc_info.value!s}"
    )
    assert recorder.calls == [], f"subprocess.run should not fire when script is missing, got {recorder.calls!r}"


def test_qcom_setup_env_success_sets_qli_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success path: script present + subprocess writes bblayers.conf.

    The dispatched argv must go through bash and reference the
    ``setup-environment`` script; the env must carry MACHINE, DISTRO,
    QCOM_SELECTED_BSP=custom, and EXTRALAYERS.
    """
    qcom = tmp_path / "qcom"
    qcom.mkdir()
    script = qcom / "setup-environment"
    script.write_text("#!/bin/sh\n")

    cfg = _qcom_cfg(tmp_path)
    log = _FakeLogger()

    def _drop_bblayers(*args: object, **kwargs: object) -> None:
        cfg.bblayers_conf.parent.mkdir(parents=True, exist_ok=True)
        cfg.bblayers_conf.write_text("# generated by setup-environment\n")

    recorder = _Recorder(side_effect=_drop_bblayers, returncode=0)
    monkeypatch.setattr(qcom_setup_env_step.subprocess, "run", recorder)

    qcom_setup_env_step.run(cfg, log)

    assert len(recorder.calls) == 1, f"expected exactly one subprocess.run call, got {len(recorder.calls)}"
    argv = recorder.calls[0][0][0]
    assert argv[0] == "bash", f"expected bash invocation, got argv[0]={argv[0]!r}"
    assert any(str(script) in tok for tok in argv), f"script path missing from argv: {argv!r}"

    env = recorder.calls[0][1]["env"]
    assert env["MACHINE"] == cfg.machine, f"MACHINE not set: {env!r}"
    assert env["DISTRO"] == cfg.distro, f"DISTRO not set: {env!r}"
    assert env["QCOM_SELECTED_BSP"] == "custom", f"QCOM_SELECTED_BSP not set: {env!r}"
    assert env["EXTRALAYERS"] == "meta-qcom-qim-product-sdk meta-innodisk-iq", f"EXTRALAYERS not set: {env!r}"
    assert "HOME" in env and "PATH" in env, f"HOME/PATH missing from env: {env!r}"

    assert any(ev[0] == "step_ok" for ev in log.events), f"expected step_ok event, got: {log.events!r}"


def test_qcom_setup_env_missing_bblayers_after_success_raises_runtimeerror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess returns 0 but bblayers.conf was not produced -> RuntimeError."""
    qcom = tmp_path / "qcom"
    qcom.mkdir()
    script = qcom / "setup-environment"
    script.write_text("#!/bin/sh\n")

    cfg = _qcom_cfg(tmp_path)
    log = _FakeLogger()
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(qcom_setup_env_step.subprocess, "run", recorder)

    with pytest.raises(RuntimeError) as exc_info:
        qcom_setup_env_step.run(cfg, log)

    assert "bblayers.conf" in str(exc_info.value), (
        f"RuntimeError should reference bblayers.conf, got: {exc_info.value!s}"
    )
    assert len(recorder.calls) == 1, f"expected subprocess.run to have fired once, got {recorder.calls!r}"
