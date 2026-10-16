"""Post-build step failures must never be attributed to the build by triage.

``bakar.triage`` is in the analysis group and cannot import the commands
package, so its exclusion set holds the post-build step names as literals.
These tests are what keeps that copy in sync with ``commands/_post_build.py``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

import bakar.commands._post_build as post_build
from bakar.cli import app
from bakar.triage import _POST_BUILD_STEPS, is_build_failure_event

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


def _post_build_step_names() -> dict[str, str]:
    return {name: value for name, value in vars(post_build).items() if re.fullmatch(r"_STEP_[A-Z_]+", name)}


def test_every_post_build_step_is_excluded_from_build_failure() -> None:
    steps = _post_build_step_names()
    assert steps, "no _STEP_* constants found in commands/_post_build.py"
    missing = sorted(value for value in steps.values() if value not in _POST_BUILD_STEPS)
    assert missing == []
    for value in steps.values():
        assert not is_build_failure_event({"event": "step_fail", "step": value})


def test_a_real_build_step_failure_is_still_a_build_failure() -> None:
    assert is_build_failure_event({"event": "step_fail", "step": "kas_build"})


_SUCCESS_THEN_FEED_FAIL = (
    '{"event": "step_start", "step": "kas_build", "ts": "2026-05-29T12:00:00Z"}\n'
    '{"event": "step_ok", "step": "kas_build", "ts": "2026-05-29T12:05:00Z"}\n'
    '{"event": "step_start", "step": "feed_sync", "ts": "2026-05-29T12:06:00Z"}\n'
    '{"event": "step_fail", "step": "feed_sync", "reason": "rsync exited 23", "ts": "2026-05-29T12:07:00Z"}\n'
)
_CLEAN = (
    '{"event": "step_start", "step": "kas_build", "ts": "2026-05-30T12:00:00Z"}\n'
    '{"event": "step_ok", "step": "kas_build", "ts": "2026-05-30T12:05:00Z"}\n'
)


def test_failed_feed_sync_does_not_make_a_successful_run_the_failed_one(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".bakar.toml").write_text("")
    for subdir, ts, events in (
        ("distro-alpha-1.0", "20260529-120000", _SUCCESS_THEN_FEED_FAIL),
        ("distro-beta-2.0", "20260530-120000", _CLEAN),
    ):
        run = tmp_path / "build" / subdir / "build" / "runs" / ts
        run.mkdir(parents=True)
        (run / "events.jsonl").write_text(events)
        (run / "kas.log").write_text("NOTE: ok\n")
    monkeypatch.chdir(tmp_path)

    # The older run's only step_fail is a post-build one, so no run counts as
    # failed and triage falls back to the newest; before the fix it picked the
    # older run and reported feed_sync as the failing step.
    result = runner.invoke(app, ["triage"])

    assert result.exit_code == 0, result.output
    assert "triage 20260530-120000" in result.output
    assert "20260529-120000" not in result.output
    assert "failing step: feed_sync" not in result.output
