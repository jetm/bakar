"""Unit tests for bakar.psi: PSI reading, recommendation, and auto-calibration."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bakar.psi import (
    apply_autocalibration,
    plan_autocalibration,
    psi_recommendation,
    read_psi_avg10,
)
from bakar.user_config import load_user_config

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# read_psi_avg10
# ---------------------------------------------------------------------------


def test_read_psi_avg10_parses_some_line() -> None:
    text = "some avg10=12.34 avg60=5.0 avg300=1.0 total=999\nfull avg10=2.0 ...\n"
    with patch("bakar.psi.Path.read_text", lambda self, **kw: text):
        assert read_psi_avg10("cpu") == 12.34


def test_read_psi_avg10_missing_file_returns_none() -> None:
    def boom(self, **kw):
        raise OSError("no PSI")

    with patch("bakar.psi.Path.read_text", boom):
        assert read_psi_avg10("io") is None


# ---------------------------------------------------------------------------
# psi_recommendation
# ---------------------------------------------------------------------------


def test_psi_recommendation_headroom_floor_clamp() -> None:
    rec = psi_recommendation({"cpu": 99.0, "io": 40.0, "memory": 0.1})
    assert rec["cpu"] == 95  # clamped
    assert rec["io"] == int(40.0 * 1.20)  # headroom
    assert rec["memory"] >= 20  # floor
    assert all(isinstance(v, int) for v in rec.values())


# ---------------------------------------------------------------------------
# plan_autocalibration (throttle-aware policy)
# ---------------------------------------------------------------------------


def test_plan_bootstrap_writes_all_unset() -> None:
    plan = plan_autocalibration(
        {"cpu": 40.0, "io": 10.0, "memory": 5.0},
        {"cpu": None, "io": None, "memory": None},
    )
    assert set(plan) == {"cpu", "io", "memory"}


def test_plan_skips_zero_peak() -> None:
    assert plan_autocalibration({"cpu": 0.0}, {"cpu": None}) == {}


def test_plan_raises_unthrottled_skips_throttled() -> None:
    # cpu: peak 45 < threshold 50, rec 54 > 50 -> raise.
    # io:  peak 60 >= threshold 55 -> throttled (peak capped by the
    #      ceiling itself) -> keep current.
    plan = plan_autocalibration({"cpu": 45.0, "io": 60.0}, {"cpu": 50.0, "io": 55.0})
    assert plan == {"cpu": 54}


def test_plan_never_lowers_threshold() -> None:
    # A cached/light build peaks far below the ceiling (rec 12 < cur 80).
    # Lowering would over-throttle the next cold build -> keep current.
    assert plan_autocalibration({"cpu": 10.0}, {"cpu": 80.0}) == {}


def test_plan_skips_when_recommendation_not_higher() -> None:
    # peak 40 -> rec 48; current already 48 and unthrottled (40 < 48) -> no change.
    assert plan_autocalibration({"cpu": 40.0}, {"cpu": 48.0}) == {}


# ---------------------------------------------------------------------------
# apply_autocalibration (writes through set_setting)
# ---------------------------------------------------------------------------


def test_apply_writes_values_and_returns_plan(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    written = apply_autocalibration(
        {"cpu": None, "io": None, "memory": None},
        {"cpu": 40.0, "io": 10.0, "memory": 5.0},
        config_path=config_file,
    )
    assert set(written) == {"cpu", "io", "memory"}

    cfg = load_user_config(config_file)
    assert cfg.pressure_max_cpu == written["cpu"]
    assert cfg.pressure_max_io == written["io"]
    assert cfg.pressure_max_memory == written["memory"]


def test_apply_noop_writes_nothing(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    written = apply_autocalibration({"cpu": None}, {"cpu": 0.0}, config_path=config_file)
    assert written == {}
    assert not config_file.exists()
