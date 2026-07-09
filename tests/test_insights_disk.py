"""Tests for :mod:`bakar.insights_disk`.

Covers monotonic growth computation, `DiskFull` events surfaced separately
from the growth figure, threshold-exceeded and threshold-not-exceeded
warnings, and the no-data unavailability signal.
"""

from __future__ import annotations

import pytest

from bakar.insights_disk import NO_DATA_MESSAGE, disk_report

pytestmark = pytest.mark.unit


def _sample(time: int, used_bytes: int) -> dict:
    return {"time": time, "used_bytes": used_bytes}


@pytest.mark.unit
def test_growth_is_end_minus_start_not_peak() -> None:
    disk_samples = [
        _sample(1, 1_000),
        _sample(2, 5_000),
        _sample(3, 3_000),
    ]

    report = disk_report(disk_samples, events={})

    assert report.message is None
    assert report.growth_bytes == 2_000


@pytest.mark.unit
def test_disk_full_event_surfaced_separately_from_growth() -> None:
    disk_samples = [_sample(1, 1_000), _sample(2, 1_500)]
    events = {"disk": {"full_events": [{"time": 2, "path": "/build/tmp"}]}}

    report = disk_report(disk_samples, events=events)

    assert report.growth_bytes == 500
    assert report.full_events == [{"time": 2, "path": "/build/tmp"}]
    assert report.warning is None


@pytest.mark.unit
def test_growth_exceeding_threshold_produces_warning_naming_both_values() -> None:
    disk_samples = [_sample(1, 0), _sample(2, 10_000)]

    report = disk_report(disk_samples, events={}, threshold_bytes=5_000)

    assert report.growth_bytes == 10_000
    assert report.warning is not None
    assert "10000" in report.warning
    assert "5000" in report.warning


@pytest.mark.unit
def test_growth_at_or_under_threshold_has_no_warning() -> None:
    disk_samples = [_sample(1, 0), _sample(2, 5_000)]

    report = disk_report(disk_samples, events={}, threshold_bytes=5_000)

    assert report.growth_bytes == 5_000
    assert report.warning is None


@pytest.mark.unit
def test_no_persisted_disk_samples_yields_no_data_message_not_exception() -> None:
    report = disk_report(None, events={})

    assert report.growth_bytes is None
    assert report.message == NO_DATA_MESSAGE

    empty_report = disk_report([], events={})

    assert empty_report.growth_bytes is None
    assert empty_report.message == NO_DATA_MESSAGE
