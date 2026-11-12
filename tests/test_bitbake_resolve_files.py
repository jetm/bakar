"""resolve_recipe_files: map recipe names to host recipe file paths via one kas shell."""

from __future__ import annotations

import contextlib
import os
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
        capture.stdout_path.write_text(self.stdout)
        if self.raises is not None:
            raise self.raises
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
    kas_work_dir = Path("/ws")
    host_mode = False


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


def test_nonzero_exit_keeps_the_answers_already_written(tmp_path):
    assert _call(FakeRunner(f"a\t{tmp_path}/a.bb\n", rc=1)) == {"a": tmp_path / "a.bb", "b": None}


def test_nonzero_exit_with_empty_log_maps_all_none():
    assert _call(FakeRunner("", rc=1)) == {"a": None, "b": None}


def test_timeout_keeps_the_answers_already_written(tmp_path):
    runner = FakeRunner(f"a\t{tmp_path}/a.bb\n", raises=subprocess.TimeoutExpired("kas", 1))
    assert _call(runner) == {"a": tmp_path / "a.bb", "b": None}


def test_timeout_drops_a_line_cut_off_mid_write(tmp_path):
    runner = FakeRunner(f"a\t{tmp_path}/a.bb\nb\t{tmp_path}/par", raises=subprocess.TimeoutExpired("kas", 1))
    assert _call(runner) == {"a": tmp_path / "a.bb", "b": None}


def test_timeout_with_nothing_written_maps_all_none():
    runner = FakeRunner(raises=subprocess.TimeoutExpired("kas", 1))
    assert _call(runner) == {"a": None, "b": None}


def test_result_names_the_lookup_run(tmp_path):
    got = _call(FakeRunner(""))
    assert got.run_id == tmp_path.name


def test_run_id_is_none_when_the_lookup_never_started(monkeypatch):
    def broken_ctx(cfg, kas_yaml):
        raise OSError("no runs dir")

    monkeypatch.setattr(bb, "_capture_context", broken_ctx)
    got = _call(FakeRunner(""))
    assert got == {"a": None, "b": None}
    assert got.run_id is None


def test_failure_to_start_maps_all_none():
    assert _call(FakeRunner(raises=OSError("no docker"))) == {"a": None, "b": None}


def test_container_path_translated_against_the_kas_work_dir(tmp_path):
    # meta-avocado: kas-container binds the workspace at /work, while bsp_root is a build-* subdirectory of it.
    class C(Cfg):
        workspace = tmp_path
        bsp_root = tmp_path / "build-qemu"
        kas_work_dir = tmp_path

    runner = FakeRunner("a\t/work/meta-openembedded/meta-oe/a.bb\n")
    got = resolve_recipe_files(C(), "x.yml", ["a"], runner=runner)
    assert got == {"a": (tmp_path / "meta-openembedded/meta-oe/a.bb").resolve()}


def test_host_mode_path_starting_with_work_is_left_alone(tmp_path):
    class C(Cfg):
        workspace = tmp_path
        bsp_root = tmp_path / "build-qemu"
        kas_work_dir = tmp_path
        host_mode = True

    runner = FakeRunner("a\t/work/meta/a.bb\n")
    got = resolve_recipe_files(C(), "x.yml", ["a"], runner=runner)
    assert got == {"a": Path("/work/meta/a.bb")}


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


def test_command_does_not_discard_stderr():
    runner = FakeRunner("")
    _call(runner)
    assert "/dev/null" not in runner.command


def _stub_runner(tmp_path, script_body: str):
    """Runner that executes the generated command under bash with a stub bitbake-getvar first on PATH."""
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    stub = stubs / "bitbake-getvar"
    stub.write_text("#!/bin/bash\n" + script_body)
    stub.chmod(0o755)

    def runner(ctx, command, capture):
        env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"}
        with capture.stdout_path.open("w") as out, capture.stderr_path.open("w") as err:
            return subprocess.run(["bash", "-c", command], env=env, stdout=out, stderr=err, check=False).returncode

    return runner


# The stub answers `bitbake-getvar --value --ignore-undefined -r <recipe> FILE`: $4 is -r, recipe is $5.
_STUB_BY_NAME = """
recipe="${@: -2:1}"
case "$recipe" in
  bad) exit 1 ;;
  *) echo "@ROOT@/$recipe.bb" ;;
esac
"""


def test_executed_command_handles_quoted_name_and_plain_name(tmp_path):
    runner = _stub_runner(tmp_path, _STUB_BY_NAME.replace("@ROOT@", str(tmp_path)))
    odd = "we ird'name"
    got = resolve_recipe_files(Cfg(), "x.yml", [odd, "plain"], runner=runner)
    assert got == {odd: tmp_path / f"{odd}.bb", "plain": tmp_path / "plain.bb"}


def test_executed_command_takes_last_line_after_a_note(tmp_path):
    body = _STUB_BY_NAME.replace("@ROOT@", str(tmp_path)).replace(
        'echo "', 'echo "NOTE: Starting bitbake server..."\n  echo "', 1
    )
    runner = _stub_runner(tmp_path, body)
    got = resolve_recipe_files(Cfg(), "x.yml", ["a"], runner=runner)
    assert got == {"a": tmp_path / "a.bb"}


def test_executed_command_keeps_the_cause_in_the_err_file(tmp_path):
    body = 'echo "ERROR: no provider for $4" >&2\nexit 1\n'
    runner = _stub_runner(tmp_path, body)
    captured = tmp_path / "out.txt"

    class Cap:
        stdout_path = captured
        stderr_path = tmp_path / "err.txt"

    assert runner(None, bb._file_lookup_command(["bad"]), Cap) == 0
    assert captured.read_text() == "bad\t\n"
    assert "ERROR: no provider" in Cap.stderr_path.read_text()


def test_executed_command_failing_recipe_prints_empty_value(tmp_path):
    runner = _stub_runner(tmp_path, _STUB_BY_NAME.replace("@ROOT@", str(tmp_path)))
    got = resolve_recipe_files(Cfg(), "x.yml", ["bad", "ok"], runner=runner)
    assert got == {"bad": None, "ok": tmp_path / "ok.bb"}


def test_executed_command_failing_recipe_line_shape(tmp_path):
    runner = _stub_runner(tmp_path, "exit 1\n")
    captured = tmp_path / "out.txt"

    class Cap:
        stdout_path = captured
        stderr_path = tmp_path / "err.txt"

    assert runner(None, bb._file_lookup_command(["bad"]), Cap) == 0
    assert captured.read_text() == "bad\t\n"


def test_executed_command_value_with_tab_stays_whole(tmp_path):
    d = tmp_path / "dir\twith tab"
    d.mkdir()
    runner = _stub_runner(tmp_path, f'echo "{d}/a.bb"\n')
    got = resolve_recipe_files(Cfg(), "x.yml", ["a"], runner=runner)
    assert got == {"a": d / "a.bb"}


@pytest.mark.parametrize("junk", ["relative/a.bb", "NOTE: something", "a.bb"])
def test_relative_or_junk_answer_maps_to_none(junk):
    assert _call(FakeRunner(f"a\t{junk}\n"), recipes=("a",)) == {"a": None}
