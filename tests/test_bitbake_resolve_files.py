"""resolve_recipe_files: map recipe names to host recipe file paths via one kas shell."""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

import pytest

import bakar.commands.bitbake as bb
from bakar.commands.bitbake import resolve_recipe_files


class FakeRunner:
    """Stands in for run_shell_capture; writes ``stdout`` to the capture file."""

    def __init__(self, stdout: str = "", rc: int = 0, raises: BaseException | None = None):
        self.stdout = stdout
        self.rc = rc
        self.raises = raises
        self.command: str | None = None
        self.capture = None

    def __call__(self, ctx, command, capture):
        self.command = command
        self.capture = capture
        if self.raises is not None:
            raise self.raises
        capture.stdout_path.write_text(self.stdout)
        return self.rc


@pytest.fixture(autouse=True)
def _no_kas(monkeypatch, tmp_path):
    """Replace the kas context setup; tests only exercise the shell step."""

    @contextlib.contextmanager
    def fake_ctx(cfg, kas_yaml):
        yield object(), tmp_path

    monkeypatch.setattr(bb, "_capture_context", fake_ctx)


class Cfg:
    bsp_root = Path("/ws")
    workspace = Path("/ws")


def _call(runner, recipes=("a", "b")):
    return resolve_recipe_files(Cfg(), "x.yml", list(recipes), runner=runner)


def test_two_recipes_resolved(tmp_path):
    runner = FakeRunner(f"a\t{tmp_path}/a.bb\nb\t{tmp_path}/b.bb\n")
    assert _call(runner) == {"a": tmp_path / "a.bb", "b": tmp_path / "b.bb"}


def test_failed_recipe_is_none_other_resolves(tmp_path):
    runner = FakeRunner(f"a\t\nb\t{tmp_path}/b.bb\n")
    assert _call(runner) == {"a": None, "b": tmp_path / "b.bb"}


def test_empty_output_maps_all_none():
    assert _call(FakeRunner("")) == {"a": None, "b": None}


def test_nonzero_exit_maps_all_none(tmp_path):
    assert _call(FakeRunner(f"a\t{tmp_path}/a.bb\n", rc=1)) == {"a": None, "b": None}


def test_timeout_maps_all_none_without_raising():
    runner = FakeRunner(raises=subprocess.TimeoutExpired("kas", 1))
    assert _call(runner) == {"a": None, "b": None}


def test_failure_to_start_maps_all_none():
    assert _call(FakeRunner(raises=OSError("no docker"))) == {"a": None, "b": None}


def test_container_path_translated(tmp_path):
    class C(Cfg):
        bsp_root = tmp_path

    runner = FakeRunner("a\t/work/meta/a.bb\n")
    got = resolve_recipe_files(C(), "x.yml", ["a"], runner=runner)
    assert got == {"a": (tmp_path / "meta/a.bb").resolve()}


def test_unnormalised_path_is_realpathed(tmp_path):
    (tmp_path / "meta").mkdir()
    runner = FakeRunner(f"a\t{tmp_path}/build/../meta/a.bb\n")
    got = resolve_recipe_files(Cfg(), "x.yml", ["a"], runner=runner)
    assert got == {"a": (tmp_path / "meta" / "a.bb").resolve()}


def test_command_shape():
    runner = FakeRunner("")
    _call(runner)
    assert runner.command is not None
    assert "-r" in runner.command
    assert "FILE" in runner.command
    assert "bitbake-getvar" in runner.command
    assert runner.capture.env_overrides == {"SHELL": "/bin/bash"}
    assert runner.capture.isolate_process_group is True
    assert runner.capture.timeout is not None
