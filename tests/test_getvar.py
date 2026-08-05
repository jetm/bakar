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
- The stream contract: the resolved value lands on stdout, verbatim and
  unstyled, and never on stderr.
- Unset-versus-failed exit semantics, both flag spellings (``--flag`` and the
  inline ``VAR[flag]``), and that ``-f`` still means ``--manifest``.
"""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import bakar.commands.getvar  # noqa: F401 - registers the command on app
from bakar.cli import app
from tests._fakes import make_fake_run_shell_capture as _make_fake_capture

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

_MANIFEST = "imx-6.6.52-2.2.0.xml"
_VAR = "MACHINE"

# Fixture: bitbake-getvar --value output for MACHINE
_GETVAR_OUTPUT = "imx8mp-lpddr4-evk\n"

# Fixture: bitbake-getvar --value -u output for IMAGE_INSTALL
_GETVAR_UNEXPANDED_OUTPUT = "${CORE_IMAGE_EXTRA_INSTALL}\n"

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


def _make_fake_capture_ctx(payloads: list[tuple[str, int]], calls: list[dict]):
    """Like ``_make_fake_capture`` but also records ``ctx.extra_overlays`` per call.

    Used by the colon-overlay tests to assert the parsed overlay chain reaches
    :class:`KasBuildContext`.
    """
    payload_iter = iter(payloads)

    def fake_capture(ctx, command, stdout_path, *, step="kas_shell_capture", python_executable=None, stderr_path=None):
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

    monkeypatch.setattr("bakar.commands._app._load_user_config_safe", lambda: UserConfig(ccache=False))
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
    # host_mode defaults on (no container image), so the host isolation overlay
    # is appended; filter it to assert the user colon-overlay forwarding.
    # cache-classify is always on, so 2 extras remain: the user's colon
    # overlay and the always-on cache-classify tuning overlay.
    extras = [o for o in calls[0]["extra_overlays"] if o.name != "bakar-tuning-host.yml"]
    assert len(extras) == 2
    assert overlay_yaml.resolve() in {o.resolve() for o in extras}
    assert any(o.name == "bakar-tuning-cache-classify.yml" for o in extras)


@pytest.mark.unit
def test_getvar_single_yaml_no_extras(
    runner: _CliRunner,
    nxp_workspace: Path,
    machine_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare single positional YAML and a default config leave no USER extra overlays.

    Pins a default ``UserConfig`` (no opt-in tuning enabled) so no user overlay
    and no opt-in tuning overlay appears - only the always-on cache-classify
    overlay remains, since it is unconditional regardless of config.
    """
    from bakar.user_config import UserConfig

    monkeypatch.setattr("bakar.commands._app._load_user_config_safe", lambda: UserConfig(ccache=False))
    calls: list[dict] = []
    fake = _make_fake_capture_ctx([(_GETVAR_OUTPUT, 0)], calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, str(machine_yaml), "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    # host_mode defaults on (no container image), so the host isolation
    # overlay is appended; filter it out along with the always-on
    # cache-classify overlay, leaving zero USER-supplied extra overlays.
    user_extras = [
        o
        for o in calls[0]["extra_overlays"]
        if o.name not in {"bakar-tuning-host.yml", "bakar-tuning-cache-classify.yml"}
    ]
    assert user_extras == []


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
    assert "imx8mp-lpddr4-evk" in result.stdout


# ---------------------------------------------------------------------------
# Recipe-scoped getvar
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_getvar_recipe_scopes_to_recipe(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--recipe`` passes -r <recipe> to bitbake-getvar."""
    recipe_output = "packagegroup-core-boot\n"
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
    assert "packagegroup-core-boot" in result.stdout


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
    # The -u flag must appear as its own token: a substring check would also
    # match the "-u" inside "--ignore-undefined" and assert nothing.
    cmd = calls[0]["command"]
    assert "-u" in cmd.split()
    # Output contains the unexpanded value
    assert "${CORE_IMAGE_EXTRA_INSTALL}" in result.stdout


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
    assert "local.conf:5" in result.stdout
    assert "imx8mp-lpddr4-evk.conf:1" in result.stdout


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
    assert "no history recorded" in result.stdout


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
# stdout / stderr split
# ---------------------------------------------------------------------------


def _make_fake_split_capture(stdout_text: str, stderr_text: str, rc: int, calls: list[dict]):
    """Fake capture that writes distinct payloads to the stdout and stderr files."""

    def fake_capture(ctx, command, stdout_path, *, step="kas_shell_capture", python_executable=None, stderr_path=None):
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(stdout_text)
        if stderr_path is not None:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            stderr_path.write_text(stderr_text)
        calls.append({"command": command, "stdout_path": stdout_path, "stderr_path": stderr_path})
        return rc

    return fake_capture


@pytest.mark.unit
def test_getvar_value_excludes_kas_stderr_chatter(runner: _CliRunner, nxp_workspace: Path) -> None:
    """kas INFO progress lines go to the stderr capture, never into the printed value."""
    calls: list[dict] = []
    fake = _make_fake_split_capture(_GETVAR_OUTPUT, "INFO: kas is doing something\n", 0, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert calls[0]["stderr_path"] is not None
    assert calls[0]["stderr_path"] != calls[0]["stdout_path"]
    assert result.output.strip() == "imx8mp-lpddr4-evk"
    assert "INFO: kas" not in result.output


@pytest.mark.unit
def test_getvar_failure_surfaces_stderr_diagnostics(runner: _CliRunner, nxp_workspace: Path) -> None:
    """On a non-zero exit the kas/bitbake error text from stderr is shown."""
    calls: list[dict] = []
    fake = _make_fake_split_capture("", "ERROR: ParseError in recipe\n", 1, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", "BADVAR", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0
    assert "ParseError in recipe" in result.output


# ---------------------------------------------------------------------------
# Stream contract: the resolved value is stdout-only, verbatim, unstyled
#
# These assert on ``result.stdout`` and ``result.stderr`` separately and never
# on ``result.output``: under click 8.3 ``result.output`` is the two streams
# interleaved, so an ``in result.output`` assertion passes identically whether
# the value is echoed to stdout or printed through the stderr Rich console and
# therefore proves nothing about the stream split.
#
# Not covered here: whether diagnostics are *styled* on an interactive
# terminal. Under ``CliRunner`` neither stream is a TTY, so Rich emits no ANSI
# on either one; the styled-diagnostics half of the contract is an E2E check.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_getvar_value_lands_on_stdout_only(runner: _CliRunner, nxp_workspace: Path) -> None:
    """The resolved value goes to stdout and never leaks onto stderr."""
    calls: list[dict] = []
    fake = _make_fake_split_capture(_GETVAR_OUTPUT, "INFO: kas is doing something\n", 0, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.stderr
    assert "imx8mp-lpddr4-evk" in result.stdout
    assert "imx8mp-lpddr4-evk" not in result.stderr


@pytest.mark.unit
def test_getvar_long_value_is_one_unwrapped_stdout_line(runner: _CliRunner, nxp_workspace: Path) -> None:
    """A value past 80 chars stays a single stdout line.

    The stderr Rich console wraps at width 80 when not attached to a TTY. A
    value routed through it comes back split across lines, which is what turned
    a kas error into an orphan fragment in the original report.
    """
    long_value = (
        "/opt/toolchain/sysroots/x86_64-pokysdk-linux/usr/bin/aarch64-poky-linux/aarch64-poky-linux-gcc"
        " --sysroot=/opt/sysroot"
    )
    assert len(long_value) > 80
    calls: list[dict] = []
    fake = _make_fake_split_capture(f"{long_value}\n", "", 0, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", "CC", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [long_value]


@pytest.mark.unit
def test_getvar_square_brackets_survive_verbatim_on_stdout(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Square brackets in a value are not eaten as Rich markup."""
    bracket_value = "ERROR: [Errno 2] no such file; tune [red]arm[/] variant"
    calls: list[dict] = []
    fake = _make_fake_split_capture(f"{bracket_value}\n", "", 0, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", "TUNE_FEATURES", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [bracket_value]


@pytest.mark.unit
def test_getvar_stdout_carries_no_ansi_escapes(runner: _CliRunner, nxp_workspace: Path) -> None:
    """The value payload is never styled, so stdout stays pipe-safe."""
    calls: list[dict] = []
    fake = _make_fake_split_capture(_GETVAR_OUTPUT, "", 0, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.stderr
    assert "\x1b[" not in result.stdout


@pytest.mark.unit
def test_getvar_unexpanded_exits_0_with_token_level_flags(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--unexpanded`` exits 0 and builds the documented bitbake-getvar token set.

    Token membership via ``shlex.split``, not substring: ``"-u" in cmd`` is a
    false-green because ``-u`` occurs inside ``--ignore-undefined``.
    """
    calls: list[dict] = []
    fake = _make_fake_split_capture(_GETVAR_UNEXPANDED_OUTPUT, "", 0, calls)

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

    assert result.exit_code == 0, result.stderr
    tokens = shlex.split(calls[0]["command"])
    assert "-u" in tokens
    assert "--value" in tokens
    assert "--ignore-undefined" in tokens
    assert "${CORE_IMAGE_EXTRA_INSTALL}" in result.stdout


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
    recipe_output = "pkg-a\n"
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


@pytest.mark.unit
def test_getvar_sccache_dist_flag_applies_overlay(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``--sccache-dist`` flag enables the overlay without a UserConfig entry.

    --sccache-dist is a global flag, not build-only: it lets ``bakar getvar
    CC --recipe <target> --sccache-dist`` resolve the same value a
    --sccache-dist build runs, so the launcher swap can be verified by
    inspection. No UserConfig is set here, proving the flag alone enables it.
    """
    calls: list[dict] = []
    fake = _make_fake_capture_ctx([(_GETVAR_OUTPUT, 0)], calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["--sccache-dist", "getvar", _VAR, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    names = [p.name for p in calls[0]["extra_overlays"]]
    assert "bakar-tuning-sccache.yml" in names, names


# ---------------------------------------------------------------------------
# Unset vs failed, flag spellings, verbatim values
#
# Every assertion here reads ``result.stdout`` / ``result.stderr`` separately
# and inspects the recorded command at token level via ``shlex.split``: a
# passing exit code alone cannot show that the right bitbake invocation was
# built, and ``result.output`` interleaves the two streams.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_getvar_unset_variable_exits_0_with_empty_stdout(runner: _CliRunner, nxp_workspace: Path) -> None:
    """An unset variable is an empty answer, not a failed query.

    Under ``--ignore-undefined`` bitbake prints nothing and exits 0 for a
    variable that was never set. ``SSTATE_MIRRORS`` reported as an error is the
    defect this guards.
    """
    calls: list[dict] = []
    fake = _make_fake_split_capture("", "", 0, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", "SSTATE_MIRRORS", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == ""
    assert "--ignore-undefined" in shlex.split(calls[0]["command"])


@pytest.mark.unit
def test_getvar_failed_query_still_exits_nonzero(runner: _CliRunner, nxp_workspace: Path) -> None:
    """A genuinely failed bitbake call keeps its non-zero status.

    The unset-is-fine change must not swallow real failures: exit status
    reports "bitbake could not be asked", output reports "the variable has no
    value", and the two signals stay distinct.
    """
    calls: list[dict] = []
    fake = _make_fake_split_capture("", "ERROR: ParseError at conf/local.conf:3\n", 1, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0
    assert "ParseError" in result.stderr


@pytest.mark.unit
def test_getvar_flag_option_and_inline_bracket_build_identical_commands(
    runner: _CliRunner, nxp_workspace: Path
) -> None:
    """``--flag x86_64 VAR`` and ``VAR[x86_64]`` normalise to one query."""
    checksum = "e4c5c1a1a9f13c5c9c3b4e3a4a6b0f2f4b8c0d1e2f3a4b5c6d7e8f90a1b2c3d4"
    calls: list[dict] = []
    fake = _make_fake_split_capture(f"{checksum}\n", "", 0, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        flag_result = runner.invoke(
            app,
            [
                "getvar",
                "UNINATIVE_CHECKSUM",
                "--flag",
                "x86_64",
                "--manifest",
                _MANIFEST,
                "--workspace",
                str(nxp_workspace),
            ],
        )
        inline_result = runner.invoke(
            app,
            [
                "getvar",
                "UNINATIVE_CHECKSUM[x86_64]",
                "--manifest",
                _MANIFEST,
                "--workspace",
                str(nxp_workspace),
            ],
        )

    assert flag_result.exit_code == 0, flag_result.stderr
    assert inline_result.exit_code == 0, inline_result.stderr
    assert calls[0]["command"] == calls[1]["command"]
    tokens = shlex.split(calls[0]["command"])
    assert tokens[tokens.index("-f") + 1] == "x86_64"
    assert tokens[-1] == "UNINATIVE_CHECKSUM"
    assert checksum in flag_result.stdout
    assert checksum in inline_result.stdout


@pytest.mark.unit
def test_getvar_bare_name_of_flag_only_variable_exits_0(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``UNINATIVE_CHECKSUM`` with no flag has no value, and that is not a failure."""
    calls: list[dict] = []
    fake = _make_fake_split_capture("", "", 0, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", "UNINATIVE_CHECKSUM", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == ""
    assert "-f" not in shlex.split(calls[0]["command"])


@pytest.mark.unit
def test_getvar_dash_f_still_means_manifest(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``-f <manifest>`` keeps resolving the MANIFEST, not a variable flag.

    ``--flag`` is long-only precisely because ``-f`` is ``--manifest`` here and
    across 23 command modules, and ``bakar getvar MACHINE -f <manifest>`` is the
    form printed in ``docs/getvar.md``.
    """
    calls: list[dict] = []
    manifests: list[str] = []

    def fake_capture(ctx, command, stdout_path, *, step="kas_shell_capture", python_executable=None, stderr_path=None):
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(_GETVAR_OUTPUT)
        manifests.append(ctx.cfg.manifest)
        calls.append({"command": command})
        return 0

    with patch("bakar.commands.getvar.run_shell_capture", fake_capture):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "-f", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.stderr
    assert manifests == [_MANIFEST]
    tokens = shlex.split(calls[0]["command"])
    assert "-f" not in tokens
    assert _MANIFEST not in tokens
    assert "imx8mp-lpddr4-evk" in result.stdout


@pytest.mark.unit
def test_getvar_value_with_quotes_is_verbatim_on_stdout(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Embedded quote characters survive with no escaping added or removed."""
    quoted_value = 'BusyBox "the Swiss Army knife" of \'embedded\' Linux \\"escaped\\"'
    calls: list[dict] = []
    fake = _make_fake_split_capture(f"{quoted_value}\n", "", 0, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            [
                "getvar",
                "SUMMARY",
                "--recipe",
                "busybox",
                "--manifest",
                _MANIFEST,
                "--workspace",
                str(nxp_workspace),
            ],
        )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [quoted_value]


@pytest.mark.unit
def test_getvar_unexpanded_with_flag_option_builds_full_token_set(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--unexpanded --flag`` sends -u, --value, -f and the flag name, exiting 0.

    The flag-valued form is the option combination that made ``--unexpanded``
    broken today (bitbake rejects ``-u`` without ``--value``), so it is asserted
    on its own rather than left to the global form.
    """
    calls: list[dict] = []
    fake = _make_fake_split_capture("${WORKDIR}/md5\n", "", 0, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            [
                "getvar",
                "SRC_URI",
                "--flag",
                "md5sum",
                "--unexpanded",
                "--recipe",
                "busybox",
                "--manifest",
                _MANIFEST,
                "--workspace",
                str(nxp_workspace),
            ],
        )

    assert result.exit_code == 0, result.stderr
    tokens = shlex.split(calls[0]["command"])
    assert "-u" in tokens
    assert "--value" in tokens
    assert tokens[tokens.index("-f") + 1] == "md5sum"


@pytest.mark.unit
def test_getvar_unexpanded_with_inline_bracket_builds_full_token_set(runner: _CliRunner, nxp_workspace: Path) -> None:
    """``--unexpanded 'VAR[flag]'`` sends the same token set as the ``--flag`` form."""
    calls: list[dict] = []
    fake = _make_fake_split_capture("${WORKDIR}/md5\n", "", 0, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            [
                "getvar",
                "SRC_URI[md5sum]",
                "--unexpanded",
                "--recipe",
                "busybox",
                "--manifest",
                _MANIFEST,
                "--workspace",
                str(nxp_workspace),
            ],
        )

    assert result.exit_code == 0, result.stderr
    tokens = shlex.split(calls[0]["command"])
    assert "-u" in tokens
    assert "--value" in tokens
    assert tokens[tokens.index("-f") + 1] == "md5sum"
    assert tokens[-1] == "SRC_URI"


# ---------------------------------------------------------------------------
# Verbatim kas errors and phase attribution
#
# The reported defect: a kas ``RepoRefError`` routed through the stderr Rich
# console came back as the single orphan line ``82cf21bc... as commit``. Rich
# wraps at width 80 when not attached to a TTY, so the message lost the word
# "Branch", the repository name, and the fact that it was a checkout failure -
# the surviving tail read as noise and was dismissed as such.
#
# ``_CHECKOUT_ERROR_LINE`` is kas's own spelling from ``kas/repos.py:547-550``
# on ONE line, because that is how it reaches a caller at runtime. A fixture
# re-wrapped at some other column is a different string and would classify as
# ``undetermined``, quietly turning every checkout assertion below into a test
# of the wrapping rather than of the classifier.
# ---------------------------------------------------------------------------

_CHECKOUT_ERROR_LINE = (
    'Branch "2.8-avocado" in repository "bitbake" does not contain commit "82cf21bc6d1a2941c2dd292ecea327031a247a8b"'
)

_KAS_CHECKOUT_LOG = "\n".join(
    [
        "INFO     - kas 4.4",
        "INFO     - Repository bitbake cloned",
        f"ERROR    - {_CHECKOUT_ERROR_LINE}",
        "INFO     - Cleaning up",
    ]
)


@pytest.mark.unit
def test_getvar_kas_checkout_error_reaches_stderr_intact(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Every line of a multi-line kas checkout error survives, in original order."""
    calls: list[dict] = []
    fake = _make_fake_split_capture("", f"{_KAS_CHECKOUT_LOG}\n", 1, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0
    err_lines = result.stderr.splitlines()
    expected = _KAS_CHECKOUT_LOG.splitlines()
    assert all(line in err_lines for line in expected)
    # Original order, not merely presence: a reordered log misattributes which
    # step the failure interrupted.
    positions = [err_lines.index(line) for line in expected]
    assert positions == sorted(positions)


@pytest.mark.unit
def test_getvar_kas_error_line_past_80_chars_is_not_split(runner: _CliRunner, nxp_workspace: Path) -> None:
    """The long checkout line stays one stderr line rather than wrapping at 80."""
    calls: list[dict] = []
    fake = _make_fake_split_capture("", f"{_KAS_CHECKOUT_LOG}\n", 1, calls)
    assert len(_CHECKOUT_ERROR_LINE) > 80

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0
    matching = [line for line in result.stderr.splitlines() if _CHECKOUT_ERROR_LINE in line]
    assert len(matching) == 1


@pytest.mark.unit
def test_getvar_kas_error_keeps_repo_branch_and_full_hash(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Repository, branch, and the full 40-char commit id all survive.

    The original report lost all three, leaving ``82cf21bc... as commit`` - a
    fragment a caller cannot act on.
    """
    calls: list[dict] = []
    fake = _make_fake_split_capture("", f"{_KAS_CHECKOUT_LOG}\n", 1, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0
    assert '"bitbake"' in result.stderr
    assert '"2.8-avocado"' in result.stderr
    assert "82cf21bc6d1a2941c2dd292ecea327031a247a8b" in result.stderr


@pytest.mark.unit
def test_getvar_kas_error_phase_label_precedes_verbatim_block(runner: _CliRunner, nxp_workspace: Path) -> None:
    """stderr names the checkout phase, and does so before the error text."""
    calls: list[dict] = []
    fake = _make_fake_split_capture("", f"{_KAS_CHECKOUT_LOG}\n", 1, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0
    err = result.stderr
    assert "checkout" in err
    assert err.index("checkout") < err.index(_CHECKOUT_ERROR_LINE)


@pytest.mark.unit
def test_getvar_bracketed_error_text_survives_verbatim(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Square-bracketed text is not consumed as Rich markup."""
    bracketed = "ERROR    - kas: unknown option [--foo] in section [bar]"
    calls: list[dict] = []
    fake = _make_fake_split_capture("", f"{bracketed}\n", 1, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0
    assert bracketed in result.stderr.splitlines()


@pytest.mark.unit
def test_getvar_failure_leaves_stdout_empty_without_json(runner: _CliRunner, nxp_workspace: Path) -> None:
    """Without --json a failure writes nothing to stdout - the payload stream stays clean."""
    calls: list[dict] = []
    fake = _make_fake_split_capture("", f"{_KAS_CHECKOUT_LOG}\n", 1, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0
    assert result.stdout == ""


@pytest.mark.unit
def test_getvar_json_failure_emits_phase_and_full_error_on_stdout(runner: _CliRunner, nxp_workspace: Path) -> None:
    """--json on failure puts one parseable document on stdout, phase and error included."""
    calls: list[dict] = []
    fake = _make_fake_split_capture("", f"{_KAS_CHECKOUT_LOG}\n", 1, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "--json", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0
    doc = json.loads(result.stdout)
    assert doc["phase"] == "checkout"
    for line in _KAS_CHECKOUT_LOG.splitlines():
        assert line in doc["error"]


@pytest.mark.unit
def test_getvar_unrecognised_failure_reports_undetermined_phase(runner: _CliRunner, nxp_workspace: Path) -> None:
    """An unmatched failure is labelled ``undetermined`` rather than guessed at.

    A confident wrong attribution recreates the original defect with the added
    authority of a label.
    """
    noise = "ERROR    - something nobody has a signature for yet"
    calls: list[dict] = []
    fake = _make_fake_split_capture("", f"{noise}\n", 1, calls)

    with patch("bakar.commands.getvar.run_shell_capture", fake):
        result = runner.invoke(
            app,
            ["getvar", _VAR, "--json", "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code != 0
    doc = json.loads(result.stdout)
    assert doc["phase"] == "undetermined"
    assert noise in doc["error"]
