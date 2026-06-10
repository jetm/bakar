"""Tests for the buildhistory parser and the ``bakar report`` section.

The parser tests build a synthetic ``build/buildhistory`` tree under
``tmp_path`` and assert ``_parse_buildhistory`` resolves image size, top
packages, package count, and dirty layers from the static files; returns
``None`` when the directory is absent; and skips a malformed
``installed-package-sizes.txt`` row without aborting. The command tests drive
``bakar report`` through the Typer ``CliRunner`` with module-qualified patches
on ``bakar.commands.report`` so the section renders only when the buildhistory
directory exists (the recap-archived testing split).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bakar.commands.report as report_module
from bakar.cli import app
from bakar.config import resolve
from bakar.report import ReportSummary, _parse_buildhistory

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

_IMAGE_INFO = "DISTRO = poky\nIMAGESIZE = 524288\nMACHINE = imx8mp-var-dart\n"
_PKG_SIZES = "8192\tKiB\tlibc6\n4096\tKiB\tbusybox\ngarbage row with no tabs\n2048\tKiB\tkernel-modules\n"
_PKG_NAMES = "libc6\nbusybox\nkernel-modules\nbase-files\n"
_METADATA_REVS = (
    "/work/layers/poky/meta abcdef0123 master\n/work/layers/meta-openembedded 4567890abc master -- modified\n"
)


def _write_buildhistory(bsp_root: Path) -> None:
    """Create a full synthetic buildhistory tree under ``bsp_root``."""
    bh = bsp_root / "build" / "buildhistory"
    image_dir = bh / "images" / "imx8mp-var-dart" / "glibc" / "core-image-minimal"
    image_dir.mkdir(parents=True)
    (image_dir / "image-info.txt").write_text(_IMAGE_INFO)
    (image_dir / "installed-package-sizes.txt").write_text(_PKG_SIZES)
    (image_dir / "installed-package-names.txt").write_text(_PKG_NAMES)
    (bh / "metadata-revs").write_text(_METADATA_REVS)


def test_parse_full_tree_resolves_all_fields(tmp_path: Path) -> None:
    """A complete buildhistory tree yields image size, top packages, count, dirty."""
    cfg = resolve(workspace=tmp_path, bsp_family="bbsetup")
    _write_buildhistory(cfg.bsp_root)

    result = _parse_buildhistory(cfg)

    assert result is not None
    assert result["buildhistory_imagesize_kib"] == 524288
    assert result["pkg_count"] == 4
    # The malformed row is skipped; the three valid rows survive in order.
    assert result["top_packages"] == [("libc6", 8192), ("busybox", 4096), ("kernel-modules", 2048)]
    assert "meta-openembedded" in result["layers_dirty"]
    assert "poky/meta" not in result["layers_dirty"]


def test_parse_absent_dir_returns_none(tmp_path: Path) -> None:
    """No buildhistory directory yields ``None`` (no section, no error)."""
    cfg = resolve(workspace=tmp_path, bsp_family="bbsetup")

    assert _parse_buildhistory(cfg) is None


def test_parse_malformed_size_line_skipped(tmp_path: Path) -> None:
    """A malformed installed-package-sizes line is skipped, parse does not abort."""
    cfg = resolve(workspace=tmp_path, bsp_family="bbsetup")
    bh = cfg.bsp_root / "build" / "buildhistory"
    image_dir = bh / "images" / "mach" / "glibc" / "img"
    image_dir.mkdir(parents=True)
    (image_dir / "image-info.txt").write_text(_IMAGE_INFO)
    (image_dir / "installed-package-sizes.txt").write_text("not-a-number\tKiB\tbad\n1000\tKiB\tgood\n")
    (image_dir / "installed-package-names.txt").write_text("good\n")

    result = _parse_buildhistory(cfg)

    assert result is not None
    assert result["top_packages"] == [("good", 1000)]


def test_parse_gate_on_metadata_revs_only(tmp_path: Path) -> None:
    """A buildhistory dir with only metadata-revs (no images/) still parses."""
    cfg = resolve(workspace=tmp_path, bsp_family="bbsetup")
    bh = cfg.bsp_root / "build" / "buildhistory"
    bh.mkdir(parents=True)
    (bh / "metadata-revs").write_text(_METADATA_REVS)

    result = _parse_buildhistory(cfg)

    assert result is not None
    assert result["buildhistory_imagesize_kib"] is None
    assert "meta-openembedded" in result["layers_dirty"]


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def nxp_workspace(tmp_path: Path) -> Path:
    """A workspace with an ``nxp/`` subdir so workspace detection picks nxp."""
    (tmp_path / "nxp").mkdir()
    return tmp_path


def _summary(has_buildhistory: bool = False) -> ReportSummary:
    return ReportSummary(
        run_id="20260527-100000",
        status="success",
        duration_s=1845.0,
        deploy_dir="/work/build/tmp/deploy/images/imx8mp-var-dart",
        image_size=123456,
        layers=[],
        build_revision=None,
        buildhistory_imagesize_kib=524288,
        top_packages=[("libc6", 8192), ("busybox", 4096)],
        pkg_count=4,
        layers_dirty=["meta-openembedded"],
        has_buildhistory=has_buildhistory,
    )


def test_report_shows_buildhistory_when_dir_exists(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The buildhistory section renders when the dir exists under bsp_root."""
    run_dir = nxp_workspace / "nxp" / "build" / "runs" / "20260527-100000"
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: _summary(has_buildhistory=True))

    result = runner.invoke(app, ["report", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output
    assert "buildhistory" in result.output
    assert "image size: 524288 KiB" in result.output
    assert "packages: 4" in result.output
    assert "meta-openembedded" in result.output


def test_report_no_buildhistory_section_when_dir_absent(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No buildhistory dir means no section and exit 0."""
    run_dir = nxp_workspace / "nxp" / "build" / "runs" / "20260527-100000"
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: _summary())

    result = runner.invoke(app, ["report", "--workspace", str(nxp_workspace)])
    assert result.exit_code == 0, result.output
    assert "buildhistory" not in result.output
