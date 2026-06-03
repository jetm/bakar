"""Regression tests for the Python 3 multi-type ``except`` fixes.

Two files carried Python 2 ``except A, B:`` syntax that silently bound the
second type as the ``as`` target and caught only the first. The tuple form
(``except (A, B):``) catches both. These tests lock in the behavior the fix
guarantees:

* :func:`bakar.triage.analyse` swallows a ``KeyError`` from an
  ``error-report.json`` missing the ``"step"`` key and falls through to the
  live-parse path rather than propagating.
* :func:`bakar.bsp_detect.is_bbsetup_workspace` returns ``False`` when reading
  ``config/config-upstream.json`` raises ``OSError``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bakar.bsp_detect import is_bbsetup_workspace
from bakar.triage import TriageReport, analyse

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.unit
def test_triage_swallows_keyerror_from_missing_step_key(tmp_path: Path) -> None:
    """A ``KeyError`` (missing ``"step"``) falls through to the live-parse path.

    The fast path does ``data["step"]`` first; with ``"step"`` absent the
    ``except (json.JSONDecodeError, KeyError, TypeError)`` clause must catch the
    ``KeyError`` so ``analyse`` continues to the live-parse path instead of
    propagating. The kas.log carries a recipe whose ERROR line only the
    live-parse path scans, so its presence in the result proves the fall-through.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run_dir.joinpath("events.jsonl").write_text(
        '{"event": "step_fail", "step": "kas_build", "ts": "2026-06-01T10:00:00Z"}\n'
    )
    run_dir.joinpath("kas.log").write_text("ERROR: live-parse-recipe-1.0-r0 do_compile: Function failed\n")
    # Valid JSON, but missing the "step" key the fast path reads first.
    run_dir.joinpath("error-report.json").write_text(
        json.dumps(
            {
                "machine": "imx8mm",
                "distro": "fslc-framebuffer",
                "bsp_family": "nxp",
                "exit_code": 1,
                "kas_log_tail": ["some tail line"],
                "recipe_errors": [],
                "suggestions": [],
            }
        )
    )

    report = analyse(run_dir, tmp_path)

    assert isinstance(report, TriageReport)
    # Fell through to live-parse: the kas.log recipe is surfaced, which the
    # fast path would never read.
    assert any("live-parse-recipe" in e.recipe for e in report.recipe_errors)


@pytest.mark.unit
def test_is_bbsetup_workspace_returns_false_on_oserror_read(tmp_path: Path) -> None:
    """An ``OSError`` on the config read returns ``False`` without raising.

    The early existence checks must pass (so the read is reached); patching
    ``Path.read_text`` to raise ``OSError`` forces the
    ``except (json.JSONDecodeError, OSError)`` clause to catch it. Under the old
    Python 2 ``except json.JSONDecodeError, OSError:`` form, ``OSError`` would
    propagate.
    """
    ws = tmp_path / "ws"
    (ws / "config").mkdir(parents=True)
    (ws / "config" / "config-upstream.json").write_text('{"data": {}, "bitbake-config": {}}')
    (ws / "build").mkdir(parents=True)
    (ws / "build" / "init-build-env").write_text("")

    with patch("pathlib.Path.read_text", side_effect=OSError("simulated unreadable file")):
        result = is_bbsetup_workspace(ws)

    assert result is False
