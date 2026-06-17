"""Tests for the ``bakar build --host`` flag and KAS_CONTAINER_IMAGE auto-detection.

Covers CLI parsing only: invoking ``bakar build <yaml> --host`` must
flip ``BuildConfig.host_mode`` to ``True``. Without ``--host`` and without
``KAS_CONTAINER_IMAGE`` set, ``resolve()`` auto-enables host mode. When
``KAS_CONTAINER_IMAGE`` is set, container mode is the default.

The actual kas/kas-container invocation is short-circuited via
``--dry-run`` so these tests stay at the argument-parsing layer, mirroring
the pattern in ``tests/test_cli_build_yaml.py``. An autouse fixture stubs the
doctor ``run_all`` to an empty pass list so it never blocks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import bakar.commands._app as cli_module
from bakar.cli import app
from bakar.config import BuildConfig
from bakar.config import resolve as real_resolve

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _stub_doctor_checks():
    """Doctor always runs now; stub ``run_all`` to an empty (all-pass) list so these
    tests stay host-independent - real checks BLOCK on disk-free / git config."""
    with patch("bakar.commands._helpers.run_all", return_value=[]):
        yield


def _make_generic_yaml(tmp_path: Path) -> Path:
    """Write a minimal generic kas YAML and return its path."""
    pilots = tmp_path / "pilot"
    pilots.mkdir()
    kas_yaml = pilots / "kas.yml"
    kas_yaml.write_text("machine: qemux86-64\n")
    return kas_yaml


def _capturing_resolve(captured: list[BuildConfig]):
    """Build a resolve wrapper that records the produced BuildConfig."""

    def _wrapper(**kwargs: object) -> BuildConfig:
        cfg = real_resolve(**kwargs)  # type: ignore[arg-type]
        captured.append(cfg)
        return cfg

    return _wrapper


@pytest.fixture(autouse=True)
def _reset_vendors() -> None:
    """Vendor cache leaks across tests; reset it before each run."""
    cli_module._VENDORS = None


def test_build_host_flag_sets_host_mode(tmp_path: Path) -> None:
    """``bakar build <yaml> --host`` must produce ``host_mode=True``."""
    kas_yaml = _make_generic_yaml(tmp_path)
    captured: list[BuildConfig] = []
    runner = CliRunner()

    with (
        patch("bakar.commands._app.load_vendors", return_value=[]),
        patch("bakar.commands.build.resolve", side_effect=_capturing_resolve(captured)),
    ):
        result = runner.invoke(
            app,
            ["build", str(kas_yaml), "--host", "--dry-run"],
        )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].host_mode is True


def test_build_no_host_flag_with_container_image_uses_container(tmp_path: Path, monkeypatch) -> None:
    """Without ``--host`` but with ``KAS_CONTAINER_IMAGE`` set, host_mode is False."""
    monkeypatch.setenv("KAS_CONTAINER_IMAGE", "test/kas-image:latest")
    kas_yaml = _make_generic_yaml(tmp_path)
    captured: list[BuildConfig] = []
    runner = CliRunner()

    with (
        patch("bakar.commands._app.load_vendors", return_value=[]),
        patch("bakar.commands.build.resolve", side_effect=_capturing_resolve(captured)),
    ):
        result = runner.invoke(
            app,
            ["build", str(kas_yaml), "--dry-run"],
        )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].host_mode is False


def test_build_no_host_flag_without_container_image_auto_enables_host(tmp_path: Path, monkeypatch) -> None:
    """Without ``--host`` and without ``KAS_CONTAINER_IMAGE``, host_mode auto-enables."""
    monkeypatch.delenv("KAS_CONTAINER_IMAGE", raising=False)
    kas_yaml = _make_generic_yaml(tmp_path)
    captured: list[BuildConfig] = []
    runner = CliRunner()

    with (
        patch("bakar.commands._app.load_vendors", return_value=[]),
        patch("bakar.commands.build.resolve", side_effect=_capturing_resolve(captured)),
    ):
        result = runner.invoke(
            app,
            ["build", str(kas_yaml), "--dry-run"],
        )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].host_mode is True
