"""Tests for :mod:`bakar.insights_pressure`.

Covers the three verdict-shaping cases: sustained I/O pressure (dominant
dimension named and storage recommended over parallelism), absent PSI
samples (explicit unavailability signal, never a fabricated 0%-pressure
summary), and all-three-categories-low (verdict states the build was not
resource-pressured).
"""

from __future__ import annotations

import pytest

from bakar.insights_pressure import LOW_PRESSURE_THRESHOLD, NO_DATA_MESSAGE, pressure_report

pytestmark = pytest.mark.unit


def _sample(cpu: float, io: float, memory: float) -> dict:
    return {"time": 0, "cpu": cpu, "io": io, "memory": memory}


@pytest.mark.unit
def test_sustained_io_pressure_names_io_and_favors_storage() -> None:
    samples = [
        _sample(cpu=5.0, io=80.0, memory=10.0),
        _sample(cpu=8.0, io=75.0, memory=12.0),
    ]

    report = pressure_report(samples)

    assert report.available is True
    assert report.time_share["io"] > report.time_share["cpu"]
    assert report.time_share["io"] > report.time_share["memory"]
    assert "I/O" in report.verdict
    # A verdict naming I/O as dominant should not instead point at CPU/parallelism.
    assert "CPU" not in report.verdict
    assert "parallel" not in report.verdict.lower()


@pytest.mark.unit
def test_no_persisted_psi_samples_yields_explicit_unavailability() -> None:
    report = pressure_report([])

    assert report.available is False
    assert report.time_share == {}
    assert report.verdict == NO_DATA_MESSAGE
    # The no-data case must never be mistaken for a 0%-pressure build.
    assert "0%" not in report.verdict
    assert "0.0%" not in report.verdict


@pytest.mark.unit
def test_missing_dimension_values_across_all_samples_also_signals_unavailable() -> None:
    samples = [{"time": 0}, {"time": 1}]

    report = pressure_report(samples)

    assert report.available is False
    assert report.verdict == NO_DATA_MESSAGE


@pytest.mark.unit
def test_all_categories_low_states_not_resource_pressured() -> None:
    below_threshold = LOW_PRESSURE_THRESHOLD - 1.0
    samples = [
        _sample(cpu=below_threshold, io=below_threshold, memory=below_threshold),
        _sample(cpu=below_threshold, io=below_threshold, memory=below_threshold),
    ]

    report = pressure_report(samples)

    assert report.available is True
    assert "not resource-pressured" in report.verdict
