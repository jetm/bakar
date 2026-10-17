"""The suite runs against an isolated home, never the developer's real one."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from bakar import daemon_log, task_timings
from bakar.user_config import _config_path
from tests.conftest import REAL_HOME, home_constants

pytestmark = pytest.mark.unit


def test_session_home_is_set_before_package_import():
    # A fresh interpreter that loads conftest first must see bakar's import-time
    # constants under the session home, not under the home it was started with.
    code = (
        "import os, sys; sys.path.insert(0, 'src'); sys.path.insert(0, '.');"
        "import tests.conftest; import bakar.daemon_log as d;"
        "print(os.environ['HOME']); print(d.STATE_DIR)"
    )
    env = {**os.environ, "HOME": "/nonexistent-real-home"}
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parent.parent,
        check=True,
    )
    home, state = done.stdout.split()
    assert "bakar-test-home-" in home
    assert state.startswith(home)


def test_user_config_path_is_under_isolated_home():
    home = Path(os.environ["HOME"])
    assert home != REAL_HOME
    assert Path.home() == home
    assert _config_path(None) == home / ".config" / "bakar" / "config.toml"


def test_state_constants_follow_the_isolated_home():
    home = Path.home()
    state = home / ".local" / "state" / "bakar"
    assert state == daemon_log.STATE_DIR
    assert task_timings.TIMINGS_DIR.is_relative_to(home)
    for module, attr, value in home_constants(home):
        assert getattr(sys.modules[module], attr) == value, f"{module}.{attr}"
        assert getattr(sys.modules[module], attr).is_relative_to(home), f"{module}.{attr}"


def test_user_config_file_fixture_roundtrip(user_config_file):
    assert user_config_file == Path.home() / ".config" / "bakar" / "config.toml"
    assert user_config_file.parent.is_dir()
    user_config_file.write_text("[build]\n")
    assert _config_path(None).read_text() == "[build]\n"


@pytest.mark.real_home
def test_real_home_marker_restores_the_real_home():
    assert Path.home() == REAL_HOME
    assert daemon_log.STATE_DIR == REAL_HOME / ".local" / "state" / "bakar"
    assert task_timings.TIMINGS_DIR == REAL_HOME / ".local/state/bakar/task-timings"
