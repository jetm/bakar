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
    strip_dispatch_options,
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
        "build/",
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
# strip_dispatch_options
# ---------------------------------------------------------------------------


def test_strip_dispatch_two_token_form() -> None:
    args = ["build", "my.yml", "--on", "pc2"]
    assert strip_dispatch_options(args) == ["build", "my.yml"]


def test_strip_dispatch_equals_form() -> None:
    args = ["build", "my.yml", "--on=pc2"]
    assert strip_dispatch_options(args) == ["build", "my.yml"]


def test_strip_dispatch_removes_yes_and_short_y() -> None:
    # --yes / -y are dispatch-only; they must never reach the remote build.
    args = ["build", "--on", "pc2", "--yes", "-y", "my.yml"]
    assert strip_dispatch_options(args) == ["build", "my.yml"]


def test_strip_dispatch_no_dispatch_option_unchanged() -> None:
    args = ["build", "my.yml", "--machine", "imx8"]
    assert strip_dispatch_options(args) == args


def test_strip_dispatch_leaves_other_tokens_intact() -> None:
    args = ["build", "--machine", "imx8", "--on", "pc2", "my.yml"]
    assert strip_dispatch_options(args) == ["build", "--machine", "imx8", "my.yml"]


def test_strip_dispatch_equals_leaves_other_tokens_intact() -> None:
    args = ["build", "--machine", "imx8", "--on=pc2", "my.yml"]
    assert strip_dispatch_options(args) == ["build", "--machine", "imx8", "my.yml"]


# ---------------------------------------------------------------------------
# build_remote_script
# ---------------------------------------------------------------------------


def test_remote_script_sccache_off_default() -> None:
    script = build_remote_script(["build", "my.yml"], Path("/home/tiamarin/ws"), sccache_off=True)
    lines = script.splitlines()
    assert lines[0] == f"cd {shlex.quote('/home/tiamarin/ws')} || exit 1"
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
    assert script.splitlines()[0] == "cd '/home/tia marin/ws' || exit 1"


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


# ---------------------------------------------------------------------------
# Orchestration: check_host_reachable / confirm_destructive_sync /
# dispatch_remote_build  (mocked subprocess, no live host)
# ---------------------------------------------------------------------------


class _Result:
    """Stand-in for a completed ``subprocess.run`` result."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class _FakeStdin:
    """Captures the script written to a fake ssh stdin (StringIO discards on close)."""

    def __init__(self) -> None:
        self.buffer = ""

    def write(self, s: str) -> None:
        self.buffer += s

    def close(self) -> None:
        pass


class _FakeProc:
    """Stand-in for the ``ssh <host> bash -s`` streaming ``Popen``."""

    def __init__(self, lines: list[str], rc: int) -> None:
        self.stdin = _FakeStdin()
        self.stdout = list(lines)
        self._rc = rc

    def wait(self) -> int:
        return self._rc


class FakeSubprocess:
    """Records every run/Popen call and dispatches a canned result per argv."""

    PIPE = "PIPE"
    STDOUT = "STDOUT"

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.reachable_rc = 0
        self.rsync_rc = 0
        self.dry_rsync_rc = 0
        self.find_stdout = ""
        self.popen_lines: list[str] = []
        self.popen_rc = 0
        self.last_proc: _FakeProc | None = None

    def run(self, argv, **kwargs) -> _Result:
        argv = list(argv)
        self.calls.append(("run", argv))
        if argv[0] == "ssh" and argv[-1] == "true":
            return _Result(self.reachable_rc)
        if argv[0] == "rsync" and "-n" in argv:
            preview = "itemized preview line\n" if self.dry_rsync_rc == 0 else ""
            return _Result(self.dry_rsync_rc, stdout=preview)
        if argv[0] == "rsync":
            return _Result(self.rsync_rc)
        if argv[0] == "ssh" and "find" in argv[-1]:
            return _Result(0, stdout=self.find_stdout)
        return _Result(0)

    def Popen(self, argv, **kwargs) -> _FakeProc:  # noqa: N802
        self.calls.append(("Popen", list(argv)))
        self.last_proc = _FakeProc(self.popen_lines, self.popen_rc)
        return self.last_proc


from bakar.steps import remote_dispatch as rd  # noqa: E402


@pytest.fixture
def fake_sp(monkeypatch: pytest.MonkeyPatch) -> FakeSubprocess:
    fake = FakeSubprocess()
    monkeypatch.setattr(rd, "subprocess", fake)
    return fake


def _run_call_argvs(fake: FakeSubprocess) -> list[list[str]]:
    return [argv for kind, argv in fake.calls if kind == "run"]


def _real_rsync_index(fake: FakeSubprocess) -> int:
    for i, (kind, argv) in enumerate(fake.calls):
        if kind == "run" and argv[0] == "rsync" and "-n" not in argv:
            return i
    return -1


def _index_of(fake: FakeSubprocess, predicate) -> int:
    for i, (kind, argv) in enumerate(fake.calls):
        if predicate(kind, argv):
            return i
    return -1


# --- check_host_reachable ---------------------------------------------------


def test_check_host_reachable_true_on_zero(fake_sp: FakeSubprocess) -> None:
    fake_sp.reachable_rc = 0
    assert rd.check_host_reachable(HOST) is True
    assert _run_call_argvs(fake_sp)[0] == ["ssh", "-o", "BatchMode=yes", HOST, "true"]


def test_check_host_reachable_false_on_nonzero(fake_sp: FakeSubprocess) -> None:
    fake_sp.reachable_rc = 255
    assert rd.check_host_reachable(HOST) is False


# --- confirm_destructive_sync -----------------------------------------------


def test_confirm_assume_yes_returns_true_without_prompt(
    fake_sp: FakeSubprocess, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a, **k):
        raise AssertionError("typer.confirm must not be called under assume_yes")

    monkeypatch.setattr(rd.typer, "confirm", _boom)
    assert rd.confirm_destructive_sync(WS, HOST, assume_yes=True) is True
    # The dry-run preview must have been produced first.
    assert any(argv[0] == "rsync" and "-n" in argv for argv in _run_call_argvs(fake_sp))


def test_confirm_prompt_answer_forwarded(fake_sp: FakeSubprocess, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rd.typer, "confirm", lambda *a, **k: False)
    assert rd.confirm_destructive_sync(WS, HOST, assume_yes=False) is False
    monkeypatch.setattr(rd.typer, "confirm", lambda *a, **k: True)
    assert rd.confirm_destructive_sync(WS, HOST, assume_yes=False) is True


# --- dispatch_remote_build: guards and ordering -----------------------------


def test_dispatch_unreachable_aborts_before_rsync(fake_sp: FakeSubprocess) -> None:
    fake_sp.reachable_rc = 255
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc != 0
    # No rsync (dry-run or real) and no remote Popen may run.
    assert not any(argv[0] == "rsync" for _, argv in fake_sp.calls)
    assert not any(kind == "Popen" for kind, _ in fake_sp.calls)


def test_dispatch_declined_confirm_aborts_before_real_rsync(
    fake_sp: FakeSubprocess, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rd.typer, "confirm", lambda *a, **k: False)
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=False)
    assert rc != 0
    # A dry-run preview may run (inside confirm), but the real rsync must not.
    assert _real_rsync_index(fake_sp) == -1
    assert not any(kind == "Popen" for kind, _ in fake_sp.calls)


def test_dispatch_strict_ordering(fake_sp: FakeSubprocess) -> None:
    fake_sp.popen_rc = 0
    fake_sp.find_stdout = "1699999999.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-235959\n"
    rd.dispatch_remote_build(HOST, WS, WS, ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=True)
    reach_i = _index_of(fake_sp, lambda k, a: k == "run" and a[-1] == "true")
    dry_i = _index_of(fake_sp, lambda k, a: k == "run" and a[0] == "rsync" and "-n" in a)
    real_i = _real_rsync_index(fake_sp)
    popen_i = _index_of(fake_sp, lambda k, a: k == "Popen")
    assert -1 < reach_i < dry_i < real_i < popen_i


def test_dispatch_rsync_failure_skips_remote_build(fake_sp: FakeSubprocess) -> None:
    fake_sp.rsync_rc = 23
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc == 23
    assert not any(kind == "Popen" for kind, _ in fake_sp.calls)


# --- dispatch_remote_build: exit propagation and script construction --------


@pytest.mark.parametrize("remote_rc", [0, 1, 2, 42])
def test_dispatch_propagates_remote_exit(fake_sp: FakeSubprocess, remote_rc: int) -> None:
    fake_sp.popen_rc = remote_rc
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-000000\n"
    fake_sp.popen_lines = ["Run `bakar triage 20260716-000000` for details.\n"] if remote_rc else []
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc == remote_rc


def test_dispatch_script_strips_on_and_sets_sccache_off(fake_sp: FakeSubprocess) -> None:
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-010101\n"
    rd.dispatch_remote_build(
        HOST, WS, Path("/home/tiamarin/ws"), ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=True
    )
    script = fake_sp.last_proc.stdin.buffer
    # ssh bash -s stdin, never bash -lc.
    assert ("Popen", ["ssh", HOST, "bash", "-s"]) in fake_sp.calls
    assert "bash -lc" not in script
    # --on stripped, sccache forced off.
    assert "--on" not in script
    assert "exec env BAKAR_SCCACHE_DIST=0 bakar build my.yml" in script


def test_dispatch_sccache_dist_opt_in_omits_env_token(fake_sp: FakeSubprocess) -> None:
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-020202\n"
    rd.dispatch_remote_build(
        HOST, WS, Path("/home/tiamarin/ws"), ["build", "my.yml", "--on", HOST], sccache_dist=True, assume_yes=True
    )
    script = fake_sp.last_proc.stdin.buffer
    assert "BAKAR_SCCACHE_DIST=0" not in script
    assert "exec env bakar build my.yml" in script


# --- dispatch_remote_build: run-id surfacing --------------------------------


def test_dispatch_run_id_from_failure_stream(fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]) -> None:
    fake_sp.popen_rc = 1
    fake_sp.popen_lines = ["some build output\n", "Run `bakar triage 20260716-120000` for details.\n"]
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc == 1
    out = capsys.readouterr().out
    assert "20260716-120000" in out
    assert f"ssh {HOST} bakar triage 20260716-120000" in out
    # A failure must NOT trigger the newest-run-dir find discovery.
    assert not any(kind == "run" and "find" in argv[-1] for kind, argv in fake_sp.calls)


def test_dispatch_run_id_from_success_discovery(fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]) -> None:
    fake_sp.popen_rc = 0
    fake_sp.popen_lines = ["build succeeded\n"]
    fake_sp.find_stdout = "1699999999.5 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-235959\n"
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "20260716-235959" in out
    assert f"ssh {HOST} bakar triage 20260716-235959" in out
    # Success path performs the discovery ssh(find).
    assert any(kind == "run" and "find" in argv[-1] for kind, argv in fake_sp.calls)


def test_confirm_failed_preview_aborts(fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]) -> None:
    # A failed dry-run must abort - never confirm rsync --delete (even under
    # --yes) blind to what it would remove.
    fake_sp.dry_rsync_rc = 5
    assert rd.confirm_destructive_sync(WS, HOST, assume_yes=True) is False
    out = capsys.readouterr().out
    assert "preview failed" in out
    assert "rsync exit 5" in out


def test_dispatch_failure_without_triage_falls_back_to_discovery(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    # On failure, if the triage-hint line is absent from the stream (e.g. lost to
    # Rich's non-TTY line-wrap), the run-id is recovered via newest-run-dir discovery.
    fake_sp.popen_rc = 1
    fake_sp.popen_lines = ["some build output with no triage hint\n"]
    fake_sp.find_stdout = "1699999999.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-333333\n"
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc == 1
    out = capsys.readouterr().out
    assert "20260716-333333" in out
    # Discovery ran because the stream did not yield the run-id.
    assert any(kind == "run" and "find" in argv[-1] for kind, argv in fake_sp.calls)


def test_dispatch_unsafe_workspace_aborts_cleanly(fake_sp: FakeSubprocess) -> None:
    # An unsafe workspace (filesystem root) must abort with exit 1 via a caught
    # ValueError - no traceback, and no ssh/rsync touched.
    rc = rd.dispatch_remote_build(
        HOST, Path("/"), Path("/"), ["build", "--on", HOST], sccache_dist=False, assume_yes=True
    )
    assert rc == 1
    assert not fake_sp.calls
