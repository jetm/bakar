"""Unit tests for ``BuildUIState.first_error`` - the first error line of a build."""

from __future__ import annotations

import pytest

from bakar.eventlog import _EventStub
from bakar.steps.build_ui import _EVT_RUNQUEUE_TASK_STARTED, BuildUIState


@pytest.mark.unit
def test_bitbake_error_after_warning_is_recorded() -> None:
    ui = BuildUIState()
    ui.process_line("WARNING: something mild")
    ui.process_line("ERROR: Nothing PROVIDES 'x'")
    assert ui.first_error == "ERROR: Nothing PROVIDES 'x'"


@pytest.mark.unit
def test_kas_form_error_is_recorded() -> None:
    ui = BuildUIState()
    ui.process_line("2026-10-19 10:00:00 - ERROR    - Failed to checkout layer")
    assert ui.first_error == "ERROR: Failed to checkout layer"


@pytest.mark.unit
def test_first_error_wins_over_later_one() -> None:
    ui = BuildUIState()
    ui.process_line("ERROR: first")
    ui.process_line("ERROR: second")
    assert ui.first_error == "ERROR: first"


@pytest.mark.unit
def test_coloured_long_line_is_stored_clean_and_bounded() -> None:
    ui = BuildUIState()
    ui.process_line("\x1b[31mERROR: " + "x" * 500 + "\x1b[0m")
    assert ui.first_error is not None
    assert "\x1b" not in ui.first_error
    assert len(ui.first_error) <= 200
    assert ui.first_error.startswith("ERROR: xxx")


@pytest.mark.unit
def test_indented_continuation_after_fatal_is_not_captured() -> None:
    ui = BuildUIState()
    ui.process_line("FATAL: sanity check failed")
    ui.process_line("    ERROR: detail under the head")
    assert ui.first_error is None


@pytest.mark.unit
def test_none_after_only_warnings_and_progress() -> None:
    ui = BuildUIState()
    ui.process_line("Parsing recipes:  47% || ETA:  0:00:28")
    ui.process_line("WARNING: just a warning")
    assert ui.first_error is None


@pytest.mark.unit
def test_recorded_in_event_driven_mode() -> None:
    ui = BuildUIState()
    stub = _EventStub()
    stub.stats = {"total": 10}
    ui.process_event(_EVT_RUNQUEUE_TASK_STARTED, stub)
    ui.process_line("ERROR: after events started")
    assert ui.first_error == "ERROR: after events started"
