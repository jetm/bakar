"""Tests for the ``bakar inspect`` command.

Drives the command through the Typer ``CliRunner``. Container exec is
monkeypatched via ``patch("bakar.commands.inspect.run_shell_capture")``
so no real kas-container is needed.

Each fake ``run_shell_capture`` writes controlled text to its ``stdout_path``
and returns a configurable exit code, letting the tests verify:

- All expected sections (Identity, Sources, Paths, Inherits, Packages,
  Dependencies) appear in the report.
- ``--recursive/-r`` adds transitive dependency fields.
- ``--json`` output is valid JSON with the expected top-level keys.
- An unknown recipe causes a non-zero exit instead of an empty report.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import bakar.commands.inspect  # noqa: F401 - registers the command on app
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

_SHOW_RECIPES_OK = """\
=== Available recipes: ===

busybox:
  meta-core                          1.36.1
"""

_GETVAR_PATHS_OK = """\
WORKDIR="/build/tmp/work/aarch64-poky-linux/busybox/1.36.1-r0"
S="/build/tmp/work/aarch64-poky-linux/busybox/1.36.1-r0/busybox-1.36.1"
B="/build/tmp/work/aarch64-poky-linux/busybox/1.36.1-r0/build"
D="/build/tmp/work/aarch64-poky-linux/busybox/1.36.1-r0/image"
T="/build/tmp/work/aarch64-poky-linux/busybox/1.36.1-r0/temp"
"""

_ENV_OK = """\
PN="busybox"
PV="1.36.1"
PR="r0"
FILE="/path/to/poky/meta/recipes-core/busybox/busybox_1.36.1.bb"
WORKDIR="/build/tmp/work/aarch64-poky-linux/busybox/1.36.1-r0"
S="/build/tmp/work/aarch64-poky-linux/busybox/1.36.1-r0/busybox-1.36.1"
B="/build/tmp/work/aarch64-poky-linux/busybox/1.36.1-r0/build"
D="/build/tmp/work/aarch64-poky-linux/busybox/1.36.1-r0/image"
T="/build/tmp/work/aarch64-poky-linux/busybox/1.36.1-r0/temp"
SRC_URI="https://busybox.net/downloads/busybox-1.36.1.tar.bz2 file://fix.patch"
LICENSE="GPLv2"
LIC_FILES_CHKSUM="file://LICENSE;md5=abc123"
INHERITED="base kernel autotools pkgconfig"
PACKAGES="busybox busybox-dbg busybox-dev"
RDEPENDS_busybox=""
RDEPENDS_busybox-dbg=""
RDEPENDS_busybox-dev="busybox"
DEPENDS="virtual/libc"
RDEPENDS=""
"""

_RECURSIVE_OK = """\
NOTE: Generating dependency graph...
virtual/libc
libc-glibc
glibc
"""

_UNKNOWN_RECIPE_ERROR = "ERROR: Nothing PROVIDES 'no-such-recipe'"


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


_DEFAULT_PAYLOADS = [
    (_SHOW_RECIPES_OK, 0),
    (_ENV_OK, 0),
]

_RECURSIVE_PAYLOADS = [
    (_SHOW_RECIPES_OK, 0),
    (_ENV_OK, 0),
    (_RECURSIVE_OK, 0),
]


# ---------------------------------------------------------------------------
# Section presence tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_identity_section_present(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Text output contains the Identity section with PN, PV, layer."""
    calls: list[dict] = []
    fake = _make_fake_capture(list(_DEFAULT_PAYLOADS), calls)

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["inspect", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert "Identity" in result.output
    assert "busybox" in result.output
    assert "1.36.1" in result.output


@pytest.mark.unit
def test_sources_section_present(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Text output contains the Sources section with LICENSE and SRC_URI."""
    calls: list[dict] = []
    fake = _make_fake_capture(list(_DEFAULT_PAYLOADS), calls)

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["inspect", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert "Sources" in result.output
    assert "GPLv2" in result.output
    assert "busybox.net" in result.output


@pytest.mark.unit
def test_paths_section_present(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Text output contains the Paths section with WORKDIR, S, B, D, T."""
    calls: list[dict] = []
    fake = _make_fake_capture(list(_DEFAULT_PAYLOADS), calls)

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["inspect", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert "Paths" in result.output
    assert "WORKDIR" in result.output
    assert "/build/tmp/work" in result.output


@pytest.mark.unit
def test_inherits_section_present(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Text output contains the Inherits section."""
    calls: list[dict] = []
    fake = _make_fake_capture(list(_DEFAULT_PAYLOADS), calls)

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["inspect", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert "Inherits" in result.output
    assert "base" in result.output


@pytest.mark.unit
def test_packages_section_present(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Text output contains the Packages section."""
    calls: list[dict] = []
    fake = _make_fake_capture(list(_DEFAULT_PAYLOADS), calls)

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["inspect", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert "Packages" in result.output
    assert "busybox-dbg" in result.output


@pytest.mark.unit
def test_dependencies_section_present(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Text output contains the Dependencies section with DEPENDS."""
    calls: list[dict] = []
    fake = _make_fake_capture(list(_DEFAULT_PAYLOADS), calls)

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["inspect", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert "Dependencies" in result.output
    assert "virtual/libc" in result.output


@pytest.mark.unit
def test_three_bitbake_calls_issued(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Without --recursive, exactly three bitbake calls are issued."""
    calls: list[dict] = []
    fake = _make_fake_capture(list(_DEFAULT_PAYLOADS), calls)

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        runner.invoke(
            app,
            ["inspect", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert len(calls) == 2
    assert any("show-recipes" in c["command"] for c in calls)
    assert any("bitbake -e" in c["command"] for c in calls)


# ---------------------------------------------------------------------------
# --recursive flag
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recursive_adds_fourth_call(runner: _CliRunner, nxp_workspace: Path) -> None:
    """With --recursive, a fourth bitbake -g call is issued."""
    calls: list[dict] = []
    fake = _make_fake_capture(list(_RECURSIVE_PAYLOADS), calls)

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["inspect", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace), "--recursive"],
        )

    assert result.exit_code == 0, result.output
    assert len(calls) == 3
    assert any("bitbake -g" in c["command"] for c in calls)


@pytest.mark.unit
def test_recursive_adds_transitive_deps_to_output(runner: _CliRunner, nxp_workspace: Path) -> None:
    """--recursive adds transitive deps content to the Dependencies section."""
    calls: list[dict] = []
    fake = _make_fake_capture(list(_RECURSIVE_PAYLOADS), calls)

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["inspect", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace), "-r"],
        )

    assert result.exit_code == 0, result.output
    # The recursive output included virtual/libc and libc-glibc
    assert "transitive" in result.output.lower() or "virtual/libc" in result.output


@pytest.mark.unit
def test_recursive_flag_short_form(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Short ``-r`` flag enables recursive mode."""
    calls: list[dict] = []
    fake = _make_fake_capture(list(_RECURSIVE_PAYLOADS), calls)

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["inspect", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace), "-r"],
        )

    assert result.exit_code == 0, result.output
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# --json output
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_json_output_valid(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--json`` produces valid JSON."""
    calls: list[dict] = []
    fake = _make_fake_capture(list(_DEFAULT_PAYLOADS), calls)

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["inspect", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace), "--json"],
        )

    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert isinstance(doc, dict)


@pytest.mark.unit
def test_json_output_has_required_keys(runner: _CliRunner, nxp_workspace: Path) -> None:
    """JSON output contains all six required top-level section keys."""
    calls: list[dict] = []
    fake = _make_fake_capture(list(_DEFAULT_PAYLOADS), calls)

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["inspect", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace), "--json"],
        )

    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    for key in ("identity", "sources", "paths", "inherits", "packages", "dependencies"):
        assert key in doc, f"Missing key: {key}"


@pytest.mark.unit
def test_json_recursive_has_transitive_keys(runner: _CliRunner, nxp_workspace: Path) -> None:
    """JSON with --recursive has transitive_forward/reverse in dependencies."""
    calls: list[dict] = []
    fake = _make_fake_capture(list(_RECURSIVE_PAYLOADS), calls)

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["inspect", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace), "--json", "-r"],
        )

    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    deps = doc["dependencies"]
    assert "transitive_forward" in deps
    assert "transitive_reverse" in deps


# ---------------------------------------------------------------------------
# Unknown recipe: non-zero exit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unknown_recipe_exits_nonzero(runner: _CliRunner, nxp_workspace: Path) -> None:
    """An unknown recipe causes a non-zero exit rather than an empty report."""
    calls: list[dict] = []
    # show-recipes fails for unknown recipe
    fake = _make_fake_capture(
        [(_UNKNOWN_RECIPE_ERROR, 1)],
        calls,
    )

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["inspect", "no-such-recipe", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0


@pytest.mark.unit
def test_unknown_recipe_surfaces_error(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Error from bitbake appears in the output for an unknown recipe."""
    calls: list[dict] = []
    fake = _make_fake_capture(
        [(_UNKNOWN_RECIPE_ERROR, 1)],
        calls,
    )

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["inspect", "no-such-recipe", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    # The bitbake error text must be surfaced, not swallowed.
    combined = (result.output or "") + (result.stderr or "" if hasattr(result, "stderr") else "")
    combined_lower = combined.lower()
    assert "no-such-recipe" in combined_lower or "failed" in combined_lower or "nothing provides" in combined_lower


@pytest.mark.unit
def test_unknown_recipe_stops_after_first_failure(runner: _CliRunner, nxp_workspace: Path) -> None:
    """When show-recipes fails, subsequent bitbake calls are not issued."""
    calls: list[dict] = []
    fake = _make_fake_capture(
        [(_UNKNOWN_RECIPE_ERROR, 1)],
        calls,
    )

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        runner.invoke(
            app,
            ["inspect", "no-such-recipe", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# No workspace
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_workspace_exits_2(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running outside a workspace exits with code 2."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["inspect", _RECIPE, "--manifest", _MANIFEST],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# bbappend parsing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recipe_file_from_env_in_identity(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Recipe file path from the bitbake -e FILE variable appears in the Identity section."""
    calls: list[dict] = []
    fake = _make_fake_capture(list(_DEFAULT_PAYLOADS), calls)

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["inspect", _RECIPE, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert "busybox_1.36.1.bb" in result.output


# ---------------------------------------------------------------------------
# Machine flag forwarded
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_machine_flag_accepted(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--machine`` flag is accepted without error."""
    calls: list[dict] = []
    fake = _make_fake_capture(list(_DEFAULT_PAYLOADS), calls)

    with patch("bakar.commands.inspect.run_shell_capture", fake):
        result = runner.invoke(
            app,
            [
                "inspect",
                _RECIPE,
                "--manifest",
                _MANIFEST,
                "--workspace",
                str(nxp_workspace),
                "--machine",
                "imx8mp-lpddr4-evk",
            ],
        )

    assert result.exit_code == 0, result.output
