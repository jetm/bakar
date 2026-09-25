"""Unit tests for the continuation-block behavior in ``BuildUIState.process_line``.

Covers forwarding the indented detail of a multi-line ERROR/FATAL block to the
live console: an open block claims following indented, non-blank lines up to
``CONTINUATION_MAX_LINES``, then one marker line, then drops the rest silently;
it closes at the first non-indented or blank line.
"""

from __future__ import annotations

from bakar.steps.build_ui import CONTINUATION_MARKER, CONTINUATION_MAX_LINES, BuildUIState

# Verbatim PC3 kas.log lines for the OE-core sanity-checker misconfiguration
# report: an ERROR head line, three indented detail lines, then an unrelated
# Summary line that must not be swept into the block.
_HEAD = "ERROR:  OE-core's config sanity checker detected a potential misconfiguration."
_DETAIL_1 = "    Either fix the cause of this error or at your own risk disable the checker (see sanity.conf)."
_DETAIL_2 = "    Following is the list of potential problems / advisories:"
_DETAIL_3 = (
    "    DL_DIR: /mnt/JETM_SATA_9.1T/yocto-cache/downloads exists but you do not appear to have write access to it. "
)
_SUMMARY = "Summary: There was 1 WARNING message."


def test_sanity_checker_block_forwarded_and_summary_dropped() -> None:
    ui = BuildUIState()
    results = [ui.process_line(line) for line in (_HEAD, _DETAIL_1, _DETAIL_2, _DETAIL_3, _SUMMARY)]

    assert results[:4] == [_HEAD, _DETAIL_1, _DETAIL_2, _DETAIL_3]
    assert results[4] is None
    assert ui.error_count == 1
    assert ui.warn_count == 0


def test_block_caps_at_max_lines_then_one_marker() -> None:
    ui = BuildUIState()
    ui.process_line(_HEAD)

    indented_lines = [f"    detail line {i}" for i in range(35)]
    results = [ui.process_line(line) for line in indented_lines]

    forwarded = [r for r in results if r is not None]
    assert forwarded == [*indented_lines[:CONTINUATION_MAX_LINES], CONTINUATION_MARKER]
    assert ui.error_count == 1
    assert ui.warn_count == 0


def test_indented_line_with_no_open_block_is_not_forwarded() -> None:
    ui = BuildUIState()
    assert ui.process_line("    a stray indented line") is None


def test_knotty_task_line_closes_block() -> None:
    ui = BuildUIState()
    ui.process_line(_HEAD)
    ui.process_line(_DETAIL_1)

    knotty_line = "0: busybox-1.36.1-r0 do_compile - 3s (pid 1234)"
    ui.process_line(knotty_line)

    assert ui._cont_open is False

    # A later indented line is no longer part of any block and is dropped.
    assert ui.process_line("    not a continuation") is None
