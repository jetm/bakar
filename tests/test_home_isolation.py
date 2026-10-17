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


def test_constants_are_derived_from_home_in_a_fresh_interpreter(tmp_path):
    # Independent of the autouse fixture: a clean `python -I` that never imports
    # tests.conftest computes every constant from the HOME it was started with,
    # and the result is compared with home_constants() for that same home.
    fresh_home = tmp_path / "fresh-home"
    fresh_home.mkdir()
    expected = {(m, a): v for m, a, v in home_constants(fresh_home)}
    code = (
        "import importlib, sys; sys.path.insert(0, 'src');"
        f"[print(m, a, getattr(importlib.import_module(m), a)) for m, a in {sorted(expected)!r}]"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "BAKAR_"))}
    env["HOME"] = str(fresh_home)
    out_file = tmp_path / "constants.out"
    with out_file.open("w") as out:
        subprocess.run(
            [sys.executable, "-I", "-c", code],
            stdout=out,
            env=env,
            cwd=Path(__file__).resolve().parent.parent,
            check=True,
            timeout=120,
        )
    reported = {}
    for line in out_file.read_text().splitlines():
        module, attr, value = line.split(" ", 2)
        reported[(module, attr)] = Path(value)
    assert reported == expected


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


@pytest.mark.real_home
def test_real_home_is_not_a_session_home_even_in_a_worker():
    # Under xdist the worker inherits HOME already pointing at the controller's
    # session home; REAL_HOME must still be the developer's own.
    assert "bakar-test-home-" not in str(REAL_HOME)
    assert os.environ["BAKAR_TEST_REAL_HOME"] == str(REAL_HOME)
    assert Path.home() == REAL_HOME


def test_inherited_xdg_and_cache_variables_do_not_reach_a_test():
    for name in ("XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "SSTATE_DIR", "DL_DIR"):
        assert name not in os.environ, name
    stray = [n for n in os.environ if n.startswith("BAKAR_") and n != "BAKAR_TEST_REAL_HOME"]
    assert stray == []


def test_shared_ccache_dir_lands_under_the_isolated_home_without_xdg():
    from bakar.config import shared_ccache_dir

    assert shared_ccache_dir(None, ccache_shared=True) == Path.home() / ".cache" / "bakar" / "ccache"


def test_shared_ccache_dir_honours_xdg_set_before_the_call(monkeypatch, tmp_path):
    from bakar.config import shared_ccache_dir

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert shared_ccache_dir(None, ccache_shared=True) == tmp_path / "xdg" / "bakar" / "ccache"
