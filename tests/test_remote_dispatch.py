"""Unit tests for bakar.steps.remote_dispatch pure builders.

These cover the host-free builders only: the exclude set, the rsync argv
constructor, the ``--on`` stripper, the remote bash-script generator, and the
``rsync --delete`` workspace guard. Orchestration (ssh/rsync subprocess) is a
later task; nothing here touches a live host.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from bakar.steps.remote_dispatch import (
    RSYNC_EXCLUDES,
    assert_safe_workspace,
    build_remote_script,
    build_rsync_argv,
    strip_on_option,
)

pytestmark = pytest.mark.unit

WS = Path("/home/tiamarin/repos/work/peridio-scarthgap-build")
HOST = "pc2"


# ---------------------------------------------------------------------------
# RSYNC_EXCLUDES
# ---------------------------------------------------------------------------


def test_excludes_is_a_tuple() -> None:
    assert isinstance(RSYNC_EXCLUDES, tuple)


@pytest.mark.parametrize(
    "pattern",
    [
        "build-*/",
        "**/tmp/",
        "**/sstate-cache/",
        "**/downloads/",
        ".bakar/runs/",
        "ccache/",
        "**/.venv/",
        "**/__pycache__/",
        "**/*.pyc",
    ],
)
def test_expected_patterns_present(pattern: str) -> None:
    assert pattern in RSYNC_EXCLUDES


def test_git_is_never_excluded() -> None:
    # kas/bitbake read git state for SRCREV/AUTOREV, so .git must be synced.
    assert ".git" not in RSYNC_EXCLUDES
    assert not any("git" in pat for pat in RSYNC_EXCLUDES)


# ---------------------------------------------------------------------------
# build_rsync_argv
# ---------------------------------------------------------------------------


def test_rsync_argv_base_flags() -> None:
    argv = build_rsync_argv(WS, HOST)
    assert argv[0] == "rsync"
    assert "-a" in argv
    assert "--delete" in argv
    assert "-n" not in argv
    assert "-i" not in argv


def test_rsync_argv_dry_run_flags() -> None:
    argv = build_rsync_argv(WS, HOST, dry_run=True)
    assert "-n" in argv
    assert "-i" in argv


def test_rsync_argv_one_exclude_per_pattern() -> None:
    argv = build_rsync_argv(WS, HOST)
    for pat in RSYNC_EXCLUDES:
        assert f"--exclude={pat}" in argv
    assert argv.count("--delete") == 1
    excludes = [a for a in argv if a.startswith("--exclude=")]
    assert len(excludes) == len(RSYNC_EXCLUDES)


def test_rsync_argv_source_and_dest_same_absolute_path_with_trailing_slash() -> None:
    argv = build_rsync_argv(WS, HOST)
    # Source and destination are the last two tokens.
    src, dest = argv[-2], argv[-1]
    assert src == f"{WS}/"
    assert dest == f"{HOST}:{WS}/"
    assert src.endswith("/")
    assert dest.endswith("/")


# ---------------------------------------------------------------------------
# strip_on_option
# ---------------------------------------------------------------------------


def test_strip_on_two_token_form() -> None:
    args = ["build", "my.yml", "--on", "pc2", "--yes"]
    assert strip_on_option(args) == ["build", "my.yml", "--yes"]


def test_strip_on_equals_form() -> None:
    args = ["build", "my.yml", "--on=pc2", "--yes"]
    assert strip_on_option(args) == ["build", "my.yml", "--yes"]


def test_strip_on_no_on_option_unchanged() -> None:
    args = ["build", "my.yml", "--yes"]
    assert strip_on_option(args) == args


def test_strip_on_leaves_other_tokens_intact() -> None:
    args = ["build", "--machine", "imx8", "--on", "pc2", "my.yml"]
    assert strip_on_option(args) == ["build", "--machine", "imx8", "my.yml"]


def test_strip_on_equals_leaves_other_tokens_intact() -> None:
    args = ["build", "--machine", "imx8", "--on=pc2", "my.yml"]
    assert strip_on_option(args) == ["build", "--machine", "imx8", "my.yml"]


# ---------------------------------------------------------------------------
# build_remote_script
# ---------------------------------------------------------------------------


def test_remote_script_sccache_off_default() -> None:
    script = build_remote_script(["build", "my.yml"], Path("/home/tiamarin/ws"), sccache_off=True)
    lines = script.splitlines()
    assert lines[0] == f"cd {shlex.quote('/home/tiamarin/ws')}"
    assert lines[-1] == "exec env BAKAR_SCCACHE_DIST=0 bakar build my.yml"


def test_remote_script_sccache_on_omits_token() -> None:
    script = build_remote_script(["build", "my.yml"], Path("/home/tiamarin/ws"), sccache_off=False)
    assert "BAKAR_SCCACHE_DIST=0" not in script
    assert script.splitlines()[-1] == "exec env bakar build my.yml"


def test_remote_script_never_uses_bash_lc() -> None:
    script = build_remote_script(["build", "my.yml"], Path("/tmp/ws"), sccache_off=True)
    assert "bash -lc" not in script


def test_remote_script_no_bare_name_value_prefix() -> None:
    # The env assignment must live behind env(1), never as a bare shell prefix.
    script = build_remote_script(["build", "my.yml"], Path("/tmp/ws"), sccache_off=True)
    exec_line = script.splitlines()[-1]
    assert exec_line.startswith("exec env ")
    assert not exec_line.startswith("BAKAR_SCCACHE_DIST=0")


def test_remote_script_quotes_cwd_with_spaces() -> None:
    script = build_remote_script(["build"], Path("/home/tia marin/ws"), sccache_off=True)
    assert script.splitlines()[0] == "cd '/home/tia marin/ws'"


def test_remote_script_shlex_joins_argv() -> None:
    script = build_remote_script(["build", "kas/my file.yml"], Path("/tmp/ws"), sccache_off=True)
    assert "'kas/my file.yml'" in script.splitlines()[-1]


# ---------------------------------------------------------------------------
# assert_safe_workspace
# ---------------------------------------------------------------------------


def test_assert_safe_workspace_accepts_absolute_nested_path() -> None:
    # Should not raise.
    assert_safe_workspace(Path("/home/tiamarin/repos/work/peridio-scarthgap-build"))


def test_assert_safe_workspace_rejects_relative() -> None:
    with pytest.raises(ValueError):
        assert_safe_workspace(Path("relative/path"))


def test_assert_safe_workspace_rejects_empty() -> None:
    with pytest.raises(ValueError):
        assert_safe_workspace(Path(""))


def test_assert_safe_workspace_rejects_root() -> None:
    with pytest.raises(ValueError):
        assert_safe_workspace(Path("/"))


def test_assert_safe_workspace_rejects_home() -> None:
    with pytest.raises(ValueError):
        assert_safe_workspace(Path.home())
