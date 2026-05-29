"""Tests for the bakar doctor --psi-calibrate flag and recommendation helper."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from bakar.commands._app import app
from bakar.commands.doctor import _psi_recommendation

pytestmark = pytest.mark.unit

runner = CliRunner()


# ---------------------------------------------------------------------------
# Pure recommendation logic
# ---------------------------------------------------------------------------


def test_psi_recommendation_headroom() -> None:
    """Peaks get ~20% headroom applied."""
    rec = _psi_recommendation({"cpu": 50.0, "io": 40.0, "memory": 30.0})
    assert rec["cpu"] == int(50.0 * 1.20)
    assert rec["io"] == int(40.0 * 1.20)
    assert rec["memory"] == int(30.0 * 1.20)


def test_psi_recommendation_memory_floor() -> None:
    """Memory floor applied even when peak is near zero."""
    rec = _psi_recommendation({"cpu": 10.0, "io": 5.0, "memory": 0.1})
    assert rec["memory"] >= 20


def test_psi_recommendation_clamped_at_upper_bound() -> None:
    """Values above 95 are clamped to 95."""
    rec = _psi_recommendation({"cpu": 99.0, "io": 85.0, "memory": 80.0})
    assert rec["cpu"] == 95
    assert rec["io"] <= 95


def test_psi_recommendation_returns_ints() -> None:
    """All returned values are plain ints."""
    rec = _psi_recommendation({"cpu": 34.2, "io": 18.7, "memory": 0.1})
    for v in rec.values():
        assert isinstance(v, int)


# ---------------------------------------------------------------------------
# CLI: unavailable PSI exits cleanly without sampling
# ---------------------------------------------------------------------------


def test_psi_calibrate_exits_zero_when_psi_unavailable(monkeypatch) -> None:
    """--psi-calibrate exits 0 and prints a message when PSI is unavailable.

    The function is imported via ``from bakar.diagnostics import _read_psi_avg10``
    so patching the local binding in doctor's namespace (not diagnostics) is required.
    """
    monkeypatch.setattr("bakar.commands.doctor._read_psi_avg10", lambda _r: None)

    result = runner.invoke(app, ["doctor", "--psi-calibrate"])

    assert result.exit_code == 0
    assert "not available" in result.output.lower() or "unreadable" in result.output.lower()
