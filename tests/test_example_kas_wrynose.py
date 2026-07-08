"""Tests for examples/kas-qemux86-64-wrynose.yml.

Validates the YAML structure and expected fields without running a live build.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

import bakar.commands._app as cli_module
from bakar.cli import app

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _stub_doctor_checks():
    """Doctor always runs now; stub ``run_all`` to an empty (all-pass) list so these
    tests stay host-independent - real checks BLOCK on disk-free / git config."""
    with patch("bakar.commands._helpers.run_all", return_value=[]):
        yield


REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_YAML = REPO_ROOT / "examples" / "kas-qemux86-64-wrynose.yml"

_REQUIRED_REPOS = ("bitbake", "openembedded-core", "meta-openembedded")

_OE_CORE_URL = "https://git.openembedded.org/openembedded-core"
_META_OE_URL = "https://git.openembedded.org/meta-openembedded"
_BITBAKE_URL = "https://git.openembedded.org/bitbake"


@pytest.fixture(autouse=True)
def _reset_vendors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "_VENDORS", None)


@pytest.fixture
def kas_doc() -> dict:
    assert EXAMPLE_YAML.is_file(), f"example YAML missing: {EXAMPLE_YAML}"
    return yaml.safe_load(EXAMPLE_YAML.read_text(encoding="utf-8"))


def test_example_yaml_parses(kas_doc: dict) -> None:
    assert isinstance(kas_doc, dict)


def test_example_yaml_has_kas_header(kas_doc: dict) -> None:
    assert kas_doc.get("header", {}).get("version") == 21


def test_example_yaml_targets_qemux86_64(kas_doc: dict) -> None:
    assert kas_doc.get("machine") == "qemux86-64"


def test_example_yaml_targets_core_image_minimal(kas_doc: dict) -> None:
    assert kas_doc.get("target") == "core-image-minimal"


def test_example_yaml_has_required_repos(kas_doc: dict) -> None:
    repos = kas_doc.get("repos") or {}
    for name in _REQUIRED_REPOS:
        assert name in repos, f"missing repo: {name!r}"


def test_example_yaml_bitbake_uses_git_url(kas_doc: dict) -> None:
    repo = kas_doc["repos"]["bitbake"]
    assert repo.get("url") == _BITBAKE_URL
    assert repo.get("branch") == "2.18"


def test_example_yaml_bitbake_has_empty_layers(kas_doc: dict) -> None:
    """bitbake must not be added to bblayers.conf; layers: {} achieves this."""
    repo = kas_doc["repos"]["bitbake"]
    layers = repo.get("layers")
    assert layers == {} or layers is None or layers == [], f"bitbake layers must be empty (got {layers!r})"


def test_example_yaml_oe_core_uses_git_url(kas_doc: dict) -> None:
    repo = kas_doc["repos"]["openembedded-core"]
    assert repo.get("url") == _OE_CORE_URL
    assert repo.get("branch") == "wrynose"


def test_example_yaml_meta_oe_uses_git_url(kas_doc: dict) -> None:
    repo = kas_doc["repos"]["meta-openembedded"]
    assert repo.get("url") == _META_OE_URL
    assert repo.get("branch") == "wrynose"


def test_example_yaml_oe_core_includes_meta_layer(kas_doc: dict) -> None:
    layers = kas_doc["repos"]["openembedded-core"].get("layers") or {}
    assert "meta" in layers


def test_example_yaml_meta_oe_includes_core_layers(kas_doc: dict) -> None:
    layers = kas_doc["repos"]["meta-openembedded"].get("layers") or {}
    assert "meta-oe" in layers


def test_example_yaml_dry_run_succeeds(tmp_path: Path, monkeypatch) -> None:
    """Dry-run bakar build with the wrynose example YAML.

    Applies the generic tuning overlay and exits before invoking kas.
    No network access is performed in dry-run mode.
    """
    monkeypatch.setenv("KAS_CONTAINER_IMAGE", "ghcr.io/siemens/kas/kas:5.2")
    runner = CliRunner()
    with patch("bakar.commands._app.load_vendors", return_value=[]):
        result = runner.invoke(
            app,
            ["build", str(EXAMPLE_YAML), "--dry-run"],
        )
    assert result.exit_code == 0, result.output
