"""Shared fixtures for the bakar test suite.

Provides hermetic fixtures and sample-content constants consumed by the
Category B logic-module tests (``test_triage``, ``test_workspace``,
``test_layers``) and the Category A command-module tests
(``test_cli_log``, ``test_cli_triage``, etc.).

All fixtures stay rooted in ``tmp_path`` so tests never touch the real
host filesystem. The sample constants intentionally match the parsers
in ``src/bakar``:

- ``MINIMAL_NXP_MANIFEST`` uses ``path=`` and a 40-hex-char ``revision``
  because ``workspace.parse_manifest_pins`` filters on those exact two
  attributes (``src/bakar/workspace.py:106-110``).
- ``SAMPLE_EVENTS_JSONL`` uses ``event``/``step``/``reason`` keys because
  ``triage._last_event_matching`` filters on ``rec.get("event")`` and
  ``analyse`` reads ``step``/``reason`` (``src/bakar/triage.py:53,209-210``).
- ``SAMPLE_KAS_LOG`` includes an ``ERROR: <recipe> do_compile: ...`` line
  matching ``_RECIPE_ERROR_RE`` (``src/bakar/triage.py:101-105``).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

# Prevent Rich from inserting ANSI escape codes into captured CLI output.
# Without this, --help text arrives with mid-token color resets (e.g.
# "--sstate" + ESC[0m + "-mirror"), breaking plain substring assertions.
os.environ.setdefault("NO_COLOR", "1")

if TYPE_CHECKING:
    from pathlib import Path

# Two synthetic projects pinned to 40-hex-char SHAs.  parse_manifest_pins
# only emits pins whose revision matches _HEX40_RE, so the SHAs below are
# deliberately literal 40-char hex strings.
MINIMAL_NXP_MANIFEST = """\
<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="freescale" fetch="https://github.com/nxp-imx"/>
  <default revision="master" remote="freescale" sync-j="4"/>
  <project path="sources/poky" name="poky" revision="{sha_a}"/>
  <project path="sources/meta-imx" name="meta-imx" revision="{sha_b}"/>
</manifest>
""".format(sha_a="a" * 40, sha_b="b" * 40)

# Two JSON lines: a step_start and a matching step_fail for the same
# step. triage._last_event_matching scans for event=="step_fail" and
# analyse() reads step/reason off the resulting record.
SAMPLE_EVENTS_JSONL = (
    '{"event": "step_start", "step": "kas-build", "ts": "2026-05-29T12:00:00Z"}\n'
    '{"event": "step_fail", "step": "kas-build", "reason": "bitbake exited 1", '
    '"ts": "2026-05-29T12:05:00Z"}\n'
)

# Includes one line matching _RECIPE_ERROR_RE (recipe + do_compile +
# message) plus surrounding context so _scan_recipe_errors finds exactly
# one RecipeError and _tail has multiple lines to slice.
SAMPLE_KAS_LOG = """\
NOTE: Resolving any missing task queue dependencies
Initialising tasks: 100% |#######################################| Time: 0:00:03
NOTE: Executing Tasks
ERROR: linux-imx-6.6.52+gitAUTOINC+a1b2c3d4e5-r0 do_compile: Function failed: do_compile
ERROR: Logfile of failure stored in: /work/tmp/work/linux-imx/temp/log.do_compile.12345
NOTE: Tasks Summary: Attempted 4321 tasks of which 4320 didn't need to be rerun and 1 failed.
"""


@pytest.fixture
def fake_workspace(tmp_path: Path) -> Path:
    """Minimal NXP workspace: ``.bakar.toml`` marker, ``nxp/`` subdir, manifest."""
    (tmp_path / ".bakar.toml").write_text("")
    nxp = tmp_path / "nxp"
    nxp.mkdir()
    (nxp / "imx-6.1.55-2.2.0.xml").write_text(MINIMAL_NXP_MANIFEST)
    return tmp_path


@pytest.fixture
def fake_run_dir(tmp_path: Path) -> Path:
    """Synthetic build run dir with ``events.jsonl`` and ``kas.log``."""
    run = tmp_path / "build" / "runs" / "20260529-120000"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(SAMPLE_EVENTS_JSONL)
    (run / "kas.log").write_text(SAMPLE_KAS_LOG)
    return run
