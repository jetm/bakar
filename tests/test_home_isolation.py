"""The suite runs against an isolated home, never the developer's real one."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bakar import daemon_log, task_timings
from bakar.user_config import _config_path
from tests.conftest import (
    PASSTHROUGH_ENV_ALLOWLIST,
    REAL_HOME,
    SCRUBBED_ENV_NAMES,
    SCRUBBED_ENV_PREFIXES,
    home_constants,
)

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
    for name in sorted(SCRUBBED_ENV_NAMES):
        assert name not in os.environ, name
    assert "BUILD_DIR" in SCRUBBED_ENV_NAMES  # clean-cache --full deletes the directory it names
    stray = [n for n in os.environ if n.startswith(SCRUBBED_ENV_PREFIXES) and n != "BAKAR_TEST_REAL_HOME"]
    assert stray == []


def _environment_reads() -> dict[str, str]:
    """Literal names src/bakar reads from the environment, each with one ``file:line``.

    Covers ``os.environ.get/pop/setdefault("X")``, ``os.getenv("X")`` and
    ``os.environ["X"]``. A name built at runtime (f-string, variable) is
    invisible to an AST walk, so it cannot be classified here.
    """
    src = Path(__file__).resolve().parent.parent / "src" / "bakar"
    found: dict[str, str] = {}

    def literal(node: ast.AST) -> str | None:
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    def is_environ(node: ast.AST) -> bool:
        return isinstance(node, ast.Attribute) and node.attr == "environ"

    for path in sorted(src.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            name = None
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
                func = node.func
                environ_call = func.attr in ("get", "pop", "setdefault") and is_environ(func.value)
                getenv_call = func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os"
                if environ_call or getenv_call:
                    name = literal(node.args[0])
            elif isinstance(node, ast.Subscript) and is_environ(node.value):
                name = literal(node.slice)
            if name:
                found.setdefault(name, f"{path.relative_to(src.parent.parent)}:{node.lineno}")
    return found


def test_every_environment_variable_src_reads_is_scrubbed_or_allowlisted():
    unclassified = [
        f"{name} (read at {where})"
        for name, where in sorted(_environment_reads().items())
        if name not in SCRUBBED_ENV_NAMES
        and not name.startswith(SCRUBBED_ENV_PREFIXES)
        and name not in PASSTHROUGH_ENV_ALLOWLIST
    ]
    assert not unclassified, (
        "src/bakar reads environment variables that tests/conftest.py neither scrubs "
        "(SCRUBBED_ENV_NAMES) nor allowlists with a reason (PASSTHROUGH_ENV_ALLOWLIST): " + "; ".join(unclassified)
    )


def test_environment_allowlist_entries_have_reasons_and_are_still_read():
    reads = _environment_reads()
    assert all(reason.strip() for reason in PASSTHROUGH_ENV_ALLOWLIST.values())
    assert not set(PASSTHROUGH_ENV_ALLOWLIST) & SCRUBBED_ENV_NAMES
    stale = sorted(set(PASSTHROUGH_ENV_ALLOWLIST) - set(reads))
    assert not stale, f"allowlisted but no longer read by src/bakar: {stale}"


def test_shared_ccache_dir_lands_under_the_isolated_home_without_xdg():
    from bakar.config import shared_ccache_dir

    assert shared_ccache_dir(None, ccache_shared=True) == Path.home() / ".cache" / "bakar" / "ccache"


def test_shared_ccache_dir_honours_xdg_set_before_the_call(monkeypatch, tmp_path):
    from bakar.config import shared_ccache_dir

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert shared_ccache_dir(None, ccache_shared=True) == tmp_path / "xdg" / "bakar" / "ccache"
