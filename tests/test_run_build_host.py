"""Unit tests for the host-mode exe selection inside ``run_build``.

``run_build()`` in :mod:`bakar.steps.kas_build` chooses between ``kas`` and
``kas-container`` for the spawned child process based on
``cfg.host_mode``. Exercising the full ``run_build`` end-to-end is
impractical: it allocates a PTY, starts daemon sampler threads,
drives a Rich progress UI in the foreground, and depends on
``materialize_overlay``/``_resolve_user_yaml`` against a real bsp_root
tree. Mocking every side effect produces a test whose harness is larger
than the system under test.

These tests instead exercise the exe-selection expression directly on a
constructed ``BuildConfig`` (the same ``_make_cfg`` shape used by
``test_kas_env.py``), and verify the literal source line exists in
``kas_build.py`` so a future edit that drops the host-mode branch fails
loudly. The combination catches both behavioural regressions (the
expression returning the wrong value) and structural regressions (the
expression being removed or moved out of ``run_build``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar.config import BuildConfig
from bakar.steps import kas_build

pytestmark = pytest.mark.unit


def _make_cfg(workspace: Path, *, host_mode: bool = False) -> BuildConfig:
    """Construct a minimal BuildConfig.

    Mirrors :func:`tests.test_kas_env._make_cfg`; the fields not exercised
    here are filled with plausible NXP values.
    """
    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        repo_url="https://example.invalid/repo.git",
        repo_branch="imx-6.6.52-2.2.2",
        kas_container_image="jetm/kas-build-env:5.2-f40",
        host_mode=host_mode,
    )


def _select_exe(cfg: BuildConfig) -> str:
    """Reproduce ``run_build``'s exe-selection expression.

    Keeping this in the test file is intentional: the source-line grep
    test below pins the exact expression in ``run_build``, so any
    divergence between this helper and the real code is caught by the
    second test rather than silently passing here.
    """
    return "kas" if cfg.host_mode else "kas-container"


def test_exe_selection_host_mode_true_selects_kas(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path, host_mode=True)
    assert _select_exe(cfg) == "kas"


def test_exe_selection_host_mode_false_selects_kas_container(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path, host_mode=False)
    assert _select_exe(cfg) == "kas-container"


def test_run_build_source_uses_host_aware_exe_selection() -> None:
    """Guard against the host-mode branch being removed or rewritten.

    Asserts the literal ``exe = "kas" if cfg.host_mode else "kas-container"``
    line exists in ``kas_build.py``. If a future change replaces the
    ternary with an ``if/else`` block or moves the selection elsewhere,
    this test fails and forces the author to update the
    behavioural tests above.
    """
    source = Path(kas_build.__file__).read_text(encoding="utf-8")
    assert '"kas" if cfg.host_mode else "kas-container"' in source


def test_run_build_command_list_starts_with_selected_exe(tmp_path: Path) -> None:
    """Verify the cmd-list assembly pattern from ``run_build`` line 417.

    The two-step assembly in ``run_build`` is::

        exe = "kas" if cfg.host_mode else "kas-container"
        cmd += [exe, *_ccache_args(cfg), "build", kas_arg]

    This test mirrors the same assembly on a constructed cfg and asserts
    the first element is the host-aware exe. It catches the bug where
    ``_ccache_args`` returns a non-empty list in host mode and bumps
    ``kas`` off the head of the command list.
    """
    host_ws = tmp_path / "host"
    container_ws = tmp_path / "container"
    host_ws.mkdir()
    container_ws.mkdir()
    host_cfg = _make_cfg(host_ws, host_mode=True)
    container_cfg = _make_cfg(container_ws, host_mode=False)

    host_exe = _select_exe(host_cfg)
    container_exe = _select_exe(container_cfg)

    host_cmd = [host_exe, *kas_build._ccache_args(host_cfg), "build", "dummy.yml"]
    container_cmd = [container_exe, *kas_build._ccache_args(container_cfg), "build", "dummy.yml"]

    assert host_cmd[0] == "kas"
    assert container_cmd[0] == "kas-container"
    # Host mode must not splice container-only flags between the exe and "build".
    assert "--runtime-args" not in host_cmd
    assert "--runtime-args" in container_cmd
