"""Tests for the ``bakar getvar`` command.

Drives the command through the Typer ``CliRunner``. The container exec is
monkeypatched via ``patch("bakar.commands.getvar.run_shell_capture")`` so no
real kas-container is needed.

Each fake ``run_shell_capture`` writes controlled text to its ``stdout_path``
and returns a configurable exit code, letting the tests verify:

- Global (no recipe) getvar path.
- Recipe-scoped (``--recipe``) getvar path.
- Unexpanded (``--unexpanded``) flag forwarding.
- History (``--history``) path: source locations printed in order.
- History path with no history comments: exits 0, prints "no history recorded".
- Non-zero bitbake exit is surfaced as an error, not treated as success.
- JSON output includes the required keys.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import bakar.commands.getvar  # noqa: F401 - registers the command on app
from bakar.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

_MANIFEST = "imx-6.6.52-2.2.0.xml"
_VAR = "MACHINE"

# Fixture: bitbake-getvar output for MACHINE
_GETVAR_OUTPUT = """\
# $MACHINE
#   set /path/to/build/conf/local.conf:5
#     "imx8mp-lpddr4-evk"
MACHINE="imx8mp-lpddr4-evk"
"""

# Fixture: bitbake-getvar --unexpanded (-e) output for IMAGE_INSTALL
_GETVAR_UNEXPANDED_OUTPUT = """\
# $IMAGE_INSTALL
#   set /path/to/build/conf/local.conf:20
#     "${CORE_IMAGE_EXTRA_INSTALL}"
IMAGE_INSTALL="${CORE_IMAGE_EXTRA_INSTALL}"
"""

# Fixture: bitbake -e output (subset of env dump) with MACHINE history
_BITBAKE_E_OUTPUT = """\
#
# $MACHINE [2 operations]
#   set /path/to/build/conf/local.conf:5
#     "imx8mp-lpddr4-evk"
#   set /path/to/meta-imx/conf/machine/imx8mp-lpddr4-evk.conf:1
#     "imx8mp-lpddr4-evk"
MACHINE="imx8mp-lpddr4-evk"

#
# $DISTRO
#   set /path/to/build/conf/local.conf:10
#     "fsl-imx-wayland"
DISTRO="fsl-imx-wayland"
"""

# Fixture: bitbake -e output with NO history comments for BB_NUMBER_THREADS
_BITBAKE_E_NO_HISTORY_OUTPUT = """\
#
# $BB_NUMBER_THREADS [no history recorded]
BB_NUMBER_THREADS="8"
"""


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
    """Return a fake ``run_shell_capture`` that writes payloads and records calls.

    ``payloads`` is a list of ``(text, exit_code)`` in call order.
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


def _make_fake_capture_ctx(payloads: list[tuple[str, int]], calls: list[dict]):
    """Like ``_make_fake_capture`` but also records ``ctx.extra_overlays`` per call.

    Used by the colon-overlay tests to assert the parsed overlay chain reaches
    :class:`KasBuildContext`.
    """
    payload_iter = iter(payloads)

    def fake_capture(ctx, command, stdout_path, *, step="kas_shell_capture", python_executable=None):
        text, rc = next(payload_iter)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(text)
        calls.append({"command": command, "extra_overlays": list(ctx.extra_overlays)})
        return rc

    return fake_capture


@pytest.fixture
def machine_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "machine.yml"
    p.write_text("header:\n  version: 14\nmachine: imx8mp-var-dart\n", encoding="utf-8")
    return p


@pytest.fixture
def overlay_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "bringup.yml"
    p.write_text("header:\n  version: 14\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Colon-separated overlays (machine.yml:overlay.yml)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_getvar_colon_overlay_forwarded_to_ctx(
    runner: _CliRunner,
    nxp_workspace: Path,
    machine_yaml: Path,
    overlay_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A colon-joined positional 'machine.yml:overlay.yml' lands in ctx.extra_overlays.

    Regression: getvar took a single Path positional and passed the whole
    'a.yml:b.yml' string through filesystem resolution, failing with
    'kas YAML not found'. It must split on ':' like ``bakar build`` and thread
    the trailing segments through as overlays.

    Pins a default ``UserConfig`` so no opt-in tuning overlays are appended,
    isolating this assertion to the user-supplied colon overlay.
    """
    from bakar.user_config import UserConfig

    monkeypatch.setattr("bakar.commands._app._load_user_config_safe", UserConfig)
    calls: list[dict] = []
    fake = _make_fake_capture_ctx([(_GETVAR_OUTPUT, 0)], calls)
    kas_arg = f"{machine_yaml}:{overlay_yaml}"

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, kas_arg, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    extras = calls[0]["extra_overlays"]
    assert len(extras) == 1
    assert extras[0].resolve() == overlay_yaml.resolve()


@pytest.mark.unit
def test_getvar_single_yaml_no_extras(
    runner: _CliRunner,
    nxp_workspace: Path,
    machine_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare single positional YAML and a default config leave ctx.extra_overlays empty.

    Pins a default ``UserConfig`` (no tuning enabled) so neither a user overlay
    nor an opt-in tuning overlay appears.
    """
    from bakar.user_config import UserConfig

    monkeypatch.setattr("bakar.commands._app._load_user_config_safe", UserConfig)
    calls: list[dict] = []
    fake = _make_fake_capture_ctx([(_GETVAR_OUTPUT, 0)], calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, str(machine_yaml), "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert calls[0]["extra_overlays"] == []


@pytest.mark.unit
def test_getvar_colon_missing_overlay_exits(
    runner: _CliRunner,
    nxp_workspace: Path,
    machine_yaml: Path,
    tmp_path: Path,
) -> None:
    """A colon arg naming a missing overlay segment exits non-zero, not a silent drop."""
    missing = str(tmp_path / "missing-overlay.yml")
    kas_arg = f"{machine_yaml}:{missing}"

    result = runner.invoke(
        app,
        ["getvar", _VAR, kas_arg, "--workspace", str(nxp_workspace)],
    )

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Global getvar (no recipe)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_getvar_global_prints_value(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Without --recipe, runs bitbake-getvar and prints the resolved value."""
    calls: list[dict] = []
    fake = _make_fake_capture([(_GETVAR_OUTPUT, 0)], calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    # Command must include bitbake-getvar but NOT -r <recipe>
    assert "bitbake-getvar" in calls[0]["command"]
    assert "-r" not in calls[0]["command"]
    assert _VAR in calls[0]["command"]
    # Resolved value appears in output
    assert "imx8mp-lpddr4-evk" in result.output


# ---------------------------------------------------------------------------
# Recipe-scoped getvar
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_getvar_recipe_scopes_to_recipe(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--recipe`` passes -r <recipe> to bitbake-getvar."""
    recipe_output = """\
# $IMAGE_INSTALL
#   set /path/to/core-image-minimal.bb:10
IMAGE_INSTALL="packagegroup-core-boot"
"""
    calls: list[dict] = []
    fake = _make_fake_capture([(recipe_output, 0)], calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            [
                "getvar",
                "IMAGE_INSTALL",
                "--recipe",
                "core-image-minimal",
                "--manifest",
                _MANIFEST,
                "--workspace",
                str(nxp_workspace),
            ],
        )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    # Command must include -r <recipe>
    cmd = calls[0]["command"]
    assert "-r" in cmd
    assert "core-image-minimal" in cmd
    assert "IMAGE_INSTALL" in cmd
    # Value appears in output
    assert "packagegroup-core-boot" in result.output


# ---------------------------------------------------------------------------
# Unexpanded flag
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_getvar_unexpanded_forwards_flag(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--unexpanded`` passes -e to bitbake-getvar so ${...} refs are preserved."""
    calls: list[dict] = []
    fake = _make_fake_capture([(_GETVAR_UNEXPANDED_OUTPUT, 0)], calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            [
                "getvar",
                "IMAGE_INSTALL",
                "--unexpanded",
                "--manifest",
                _MANIFEST,
                "--workspace",
                str(nxp_workspace),
            ],
        )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    # The -e flag must appear in the bitbake-getvar command
    cmd = calls[0]["command"]
    assert "-u" in cmd
    # Output contains the unexpanded value
    assert "${CORE_IMAGE_EXTRA_INSTALL}" in result.output


# ---------------------------------------------------------------------------
# History path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_getvar_history_prints_source_locations(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--history`` runs bitbake -e and prints the ordered include-chain locations."""
    calls: list[dict] = []
    fake = _make_fake_capture([(_BITBAKE_E_OUTPUT, 0)], calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "--history", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    # Command must be bitbake -e (not bitbake-getvar)
    cmd = calls[0]["command"]
    assert "bitbake" in cmd
    assert "-e" in cmd
    assert "bitbake-getvar" not in cmd
    # Both source locations appear in output
    assert "local.conf:5" in result.output
    assert "imx8mp-lpddr4-evk.conf:1" in result.output


@pytest.mark.unit
def test_getvar_history_with_recipe(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--history --recipe`` appends the recipe name to ``bitbake -e``."""
    calls: list[dict] = []
    fake = _make_fake_capture([(_BITBAKE_E_OUTPUT, 0)], calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            [
                "getvar",
                _VAR,
                "--history",
                "--recipe",
                "core-image-minimal",
                "--manifest",
                _MANIFEST,
                "--workspace",
                str(nxp_workspace),
            ],
        )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    cmd = calls[0]["command"]
    # Recipe name appears in the bitbake -e command
    assert "core-image-minimal" in cmd


@pytest.mark.unit
def test_getvar_history_no_history_exits_0_with_message(runner: _CliRunner, nxp_workspace: Path) -> None:
    """When no history comments exist, exits 0 and prints 'no history recorded'."""
    calls: list[dict] = []
    fake = _make_fake_capture([(_BITBAKE_E_NO_HISTORY_OUTPUT, 0)], calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            [
                "getvar",
                "BB_NUMBER_THREADS",
                "--history",
                "--manifest",
                _MANIFEST,
                "--workspace",
                str(nxp_workspace),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "no history recorded" in result.output


# ---------------------------------------------------------------------------
# Non-zero bitbake exit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_getvar_nonzero_exit_surfaces_error(runner: _CliRunner, nxp_workspace: Path) -> None:
    """When bitbake-getvar exits non-zero, the command exits non-zero too."""
    error_output = "ERROR: Nothing PROVIDES 'BADVAR'\n"
    calls: list[dict] = []
    fake = _make_fake_capture([(error_output, 1)], calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", "BADVAR", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0
    assert len(calls) == 1


@pytest.mark.unit
def test_getvar_history_nonzero_exit_surfaces_error(runner: _CliRunner, nxp_workspace: Path) -> None:
    """When ``bitbake -e`` exits non-zero under --history, the command exits non-zero."""
    calls: list[dict] = []
    fake = _make_fake_capture([("ERROR: bitbake parse failed\n", 2)], calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "--history", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_getvar_json_global_has_required_keys(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--json`` output parses as JSON with required keys var and value."""
    calls: list[dict] = []
    fake = _make_fake_capture([(_GETVAR_OUTPUT, 0)], calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "--json", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert "var" in doc
    assert "value" in doc
    assert doc["var"] == _VAR
    assert doc["value"] == "imx8mp-lpddr4-evk"


@pytest.mark.unit
def test_getvar_json_history_has_history_key(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--history --json`` output has var and history keys; history is a list."""
    calls: list[dict] = []
    fake = _make_fake_capture([(_BITBAKE_E_OUTPUT, 0)], calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            [
                "getvar",
                _VAR,
                "--history",
                "--json",
                "--manifest",
                _MANIFEST,
                "--workspace",
                str(nxp_workspace),
            ],
        )

    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert "var" in doc
    assert "history" in doc
    assert isinstance(doc["history"], list)
    assert len(doc["history"]) == 2


@pytest.mark.unit
def test_getvar_json_no_history_is_empty_list(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--history --json`` with no history comments returns history: []."""
    calls: list[dict] = []
    fake = _make_fake_capture([(_BITBAKE_E_NO_HISTORY_OUTPUT, 0)], calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            [
                "getvar",
                "BB_NUMBER_THREADS",
                "--history",
                "--json",
                "--manifest",
                _MANIFEST,
                "--workspace",
                str(nxp_workspace),
            ],
        )

    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["history"] == []


@pytest.mark.unit
def test_getvar_json_recipe_key_present(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--recipe --json`` output includes the recipe key."""
    recipe_output = 'IMAGE_INSTALL="pkg-a"\n'
    calls: list[dict] = []
    fake = _make_fake_capture([(recipe_output, 0)], calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            [
                "getvar",
                "IMAGE_INSTALL",
                "--recipe",
                "core-image-minimal",
                "--json",
                "--manifest",
                _MANIFEST,
                "--workspace",
                str(nxp_workspace),
            ],
        )

    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert "recipe" in doc
    assert doc["recipe"] == "core-image-minimal"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_getvar_no_workspace_exits_2(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running outside a workspace exits with code 2."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["getvar", _VAR, "--manifest", _MANIFEST],
    )
    assert result.exit_code == 2


@pytest.mark.unit
def test_getvar_applies_sccache_tuning_overlay_when_enabled(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """getvar must apply the sccache tuning overlay so ``bakar getvar CC`` can show the prefix.

    The runbook's A1 check runs ``bakar getvar CC`` to confirm ``CCACHE = "sccache "``
    reached ``CC``. If getvar omits the opt-in tuning overlays, the launcher swap is
    invisible and the documented check is a false negative.
    """
    from bakar.user_config import UserConfig

    calls: list[dict] = []
    fake = _make_fake_capture_ctx([(_GETVAR_OUTPUT, 0)], calls)
    uc = UserConfig(sccache_dist=True, sccache_scheduler_url="http://localhost:10600")
    monkeypatch.setattr("bakar.commands._app._load_user_config_safe", lambda: uc)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    names = [p.name for p in calls[0]["extra_overlays"]]
    assert "bakar-tuning-sccache.yml" in names, names
