"""Tests for the ``bakar diffsigs`` command.

Drives the command through the Typer ``CliRunner``. The container exec
is monkeypatched via ``patch("bakar.commands.diffsigs.run_shell_capture")``
so no real kas-container is needed.

Each fake ``run_shell_capture`` writes controlled text to its ``stdout_path``
and returns a configurable exit code, letting the tests verify:

- Both ``bitbake -S printdiff`` and ``bitbake-diffsigs -t`` calls are
  issued in order.
- On success, the rendered diff text is printed.
- When the second call exits non-zero (missing sigdata), the command exits
  non-zero with a clear diagnostic message rather than printing an empty diff.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import bakar.commands.diffsigs  # noqa: F401 - registers the command on app
from bakar.cli import app
from bakar.commands.diffsigs import _extract_dep_diff, _render_diffsigs, _strip_kas_preamble
from tests._fakes import make_fake_run_shell_capture as _make_fake_capture

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

_MANIFEST = "imx-6.6.52-2.2.0.xml"
_RECIPE = "busybox"
_TASK = "do_compile"

_DIFF_OUTPUT = """\
Preparing runqueue
NOTE: Executing Tasks
Variable do_compile changed:
  old: 'sha256:aabbcc'
  new: 'sha256:ddeeff'
"""

_EMPTY_OUTPUT = ""


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def nxp_workspace(tmp_path: Path) -> Path:
    """Minimal NXP workspace so ``_resolve_workspace`` succeeds."""
    (tmp_path / "nxp").mkdir()
    return tmp_path


@pytest.mark.unit
def test_success_prints_diff_and_runs_both_commands(runner: _CliRunner, nxp_workspace: Path) -> None:
    """On success both bitbake calls run in order and the diff text is printed."""
    calls: list[dict] = []
    fake = _make_fake_capture(
        [("", 0), (_DIFF_OUTPUT, 0)],
        calls,
    )

    with patch("bakar.commands.diffsigs.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["diffsigs", _RECIPE, _TASK, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output

    # Both calls must have been issued.
    assert len(calls) == 2

    # First call: bitbake -S printdiff
    assert f"bitbake -S printdiff {_RECIPE}" in calls[0]["command"]

    # Second call: bitbake-diffsigs -t
    assert f"bitbake-diffsigs -t {_RECIPE} {_TASK}" in calls[1]["command"]

    # Diff content appears in output.
    assert "Variable do_compile changed" in result.output
    assert "aabbcc" in result.output


@pytest.mark.unit
def test_missing_sigdata_exits_nonzero_with_message(runner: _CliRunner, nxp_workspace: Path) -> None:
    """When bitbake-diffsigs exits non-zero with empty output, report missing sigdata."""
    calls: list[dict] = []
    # printdiff succeeds; diffsigs exits 1 with empty output (no prior sigdata).
    fake = _make_fake_capture(
        [("", 0), (_EMPTY_OUTPUT, 1)],
        calls,
    )

    with patch("bakar.commands.diffsigs.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["diffsigs", _RECIPE, _TASK, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0

    # Both calls must still have run.
    assert len(calls) == 2

    # The message must point the user toward running a build first.
    output_lower = result.output.lower()
    assert "sigdata" in output_lower or "prior build" in output_lower or "build first" in output_lower


@pytest.mark.unit
def test_missing_sigdata_message_explicit_text(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Output explicitly mentions sigdata does not exist when output is empty."""
    calls: list[dict] = []
    fake = _make_fake_capture(
        [("", 0), ("", 1)],
        calls,
    )

    with patch("bakar.commands.diffsigs.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["diffsigs", _RECIPE, _TASK, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0
    # The message must clearly state a prior build is needed.
    assert "build" in result.output.lower()


@pytest.mark.unit
def test_missing_sigdata_with_no_such_file_message(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Output containing 'No such file' is classified as missing sigdata."""
    calls: list[dict] = []
    fake = _make_fake_capture(
        [("", 0), ("No such file or directory: stamps/...", 1)],
        calls,
    )

    with patch("bakar.commands.diffsigs.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["diffsigs", _RECIPE, _TASK, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0
    # A missing-sigdata message is shown; the raw error is not just dumped.
    assert "sigdata" in result.output.lower() or "build" in result.output.lower()


@pytest.mark.unit
def test_printdiff_failure_stops_before_diffsigs(runner: _CliRunner, nxp_workspace: Path) -> None:
    """When bitbake -S printdiff fails, the second call must not run."""
    calls: list[dict] = []
    # Only one payload: printdiff fails. diffsigs must not be called.
    fake = _make_fake_capture(
        [("bitbake parse error", 1)],
        calls,
    )

    with patch("bakar.commands.diffsigs.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["diffsigs", _RECIPE, _TASK, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0
    # Only the printdiff call was issued.
    assert len(calls) == 1
    assert "printdiff" in calls[0]["command"]


@pytest.mark.unit
def test_call_order_printdiff_before_diffsigs(runner: _CliRunner, nxp_workspace: Path) -> None:
    """The printdiff call must always precede the diffsigs call."""
    calls: list[dict] = []
    fake = _make_fake_capture(
        [("", 0), ("task hash diff output", 0)],
        calls,
    )

    with patch("bakar.commands.diffsigs.run_shell_capture", fake):
        runner.invoke(
            app,
            ["diffsigs", _RECIPE, _TASK, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert len(calls) == 2
    # Verify order: first printdiff, then diffsigs.
    assert "printdiff" in calls[0]["command"]
    assert "diffsigs" in calls[1]["command"]


@pytest.mark.unit
def test_no_workspace_exits_2(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running outside a workspace exits with code 2."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["diffsigs", _RECIPE, _TASK, "--manifest", _MANIFEST],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------

_SAMPLE_DIFFSIGS = """\
2026-06-03 12:59:14 - INFO     - kas 5.2 started on Fedora Linux 40
2026-06-03 12:59:14 - WARNING  - kas-container (5.3) and kas (5.2) versions do not match
2026-06-03 12:59:14 - INFO     - Repository bitbake already contains abc1234 as commit
2026-06-03 12:59:14 - INFO     - Repository bitbake checked out to abc1234
2026-06-03 12:59:14 - INFO     - To start the default build, run: bitbake -c build avocado-distro
NOTE: Starting bitbake server...
Hash for task dependency linux-yocto:do_configure changed from aaa to
bbb
    Hash for task dependency kern-tools-native:do_compile changed from ccc to
ddd
        Hash for task dependency kern-tools-native:do_configure changed from eee to
fff
                                    Task dependencies changed from:
                                    ['AR', 'AS', 'CC', 'CCACHE_CONFIGPATH']
                                    to:
                                    ['AR', 'AS', 'CC', 'CCACHE_CONFIGPATH', 'CCACHE_MAXSIZE']
                                    basehash changed from eee to fff
                                    Dependency on variable CCACHE_MAXSIZE was added
"""


@pytest.mark.unit
def test_strip_kas_preamble_removes_log_lines() -> None:
    """kas INFO/WARNING lines are filtered; NOTE and Hash lines are kept."""
    lines = _SAMPLE_DIFFSIGS.splitlines()
    clean = _strip_kas_preamble(lines)
    assert not any("INFO" in line and "kas" in line for line in clean)
    assert any("NOTE:" in line for line in clean)
    assert any("Hash for task" in line for line in clean)


@pytest.mark.unit
def test_extract_dep_diff_finds_added_variable() -> None:
    """_extract_dep_diff returns CCACHE_MAXSIZE as added, nothing removed."""
    lines = _SAMPLE_DIFFSIGS.splitlines()
    clean = _strip_kas_preamble(lines)
    added, removed = _extract_dep_diff(clean)
    assert "CCACHE_MAXSIZE" in added
    assert removed == []


@pytest.mark.unit
def test_render_diffsigs_output_contains_root_cause(capsys) -> None:
    """Rendered output calls console.print with root cause and chain."""
    # _render_diffsigs uses Rich console; capture via the printed text in result
    # We verify it doesn't crash and the root cause line from the fixture appears.
    from unittest.mock import patch as mpatch

    printed: list[str] = []
    with mpatch("bakar.commands.diffsigs.console") as mock_console:
        mock_console.print.side_effect = lambda *a, **kw: printed.append(str(a[0]) if a else "")
        _render_diffsigs(_SAMPLE_DIFFSIGS)
    combined = "\n".join(printed)
    assert "CCACHE_MAXSIZE" in combined
    assert "kern-tools-native:do_configure" in combined
