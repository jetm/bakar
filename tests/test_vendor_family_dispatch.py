"""A vendors.toml family with no manifest model is refused at dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from bakar.bsp_model import detect_bsp_family
from bakar.cli import app

pytestmark = pytest.mark.unit


def _write_vendors(home: Path, family: str) -> None:
    cfg = home / ".config" / "bakar"
    cfg.mkdir(parents=True)
    (cfg / "vendors.toml").write_text(
        f'[[vendors]]\nname = "acme"\nfamily = "{family}"\nmanifest_regex = "^acme-.*\\\\.xml$"\n'
    )


@pytest.mark.parametrize("family", ["generic", "bbsetup"])
def test_modelless_family_exits_2_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, family: str
) -> None:
    _write_vendors(tmp_path, family)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("BAKAR_MANIFEST", raising=False)
    result = CliRunner().invoke(app, ["build", "-f", "acme-1.0.xml"])
    assert result.exit_code == 2
    assert "acme" in result.output
    assert family in result.output
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, ValueError)


def test_vendor_with_manifest_family_not_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_vendors(tmp_path, "nxp")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert detect_bsp_family(Path("acme-1.0.xml")) == "nxp"
