"""Tests for the ``bakar settings`` sub-app driven through Typer ``CliRunner``.

The CRUD functions in ``bakar.user_config`` resolve the live config path via
``_config_path(None)``, which defaults to ``~/.config/bakar/config.toml``. The
sub-app calls them with no path argument, so monkeypatching
``bakar.user_config._config_path`` to return a tmp path isolates every test
from the real config file.

``commands/settings.py`` prints via the shared ``Console(stderr=True)`` from
``commands/_app.py``; ``CliRunner`` merges stderr into ``result.output``, so all
output assertions read ``result.output`` rather than ``result.stdout``.

Importing ``bakar.commands.settings`` registers the sub-app on the shared
``app`` (``cli.py`` does not import it yet - that wiring lands in task 5.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bakar.commands.settings  # noqa: F401 - registers the settings sub-app on `app`
import bakar.user_config as user_config
from bakar.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the default config path to a tmp file the test owns.

    The path does not exist on disk yet; ``set`` creates it on first write.
    """
    path = tmp_path / "config.toml"
    monkeypatch.setattr(user_config, "_config_path", lambda p: path if p is None else p)
    return path


def test_set_then_get_round_trips_a_value(runner: _CliRunner, config_path: Path) -> None:
    set_result = runner.invoke(app, ["settings", "set", "defaults.nxp.machine", "imx95-var-dart"])
    assert set_result.exit_code == 0, set_result.output

    get_result = runner.invoke(app, ["settings", "get", "defaults.nxp.machine"])
    assert get_result.exit_code == 0, get_result.output
    assert "imx95-var-dart" in get_result.output


def test_get_unrecognized_key_exits_nonzero(runner: _CliRunner, config_path: Path) -> None:
    result = runner.invoke(app, ["settings", "get", "not.a.real.key"])
    assert result.exit_code != 0
    assert "not.a.real.key" in result.output


def test_set_unrecognized_key_exits_nonzero_without_writing(runner: _CliRunner, config_path: Path) -> None:
    result = runner.invoke(app, ["settings", "set", "not.a.real.key", "value"])
    assert result.exit_code != 0
    assert "not.a.real.key" in result.output
    assert not config_path.exists()


def test_set_bool_key_with_bad_value_exits_nonzero(runner: _CliRunner, config_path: Path) -> None:
    result = runner.invoke(app, ["settings", "set", "build.show_doctor_report", "maybe"])
    assert result.exit_code != 0
    assert not config_path.exists()


def test_list_shows_set_and_unset_keys(runner: _CliRunner, config_path: Path) -> None:
    set_result = runner.invoke(app, ["settings", "set", "defaults.nxp.machine", "imx95-var-dart"])
    assert set_result.exit_code == 0, set_result.output

    result = runner.invoke(app, ["settings", "list"])
    assert result.exit_code == 0, result.output
    # The key we set shows its value; an untouched key shows the unset marker.
    assert "defaults.nxp.machine" in result.output
    assert "imx95-var-dart" in result.output
    assert "defaults.ti.machine" in result.output
    assert "(unset)" in result.output


def test_list_with_no_config_file_shows_every_key_as_unset(runner: _CliRunner, config_path: Path) -> None:
    assert not config_path.exists()

    result = runner.invoke(app, ["settings", "list"])
    assert result.exit_code == 0, result.output
    # Every recognized key is listed, and each line carries the unset marker.
    for key in user_config.SETTINGS_SCHEMA:
        assert key in result.output
    assert result.output.count("(unset)") == len(user_config.SETTINGS_SCHEMA)


def test_unset_removes_a_key(runner: _CliRunner, config_path: Path) -> None:
    runner.invoke(app, ["settings", "set", "defaults.nxp.machine", "imx95-var-dart"])
    runner.invoke(app, ["settings", "set", "defaults.nxp.distro", "fsl-imx-wayland"])

    unset_result = runner.invoke(app, ["settings", "unset", "defaults.nxp.machine"])
    assert unset_result.exit_code == 0, unset_result.output

    assert runner.invoke(app, ["settings", "get", "defaults.nxp.machine"]).output.strip().endswith("(unset)")
    # The sibling key survives the removal.
    distro = runner.invoke(app, ["settings", "get", "defaults.nxp.distro"])
    assert "fsl-imx-wayland" in distro.output


def test_unset_absent_key_exits_zero_and_leaves_well_formed_file(runner: _CliRunner, config_path: Path) -> None:
    runner.invoke(app, ["settings", "set", "defaults.nxp.distro", "fsl-imx-wayland"])

    result = runner.invoke(app, ["settings", "unset", "defaults.nxp.machine"])
    assert result.exit_code == 0, result.output

    # The file still parses and the untouched key is intact.
    cfg = user_config.load_user_config(config_path)
    assert cfg.nxp_distro == "fsl-imx-wayland"
    assert cfg.nxp_machine is None
