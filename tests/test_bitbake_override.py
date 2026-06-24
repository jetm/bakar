"""Unit tests for bakar.steps.bitbake_override.

Covers the BSP-aware path resolution (NXP carries bitbake at
``sources/poky/bitbake``; TI carries it at ``sources/bitbake``), the
relative-symlink computation in ``_swap_to_symlink``, the ``status()``
state machine for both BSP families, and the ``BAKAR_BITBAKE_OVERRIDE=0``
disable knob.

Tests build minimal on-disk fixtures under ``tmp_path`` (a real bitbake
checkout layout with a ``lib/bb/__init__.py`` carrying ``__version__``),
plus a tiny standalone git repo to feed ``apply()`` via ``--repo``.
No subprocess mocking - we exercise the real git pipeline, just at small
scale.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from bakar.config import BuildConfig
from bakar.steps.bitbake_override import (
    _bsp_bitbake,
    _upstream_dir,
    apply,
    status,
)

pytestmark = pytest.mark.unit

if TYPE_CHECKING:
    from pathlib import Path


def _cfg(workspace: Path, family: str) -> BuildConfig:
    """Construct a BuildConfig for either BSP family."""
    if family == "ti":
        return BuildConfig(
            workspace=workspace,
            bsp_family="ti",
            machine="am62x-var-som",
            distro="arago",
            image="var-thin-image",
            manifest="processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
            repo_url="https://example.invalid/none.git",
            repo_branch="scarthgap",
            kas_container_image="jetm/kas-build-env:latest",
        )
    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",
        machine="imx95-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        repo_url="https://example.invalid/none.git",
        repo_branch="scarthgap",
        kas_container_image="jetm/kas-build-env:latest",
    )


def _bsp_bitbake_relpath(family: str) -> tuple[str, ...]:
    """Path components from ``bsp_root`` to the BSP-bundled bitbake dir."""
    if family == "ti":
        return ("sources", "bitbake")
    return ("sources", "poky", "bitbake")


def _expected_symlink(family: str) -> str:
    """Relative target from BSP-bundled bitbake's parent to upstream-bitbake."""
    return "../upstream-bitbake" if family == "ti" else "../../upstream-bitbake"


def _make_bsp_tree(workspace: Path, family: str, version: str) -> Path:
    """Build a minimal BSP-bundled bitbake tree with a version stamp.

    Returns the path of the BSP-bundled bitbake directory.
    """
    cfg = _cfg(workspace, family)
    bsp_bitbake = cfg.bsp_root.joinpath(*_bsp_bitbake_relpath(family))
    init_py = bsp_bitbake / "lib" / "bb" / "__init__.py"
    init_py.parent.mkdir(parents=True, exist_ok=True)
    init_py.write_text(
        f'__version__ = "{version}"\n',
        encoding="utf-8",
    )
    return bsp_bitbake


def _make_upstream_repo(repo_dir: Path, version: str, branch: str) -> Path:
    """Build a tiny git repo that mirrors the bitbake source-repo layout.

    The repo carries ``lib/bb/__init__.py`` with the requested version
    on the requested branch. Used as the ``--repo`` argument to
    ``apply()``.
    """
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo_dir)], check=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "Test"],
        check=True,
    )
    init_py = repo_dir / "lib" / "bb" / "__init__.py"
    init_py.parent.mkdir(parents=True, exist_ok=True)
    init_py.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-q", "-m", "init"],
        check=True,
    )
    return repo_dir


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("family", "expected_tail"),
    [
        ("nxp", ("sources", "poky", "bitbake")),
        ("ti", ("sources", "bitbake")),
    ],
)
def test_bsp_bitbake_path_resolution(
    tmp_path: Path,
    family: str,
    expected_tail: tuple[str, ...],
) -> None:
    cfg = _cfg(tmp_path, family)
    expected = cfg.bsp_root.joinpath(*expected_tail)
    assert cfg.bsp_bitbake_path == expected
    assert _bsp_bitbake(cfg) == expected


@pytest.mark.parametrize("family", ["nxp", "ti"])
def test_upstream_dir_lives_at_bsp_root(tmp_path: Path, family: str) -> None:
    cfg = _cfg(tmp_path, family)
    assert _upstream_dir(cfg) == cfg.bsp_root / "upstream-bitbake"


@pytest.mark.parametrize(
    ("family", "expected_tail"),
    [
        ("nxp", ("sources", "poky", "meta", "conf", "bitbake.conf")),
        ("ti", ("sources", "oe-core", "meta", "conf", "bitbake.conf")),
    ],
)
def test_bsp_bitbake_conf_resolution(
    tmp_path: Path,
    family: str,
    expected_tail: tuple[str, ...],
) -> None:
    cfg = _cfg(tmp_path, family)
    assert cfg.bsp_bitbake_conf == cfg.bsp_root.joinpath(*expected_tail)


# ---------------------------------------------------------------------------
# apply(): real git pipeline against a tiny fixture repo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("family", "expected_link"),
    [
        ("nxp", "../../upstream-bitbake"),
        ("ti", "../upstream-bitbake"),
    ],
)
def test_apply_swaps_with_correct_relative_symlink(
    tmp_path: Path,
    family: str,
    expected_link: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apply() must produce the right relative-symlink depth per BSP."""
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE", raising=False)
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE_BRANCH", raising=False)

    bsp_bitbake = _make_bsp_tree(tmp_path, family, version="2.8.0")
    repo = _make_upstream_repo(tmp_path / "upstream-source", version="2.8.1", branch="br-2.8")

    cfg = _cfg(tmp_path, family)
    result = apply(cfg, log=None, repo_path=repo)

    assert result.state == "active"
    assert bsp_bitbake.is_symlink()
    assert os.readlink(bsp_bitbake) == expected_link
    # Resolves to the per-BSP upstream-bitbake clone:
    assert bsp_bitbake.resolve() == (cfg.bsp_root / "upstream-bitbake").resolve()
    assert result.upstream_version == "2.8.1"
    assert result.bsp_version == "2.8.0"
    assert result.branch == "br-2.8"


@pytest.mark.parametrize("family", ["nxp", "ti"])
def test_apply_idempotent(
    tmp_path: Path,
    family: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second apply on an already-correct symlink reports linked-fresh."""
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE", raising=False)
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE_BRANCH", raising=False)

    _make_bsp_tree(tmp_path, family, version="2.8.0")
    repo = _make_upstream_repo(tmp_path / "upstream-source", version="2.8.1", branch="br-2.8")

    cfg = _cfg(tmp_path, family)
    apply(cfg, log=None, repo_path=repo)
    second = apply(cfg, log=None, repo_path=repo)

    assert second.state == "active"
    assert second.detail == "linked-fresh"


# ---------------------------------------------------------------------------
# status(): state machine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family", ["nxp", "ti"])
def test_status_stale_when_real_dir_present(tmp_path: Path, family: str) -> None:
    """Pre-apply (real BSP dir, no clone) returns 'stale'."""
    _make_bsp_tree(tmp_path, family, version="2.8.0")

    cfg = _cfg(tmp_path, family)
    st = status(cfg)

    assert st.state == "stale"
    assert st.bsp_version == "2.8.0"
    assert st.poky_bitbake == cfg.bsp_bitbake_path
    assert st.upstream_dir == cfg.bsp_root / "upstream-bitbake"


@pytest.mark.parametrize("family", ["nxp", "ti"])
def test_status_missing_when_pre_bootstrap(tmp_path: Path, family: str) -> None:
    """No BSP tree and no clone yet returns 'missing'."""
    cfg = _cfg(tmp_path, family)
    cfg.bsp_root.mkdir(parents=True, exist_ok=True)

    st = status(cfg)
    assert st.state == "missing"


@pytest.mark.parametrize("family", ["nxp", "ti"])
def test_status_active_after_apply(
    tmp_path: Path,
    family: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE", raising=False)
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE_BRANCH", raising=False)

    _make_bsp_tree(tmp_path, family, version="2.8.0")
    repo = _make_upstream_repo(tmp_path / "upstream-source", version="2.8.1", branch="br-2.8")

    cfg = _cfg(tmp_path, family)
    apply(cfg, log=None, repo_path=repo)

    st = status(cfg)
    assert st.state == "active"
    assert st.upstream_version == "2.8.1"
    assert st.branch == "br-2.8"
    assert st.poky_bitbake == cfg.bsp_bitbake_path


# ---------------------------------------------------------------------------
# Disable knob
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family", ["nxp", "ti"])
def test_disabled_env_short_circuits_apply(
    tmp_path: Path,
    family: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BAKAR_BITBAKE_OVERRIDE=0 must skip apply for both BSPs."""
    monkeypatch.setenv("BAKAR_BITBAKE_OVERRIDE", "0")

    bsp_bitbake = _make_bsp_tree(tmp_path, family, version="2.8.0")

    cfg = _cfg(tmp_path, family)
    result = apply(cfg, log=None)  # no repo_path - would fail if pipeline ran

    assert result.state == "disabled"
    # BSP tree untouched:
    assert bsp_bitbake.is_dir()
    assert not bsp_bitbake.is_symlink()
