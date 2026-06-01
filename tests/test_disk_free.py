"""Unit tests for bakar.diagnostics.check_disk_free and the threshold config field."""

from __future__ import annotations

import os
import re
import textwrap
from collections import namedtuple
from typing import TYPE_CHECKING

import pytest

from bakar import diagnostics
from bakar.config import BuildConfig
from bakar.diagnostics import Severity, Status, check_disk_free
from bakar.user_config import load_user_config

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

_Usage = namedtuple("_Usage", "total used free")
_GiB = 1024**3

# Bound at import time so it survives monkeypatching of os.stat (diagnostics.os
# is the same module object as os, so patching one patches both).
_REAL_STAT = os.stat


def _stat_with_dev(dev: int) -> os.stat_result:
    """A stat_result mirroring this test file but with st_dev forced to ``dev``."""
    st = _REAL_STAT(__file__)
    return os.stat_result((*tuple(st)[:2], dev, *tuple(st)[3:]))


def _build_cfg(workspace: Path, **overrides: object) -> BuildConfig:
    """Minimal NXP BuildConfig rooted at ``workspace`` for disk-free tests."""
    base: dict[str, object] = {
        "workspace": workspace,
        "bsp_family": "nxp",
        "machine": "imx8mp-var-dart",
        "distro": "fsl-imx-xwayland",
        "image": "core-image-minimal",
        "manifest": "imx-6.6.52-2.2.2.xml",
        "repo_url": "https://example.com/bsp.git",
        "repo_branch": "main",
        "container_image": "jetm/kas-build-env:latest",
    }
    base.update(overrides)
    return BuildConfig(**base)


def _patch_disk(monkeypatch: pytest.MonkeyPatch, free_gb: float) -> None:
    """Make every disk_usage call report ``free_gb`` free."""
    monkeypatch.setattr(
        diagnostics.shutil,
        "disk_usage",
        lambda _path: _Usage(total=1000 * _GiB, used=0, free=int(free_gb * _GiB)),
    )


def _patch_stat_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each distinct path its own st_dev so no dedup occurs."""
    devs: dict[str, int] = {}

    def fake_stat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        key = str(path)
        devs.setdefault(key, len(devs) + 1)
        return _stat_with_dev(devs[key])

    monkeypatch.setattr(diagnostics.os, "stat", fake_stat)


@pytest.mark.unit
def test_cfg_sstate_sourced_when_env_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cfg.sstate_dir is measured even when the SSTATE_DIR env var is unset."""
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    monkeypatch.delenv("DL_DIR", raising=False)
    sstate = tmp_path / "sstate"
    sstate.mkdir()

    cfg = _build_cfg(tmp_path, sstate_dir=str(sstate))
    _patch_stat_distinct(monkeypatch)
    _patch_disk(monkeypatch, free_gb=10.0)  # below default 50G threshold

    result = check_disk_free(cfg)

    assert result.status is Status.FAIL
    assert str(sstate) in result.message
    assert "sstate@" in result.message


@pytest.mark.unit
def test_env_sstate_used_when_cfg_field_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SSTATE_DIR env is the fallback when cfg.sstate_dir is None."""
    env_sstate = tmp_path / "env-sstate"
    env_sstate.mkdir()
    monkeypatch.setenv("SSTATE_DIR", str(env_sstate))
    monkeypatch.delenv("DL_DIR", raising=False)

    cfg = _build_cfg(tmp_path, sstate_dir=None)
    _patch_stat_distinct(monkeypatch)
    _patch_disk(monkeypatch, free_gb=5.0)

    result = check_disk_free(cfg)

    assert result.status is Status.FAIL
    assert str(env_sstate) in result.message


@pytest.mark.unit
def test_same_device_paths_deduplicated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Workspace and sstate_dir on one device produce a single measurement, not two."""
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    monkeypatch.delenv("DL_DIR", raising=False)
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    cfg = _build_cfg(tmp_path, sstate_dir=str(sstate))

    # Force every path onto the same st_dev so dedup must fire.
    monkeypatch.setattr(diagnostics.os, "stat", lambda *_a, **_k: _stat_with_dev(42))

    calls: list[object] = []

    def counting_usage(path):  # type: ignore[no-untyped-def]
        calls.append(path)
        return _Usage(total=1000 * _GiB, used=0, free=5 * _GiB)

    monkeypatch.setattr(diagnostics.shutil, "disk_usage", counting_usage)

    result = check_disk_free(cfg)

    assert len(calls) == 1
    # Only one FAIL entry despite two candidate paths sharing the device.
    assert result.message.count("@") == 1


@pytest.mark.unit
def test_custom_threshold_used_in_comparison(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 100.0G threshold flags a mount with 60G free that 50G default would pass."""
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    monkeypatch.delenv("DL_DIR", raising=False)
    cfg = _build_cfg(tmp_path, disk_free_threshold_gb=100.0)
    _patch_stat_distinct(monkeypatch)
    _patch_disk(monkeypatch, free_gb=60.0)

    result = check_disk_free(cfg)

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "workspace@" in result.message


@pytest.mark.unit
def test_threshold_pass_above_custom_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The custom threshold appears in the PASS message and gates correctly."""
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    monkeypatch.delenv("DL_DIR", raising=False)
    cfg = _build_cfg(tmp_path, disk_free_threshold_gb=30.0)
    _patch_stat_distinct(monkeypatch)
    _patch_disk(monkeypatch, free_gb=40.0)

    result = check_disk_free(cfg)

    assert result.status is Status.PASS
    assert "30G" in result.message


@pytest.mark.unit
def test_user_config_rejects_zero_threshold(tmp_path: Path) -> None:
    """load_user_config raises ValueError when disk_free_threshold_gb = 0."""
    toml_content = textwrap.dedent("""\
        [build]
        disk_free_threshold_gb = 0
    """)
    config_file = tmp_path / "config.toml"
    config_file.write_text(toml_content)

    with pytest.raises(ValueError, match=re.escape(str(config_file))):
        load_user_config(config_file)
