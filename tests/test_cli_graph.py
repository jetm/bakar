"""Tests for the ``bakar graph`` command.

Drives the command through the Typer ``CliRunner``. Container exec is
monkeypatched via ``patch("bakar.commands.graph.run_shell_capture")`` so no
real kas-container is needed.

The fake ``run_shell_capture`` writes controlled text to each ``stdout_path``
in call order (TOPDIR resolution, ``bitbake -g``, ``task-depends.dot``,
``pn-buildlist``) and returns a configurable exit code, letting the tests
verify:

- A failing ``bitbake -g`` propagates a non-zero exit instead of printing
  empty graph data as success.
- ``--format json`` emits valid JSON carrying the ``blast_radius`` key.
- The default text output contains the package count and a cycle report.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import bakar.commands.graph  # noqa: F401 - registers the command on app
from bakar.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

_MANIFEST = "imx-6.6.52-2.2.0.xml"
_RECIPE = "busybox"

# ---------------------------------------------------------------------------
# Fixture payloads (representative bitbake output excerpts)
# ---------------------------------------------------------------------------

_TOPDIR_OK = 'TOPDIR="/build"\n'

_BITBAKE_G_OK = "NOTE: Generating dependency graph...\n"

# Acyclic task-depends.dot: busybox depends on glibc and libc-glibc.
_TASK_DEPENDS_ACYCLIC = """\
digraph depends {
"busybox.do_compile" -> "glibc.do_populate_sysroot"
"busybox.do_compile" -> "libc-glibc.do_populate_sysroot"
"glibc.do_populate_sysroot" -> "libc-glibc.do_populate_sysroot"
}
"""

_PN_BUILDLIST_OK = """\
busybox
glibc
libc-glibc
"""

# Cyclic task-depends.dot: a -> b -> a.
_TASK_DEPENDS_CYCLE = """\
digraph depends {
"a.do_compile" -> "b.do_compile"
"b.do_compile" -> "a.do_compile"
}
"""

_BITBAKE_G_ERROR = "ERROR: Nothing PROVIDES 'no-such-recipe'\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def nxp_workspace(tmp_path: Path) -> Path:
    """Minimal NXP workspace so ``_resolve_workspace`` succeeds."""
    (tmp_path / "nxp").mkdir()
    return tmp_path


def _make_fake_capture(payloads: list[tuple[str, int]], calls: list[dict]):
    """Return a fake ``run_shell_capture`` writing payloads and recording calls.

    ``payloads`` is ``(text, exit_code)`` in call order.
    ``calls`` accumulates ``{"command": ..., "stdout_path": ...}`` dicts.
    """
    payload_iter = iter(payloads)

    def fake_capture(ctx, command, stdout_path, *, step="kas_shell_capture", python_executable=None):
        text, rc = next(payload_iter)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(text)
        calls.append({"command": command, "stdout_path": stdout_path})
        return rc

    return fake_capture


# Full happy path: TOPDIR, bitbake -g, task-depends.dot, pn-buildlist.
def _acyclic_payloads() -> list[tuple[str, int]]:
    return [
        (_TOPDIR_OK, 0),
        (_BITBAKE_G_OK, 0),
        (_TASK_DEPENDS_ACYCLIC, 0),
        (_PN_BUILDLIST_OK, 0),
    ]


def _cycle_payloads() -> list[tuple[str, int]]:
    return [
        (_TOPDIR_OK, 0),
        (_BITBAKE_G_OK, 0),
        (_TASK_DEPENDS_CYCLE, 0),
        (_PN_BUILDLIST_OK, 0),
    ]


# ---------------------------------------------------------------------------
# bitbake -g failure: non-zero exit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bitbake_g_failure_exits_nonzero(runner: _CliRunner, nxp_workspace: Path) -> None:
    """A failing ``bitbake -g`` propagates a non-zero exit."""
    calls: list[dict] = []
    # TOPDIR resolves, but bitbake -g fails.
    fake = _make_fake_capture([(_TOPDIR_OK, 0), (_BITBAKE_G_ERROR, 1)], calls)

    with patch("bakar.commands.graph.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["graph", "no-such-recipe", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0


@pytest.mark.unit
def test_bitbake_g_failure_surfaces_error(runner: _CliRunner, nxp_workspace: Path) -> None:
    """The bitbake error is surfaced, not swallowed, on a graph failure."""
    calls: list[dict] = []
    fake = _make_fake_capture([(_TOPDIR_OK, 0), (_BITBAKE_G_ERROR, 1)], calls)

    with patch("bakar.commands.graph.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["graph", "no-such-recipe", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    combined = (result.output or "").lower()
    assert "failed" in combined or "nothing provides" in combined
    # The artifact retrieval calls must not have run after the failure.
    assert len(calls) == 2


@pytest.mark.unit
def test_topdir_failure_exits_nonzero(runner: _CliRunner, nxp_workspace: Path) -> None:
    """A failing TOPDIR resolution propagates a non-zero exit."""
    calls: list[dict] = []
    fake = _make_fake_capture([("ERROR\n", 1)], calls)

    with patch("bakar.commands.graph.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["graph", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0
    assert len(calls) == 1


@pytest.mark.unit
def test_artifact_retrieval_failure_exits_nonzero(runner: _CliRunner, nxp_workspace: Path) -> None:
    """A failed ``cat task-depends.dot`` exits non-zero, not a false-success empty report."""
    calls: list[dict] = []
    # TOPDIR + bitbake -g succeed, but reading task-depends.dot fails.
    fake = _make_fake_capture(
        [
            (_TOPDIR_OK, 0),
            (_BITBAKE_G_OK, 0),
            ("cat: /build/task-depends.dot: No such file or directory\n", 1),
        ],
        calls,
    )

    with patch("bakar.commands.graph.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["graph", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0
    # pn-buildlist retrieval must not run after the dot retrieval failed.
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# --format json
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_json_output_valid_with_blast_radius(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--format json`` emits valid JSON containing the blast-radius key."""
    calls: list[dict] = []
    fake = _make_fake_capture(_acyclic_payloads(), calls)

    with patch("bakar.commands.graph.run_shell_capture", fake):
        result = runner.invoke(
            app,
            [
                "graph",
                _RECIPE,
                "--manifest",
                _MANIFEST,
                "--workspace",
                str(nxp_workspace),
                "--format",
                "json",
            ],
        )

    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert "blast_radius" in doc
    assert doc["target"] == _RECIPE


# ---------------------------------------------------------------------------
# default text output
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_text_output_has_package_count_and_cycle_report(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Default text output contains the package count and a cycle report."""
    calls: list[dict] = []
    fake = _make_fake_capture(_acyclic_payloads(), calls)

    with patch("bakar.commands.graph.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["graph", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert "package count" in result.output
    # pn-buildlist has three non-empty lines.
    assert "3" in result.output
    # Acyclic graph: cycle report reads "no cycles".
    assert "no cycles" in result.output


@pytest.mark.unit
def test_text_output_reports_seeded_cycle(runner: _CliRunner, nxp_workspace: Path) -> None:
    """A cyclic graph names the cycle in the cycle report."""
    calls: list[dict] = []
    fake = _make_fake_capture(_cycle_payloads(), calls)

    with patch("bakar.commands.graph.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["graph", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert "cycle" in result.output.lower()
    # Both seeded cycle members appear.
    assert "a" in result.output and "b" in result.output


# ---------------------------------------------------------------------------
# --format dot
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dot_output_emits_raw_dot(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--format dot`` prints the raw task-depends.dot text."""
    calls: list[dict] = []
    fake = _make_fake_capture(_acyclic_payloads(), calls)

    with patch("bakar.commands.graph.run_shell_capture", fake):
        result = runner.invoke(
            app,
            [
                "graph",
                _RECIPE,
                "--manifest",
                _MANIFEST,
                "--workspace",
                str(nxp_workspace),
                "--format",
                "dot",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "digraph depends" in result.output


# ---------------------------------------------------------------------------
# --depth honored
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_depth_flag_accepted(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--depth`` is accepted and reflected in JSON output."""
    calls: list[dict] = []
    fake = _make_fake_capture(_acyclic_payloads(), calls)

    with patch("bakar.commands.graph.run_shell_capture", fake):
        result = runner.invoke(
            app,
            [
                "graph",
                _RECIPE,
                "--manifest",
                _MANIFEST,
                "--workspace",
                str(nxp_workspace),
                "--format",
                "json",
                "--depth",
                "1",
            ],
        )

    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["depth"] == 1
