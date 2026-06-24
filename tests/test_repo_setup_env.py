"""Unit tests for ``bakar.steps.repo`` and ``bakar.steps.setup_env``.

Both step modules wrap a single ``subprocess.run`` call surrounded by
testable logic (path checks, manifest/branch argv assembly, post-run
validation). The bare ``subprocess.run`` calls themselves are mocked at
the module-qualified path so the surrounding logic executes hermetically
under ``tmp_path``.

The seam is intentionally narrow: tests assert on the recorded argv
tokens, raised exceptions, and on-disk side effects produced by the
mocks - never on call counts alone.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from bakar.config import BuildConfig
from bakar.steps import repo as repo_step
from bakar.steps import setup_env as setup_env_step

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _nxp_cfg(workspace: Path) -> BuildConfig:
    """Minimal NXP BuildConfig pointing at a tmp_path workspace."""
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
    """Stand-in for a ``subprocess.CompletedProcess``."""
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = ""
    cp.stderr = ""
    return cp


class _Recorder:
    """Records the argv of every ``subprocess.run`` call.

    Tests inspect ``.calls`` (the list of positional/keyword argv tuples)
    and ``.argv_tokens`` (a flat list of every token from every recorded
    argv) to assert on the dispatched command shape.
    """

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
    """Minimal stand-in for :class:`bakar.observability.RunLogger`.

    The step modules call ``log.step_start`` and ``log.step_ok``; the
    bodies don't care about return values, so MagicMock would suffice,
    but a tiny named class keeps test failures readable.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def step_start(self, step: str, **fields: object) -> None:
        self.events.append(("step_start", step, fields))

    def step_ok(self, step: str, **fields: object) -> None:
        self.events.append(("step_ok", step, fields))

    def step_fail(self, step: str, reason: str, **fields: object) -> None:
        self.events.append(("step_fail", step, {"reason": reason, **fields}))


# ---------------------------------------------------------------------------
# repo.init_and_sync
# ---------------------------------------------------------------------------


def test_repo_force_init_runs_init_and_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``force_init=True`` must dispatch both ``repo init`` and ``repo sync``.

    Even when ``.repo/`` already exists on disk, the force flag wins:
    ``init_and_sync`` should still send ``repo init`` first and then
    ``repo sync``. Both subcommands must land in the recorded argv.
    """
    nxp = tmp_path / "nxp"
    nxp.mkdir()
    # Pre-create .repo/ so the existing-dir branch alone would skip init -
    # force_init must override it.
    (nxp / ".repo").mkdir()

    cfg = _nxp_cfg(tmp_path)
    log = _FakeLogger()
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(repo_step.subprocess, "run", recorder)

    repo_step.init_and_sync(cfg, log, force_init=True)

    tokens = recorder.argv_tokens
    assert "repo" in tokens, f"expected `repo` in recorded argv, got {tokens!r}"
    assert "init" in tokens, f"expected `repo init` to have fired, got {tokens!r}"
    assert "sync" in tokens, f"expected `repo sync` to have fired, got {tokens!r}"
    # Two distinct calls: one init, one sync.
    argv_firsts = [call[0][0][0] for call in recorder.calls]
    argv_subcmds = [call[0][0][1] for call in recorder.calls]
    assert argv_firsts == ["repo", "repo"], f"unexpected argv[0]s: {argv_firsts!r}"
    assert argv_subcmds == ["init", "sync"], f"unexpected subcommand order: {argv_subcmds!r}"
    # Manifest and branch from cfg must thread through repo init.
    init_argv = recorder.calls[0][0][0]
    assert cfg.manifest in init_argv, f"manifest missing from init argv: {init_argv!r}"
    assert cfg.repo_branch in init_argv, f"branch missing from init argv: {init_argv!r}"


def test_repo_existing_dir_without_force_skips_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing ``.repo/`` without ``force_init`` must skip ``repo init``.

    Only ``repo sync`` should fire. The dispatched argv must contain
    ``sync`` and must NOT contain ``init``.
    """
    nxp = tmp_path / "nxp"
    nxp.mkdir()
    (nxp / ".repo").mkdir()

    cfg = _nxp_cfg(tmp_path)
    log = _FakeLogger()
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(repo_step.subprocess, "run", recorder)

    repo_step.init_and_sync(cfg, log, force_init=False)

    assert len(recorder.calls) == 1, (
        f"expected exactly one subprocess.run call (sync only), got {len(recorder.calls)}: {recorder.calls!r}"
    )
    sync_argv = recorder.calls[0][0][0]
    assert sync_argv[0] == "repo", f"expected argv[0] == 'repo', got {sync_argv!r}"
    assert "sync" in sync_argv, f"expected `sync` token, got {sync_argv!r}"
    assert "init" not in sync_argv, f"`init` must not appear when force_init=False and .repo/ exists: {sync_argv!r}"


def test_repo_missing_dir_runs_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``.repo/`` directory means init must fire even without ``force_init``.

    Confirms the ``need_init = force_init or not repo_dir.is_dir()`` path
    where the disk state alone triggers init.
    """
    nxp = tmp_path / "nxp"
    nxp.mkdir()
    # Intentionally no .repo/ subdir.

    cfg = _nxp_cfg(tmp_path)
    log = _FakeLogger()
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(repo_step.subprocess, "run", recorder)

    repo_step.init_and_sync(cfg, log, force_init=False)

    argv_subcmds = [call[0][0][1] for call in recorder.calls]
    assert argv_subcmds == ["init", "sync"], f"unexpected subcommand order: {argv_subcmds!r}"


def test_repo_sync_wipes_existing_build_conf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing ``nxp/build/conf/`` must be removed before sync.

    The wipe-on-sync invariant documented in ``init_and_sync``'s docstring
    guards against stale ``bblayers.conf`` from a previous branch
    surviving a sync that moved layer SHAs out from under it. Without this
    side effect, ``setup_env`` would not regenerate the file and the
    subsequent build would fail with confusing layer-resolution errors.
    """
    nxp = tmp_path / "nxp"
    nxp.mkdir()
    (nxp / ".repo").mkdir()
    build_conf = nxp / "build" / "conf"
    build_conf.mkdir(parents=True)
    (build_conf / "bblayers.conf").write_text("# stale\n", encoding="utf-8")

    cfg = _nxp_cfg(tmp_path)
    log = _FakeLogger()
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(repo_step.subprocess, "run", recorder)

    repo_step.init_and_sync(cfg, log, force_init=False)

    survivors = sorted(build_conf.iterdir()) if build_conf.exists() else "(absent)"
    assert not build_conf.exists(), f"expected build/conf/ wiped, but it survived: {survivors!r}"


# ---------------------------------------------------------------------------
# setup_env.run
# ---------------------------------------------------------------------------


def test_setup_env_missing_script_raises_filenotfound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``setup_env.run`` must raise ``FileNotFoundError`` when the script is absent.

    The script ``var-setup-release.sh`` is normally a repo-sync linkfile;
    if repo sync did not complete it is missing, and ``run`` should fail
    fast before reaching ``subprocess.run``.
    """
    nxp = tmp_path / "nxp"
    nxp.mkdir()
    # Intentionally no var-setup-release.sh.

    cfg = _nxp_cfg(tmp_path)
    log = _FakeLogger()
    # Patch subprocess.run anyway so an accidental call would surface
    # via the recorder's call list rather than spawning a real shell.
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(setup_env_step.subprocess, "run", recorder)

    with pytest.raises(FileNotFoundError) as exc_info:
        setup_env_step.run(cfg, log)

    # The script path should appear in the message so the operator can act on it.
    assert "var-setup-release.sh" in str(exc_info.value), (
        f"FileNotFoundError message should reference the missing script, got: {exc_info.value!s}"
    )
    # And subprocess.run must NOT have been invoked.
    assert recorder.calls == [], f"subprocess.run should not fire when script is missing, got {recorder.calls!r}"


def test_setup_env_success_when_bblayers_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful path: script present + subprocess writes bblayers.conf.

    The script's job is to drop ``build/conf/bblayers.conf`` onto disk.
    Simulate that by making the mocked ``subprocess.run`` create the
    file as a side-effect before returning 0. ``setup_env.run`` must
    complete without raising.
    """
    nxp = tmp_path / "nxp"
    nxp.mkdir()
    script = nxp / "var-setup-release.sh"
    script.write_text("#!/bin/sh\n")

    cfg = _nxp_cfg(tmp_path)
    log = _FakeLogger()

    def _drop_bblayers(*args: object, **kwargs: object) -> None:
        # Replicate the script's observable effect: create build/conf/bblayers.conf.
        cfg.bblayers_conf.parent.mkdir(parents=True, exist_ok=True)
        cfg.bblayers_conf.write_text("# generated by var-setup-release.sh\n")

    recorder = _Recorder(side_effect=_drop_bblayers, returncode=0)
    monkeypatch.setattr(setup_env_step.subprocess, "run", recorder)

    # Should not raise.
    setup_env_step.run(cfg, log)

    assert len(recorder.calls) == 1, f"expected exactly one subprocess.run call, got {len(recorder.calls)}"
    # The script invocation goes through bash; verify the dispatched argv
    # references the script path so the right thing was asked to run.
    argv = recorder.calls[0][0][0]
    assert argv[0] == "bash", f"expected bash invocation, got argv[0]={argv[0]!r}"
    assert any(str(script) in tok for tok in argv), f"script path missing from argv: {argv!r}"
    # The success path logs a step_ok.
    assert any(ev[0] == "step_ok" for ev in log.events), (
        f"expected step_ok event on success, got events: {log.events!r}"
    )


def test_setup_env_missing_bblayers_after_success_raises_runtimeerror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subprocess returns 0 but bblayers.conf was not produced.

    The check after ``subprocess.run`` is the safety net for a script
    that exits 0 without writing its expected output; ``setup_env.run``
    must raise ``RuntimeError`` so the build halts.
    """
    nxp = tmp_path / "nxp"
    nxp.mkdir()
    script = nxp / "var-setup-release.sh"
    script.write_text("#!/bin/sh\n")

    cfg = _nxp_cfg(tmp_path)
    log = _FakeLogger()
    # Recorder returns 0 but does NOT create bblayers.conf - the
    # post-condition check should fail.
    recorder = _Recorder(returncode=0)
    monkeypatch.setattr(setup_env_step.subprocess, "run", recorder)

    with pytest.raises(RuntimeError) as exc_info:
        setup_env_step.run(cfg, log)

    assert "bblayers.conf" in str(exc_info.value), (
        f"RuntimeError should reference bblayers.conf, got: {exc_info.value!s}"
    )
    # subprocess.run was invoked (we got past the script-present check).
    assert len(recorder.calls) == 1, (
        f"expected subprocess.run to have fired once before the missing-bblayers check, got {recorder.calls!r}"
    )


# Sanity: ensure subprocess module is imported in this test module so a
# real CalledProcessError type is available if a future test wants to
# simulate a non-zero return code.  Silences unused-import warnings.
_ = subprocess.CalledProcessError
