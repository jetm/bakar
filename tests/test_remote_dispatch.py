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
        "/build/",
        "/build-*/",
        "/*/build/",
        "/ccache/",
        "**/tmp/",
        "**/sstate-cache/",
        "**/downloads/",
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


def test_workspace_root_outputs_are_anchored() -> None:
    # Anchored to the transfer root with a leading '/', so a same-named source
    # dir at depth (e.g. oe-core's meta/recipes-devtools/ccache/) is NOT dropped.
    assert "/ccache/" in RSYNC_EXCLUDES
    assert "ccache/" not in RSYNC_EXCLUDES
    assert "/build/" in RSYNC_EXCLUDES
    assert "build/" not in RSYNC_EXCLUDES
    assert "build-*/" not in RSYNC_EXCLUDES


def test_vestigial_bakar_runs_dropped() -> None:
    # Runs live at <bsp_root>/build/runs/, not .bakar/runs/, so the old pattern
    # matched nothing bakar produces.
    assert ".bakar/runs/" not in RSYNC_EXCLUDES


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


def test_rsync_argv_extra_excludes_appended_and_delete_retained() -> None:
    # Remote-only dirs are threaded in as anchored excludes so --delete cannot
    # wipe a checkout the local side does not carry.
    argv = build_rsync_argv(WS, HOST, extra_excludes=("openembedded-core",))
    assert "--exclude=/openembedded-core/" in argv
    assert "--delete" in argv


def test_rsync_argv_extra_excludes_default_adds_nothing() -> None:
    base = build_rsync_argv(WS, HOST)
    with_default = build_rsync_argv(WS, HOST, extra_excludes=())
    assert base == with_default


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


def test_strip_dispatch_short_cluster_drops_only_y() -> None:
    # `-nky` is click-parsed as `-n -k -y`; the clustered `y` must not ride to
    # the remote, but `-nk` must survive.
    assert strip_dispatch_options(["build", "-nky", "my.yml"]) == ["build", "-nk", "my.yml"]


def test_strip_dispatch_cluster_of_only_y_removed() -> None:
    # A cluster that reduces to a bare "-" is dropped entirely.
    assert strip_dispatch_options(["build", "-yy", "my.yml"]) == ["build", "my.yml"]


def test_strip_dispatch_non_y_cluster_untouched() -> None:
    assert strip_dispatch_options(["build", "-nk", "my.yml"]) == ["build", "-nk", "my.yml"]


# ---------------------------------------------------------------------------
# build_remote_script
# ---------------------------------------------------------------------------


def test_remote_script_sccache_off_default() -> None:
    script = build_remote_script(["build", "my.yml"], Path("/home/tiamarin/ws"), {}, sccache_off=True)
    lines = script.splitlines()
    assert lines[0] == f"cd {shlex.quote('/home/tiamarin/ws')} || exit 1"
    assert lines[-1] == "exec env BAKAR_SCCACHE_DIST=0 bakar build my.yml"


def test_remote_script_sccache_on_omits_token() -> None:
    script = build_remote_script(["build", "my.yml"], Path("/home/tiamarin/ws"), {}, sccache_off=False)
    assert "BAKAR_SCCACHE_DIST=0" not in script
    assert script.splitlines()[-1] == "exec env bakar build my.yml"


def test_remote_script_never_uses_bash_lc() -> None:
    script = build_remote_script(["build", "my.yml"], Path("/tmp/ws"), {}, sccache_off=True)
    assert "bash -lc" not in script


def test_remote_script_no_bare_name_value_prefix() -> None:
    # The env assignment must live behind env(1), never as a bare shell prefix.
    script = build_remote_script(["build", "my.yml"], Path("/tmp/ws"), {}, sccache_off=True)
    exec_line = script.splitlines()[-1]
    assert exec_line.startswith("exec env ")
    assert not exec_line.startswith("BAKAR_SCCACHE_DIST=0")


def test_remote_script_quotes_cwd_with_spaces() -> None:
    script = build_remote_script(["build"], Path("/home/tia marin/ws"), {}, sccache_off=True)
    assert script.splitlines()[0] == "cd '/home/tia marin/ws' || exit 1"


def test_remote_script_shlex_joins_argv() -> None:
    script = build_remote_script(["build", "kas/my file.yml"], Path("/tmp/ws"), {}, sccache_off=True)
    assert "'kas/my file.yml'" in script.splitlines()[-1]


def test_remote_script_forwards_env_as_sorted_tokens() -> None:
    # BAKAR_*/KAS_* env is forwarded as sorted, shlex-quoted NAME=value tokens
    # after `env`, so the remote resolves the same build as the local one.
    env = {"KAS_CONTAINER_IMAGE": "img", "BAKAR_MACHINE": "imx8mp"}
    script = build_remote_script(["build"], Path("/tmp/ws"), env, sccache_off=False)
    exec_line = script.splitlines()[-1]
    assert exec_line == "exec env BAKAR_MACHINE=imx8mp KAS_CONTAINER_IMAGE=img bakar build"


def test_remote_script_sccache_off_token_wins_over_forwarded() -> None:
    # A forwarded BAKAR_SCCACHE_DIST=1 is overridden by the appended =0 (env(1)
    # applies tokens left-to-right, last wins), so the token must come LAST.
    env = {"BAKAR_SCCACHE_DIST": "1"}
    script = build_remote_script(["build"], Path("/tmp/ws"), env, sccache_off=True)
    exec_line = script.splitlines()[-1]
    assert exec_line == "exec env BAKAR_SCCACHE_DIST=1 BAKAR_SCCACHE_DIST=0 bakar build"
    assert exec_line.index("BAKAR_SCCACHE_DIST=1") < exec_line.index("BAKAR_SCCACHE_DIST=0")


def test_remote_script_emits_dispatch_start_marker() -> None:
    # The dispatch-start marker is echoed before exec so it streams back and
    # fences run-id discovery against a stale previous run.
    script = build_remote_script(["build"], Path("/tmp/ws"), {}, sccache_off=True)
    lines = script.splitlines()
    assert lines[1] == 'echo "BAKAR_DISPATCH_START=$(date -u +%Y%m%d-%H%M%S)"'


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
# Orchestration: preflight_remote / confirm_destructive_sync /
# dispatch_remote_build  (mocked subprocess, no live host)
# ---------------------------------------------------------------------------


class _Result:
    """Stand-in for a completed ``subprocess.run`` result."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeStdin:
    """Captures the script written to a fake ssh stdin (StringIO discards on close)."""

    def __init__(self, broken: bool = False) -> None:
        self.buffer = ""
        self._broken = broken

    def write(self, s: str) -> None:
        if self._broken:
            raise BrokenPipeError("fake ssh dropped the connection")
        self.buffer += s

    def close(self) -> None:
        pass


class _FakeProc:
    """Stand-in for the ``ssh <host> bash -s`` streaming ``Popen``."""

    def __init__(self, lines: list[str], rc: int, broken: bool = False) -> None:
        self.stdin = _FakeStdin(broken=broken)
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
        self.reachable_stderr = ""
        self.remote_version = ""
        self.local_version = ""
        self.rsync_rc = 0
        self.dry_rsync_rc = 0
        self.dry_rsync_stdout: str | None = None
        self.find_stdout = ""
        self.popen_lines: list[str] = []
        self.popen_rc = 0
        self.popen_kwargs: dict = {}
        self.broken_pipe = False
        self.last_proc: _FakeProc | None = None

    def run(self, argv, **kwargs) -> _Result:
        argv = list(argv)
        self.calls.append(("run", argv))
        # Preflight: ssh -o BatchMode=yes <host> bash -s (probe over non-login bash).
        if argv[0] == "ssh" and argv[-1] == "-s":
            return _Result(self.reachable_rc, stdout=self.remote_version, stderr=self.reachable_stderr)
        if argv[0] == "bakar" and "--version" in argv:
            return _Result(0, stdout=self.local_version)
        if argv[0] == "rsync" and "-n" in argv:
            if self.dry_rsync_stdout is not None:
                preview = self.dry_rsync_stdout if self.dry_rsync_rc == 0 else ""
            else:
                preview = "itemized preview line\n" if self.dry_rsync_rc == 0 else ""
            return _Result(self.dry_rsync_rc, stdout=preview)
        if argv[0] == "rsync":
            return _Result(self.rsync_rc)
        if argv[0] == "ssh" and "find" in argv[-1]:
            return _Result(0, stdout=self.find_stdout)
        return _Result(0)

    def Popen(self, argv, **kwargs) -> _FakeProc:  # noqa: N802
        self.calls.append(("Popen", list(argv)))
        self.popen_kwargs = kwargs
        self.last_proc = _FakeProc(self.popen_lines, self.popen_rc, broken=self.broken_pipe)
        return self.last_proc


from bakar.steps import remote_dispatch as rd  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_bakar_kas_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear ambient BAKAR_*/KAS_* env so forwarded-env assertions are deterministic."""
    import os

    for key in list(os.environ):
        if key.startswith(("BAKAR_", "KAS_")):
            monkeypatch.delenv(key, raising=False)


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


# --- preflight_remote -------------------------------------------------------


def test_preflight_true_when_bakar_present(fake_sp: FakeSubprocess) -> None:
    fake_sp.reachable_rc = 0
    fake_sp.remote_version = "bakar 1.2.3"
    ok, detail = rd.preflight_remote(HOST)
    assert ok is True
    assert detail == "bakar 1.2.3"
    # Probe runs over the non-login bash the build itself uses, with BatchMode.
    assert _run_call_argvs(fake_sp)[0] == ["ssh", "-o", "BatchMode=yes", HOST, "bash", "-s"]


def test_preflight_false_when_unreachable_surfaces_stderr(fake_sp: FakeSubprocess) -> None:
    fake_sp.reachable_rc = 255
    fake_sp.reachable_stderr = "Permission denied (publickey)."
    ok, detail = rd.preflight_remote(HOST)
    assert ok is False
    assert detail == "Permission denied (publickey)."


def test_preflight_false_when_bakar_missing(fake_sp: FakeSubprocess) -> None:
    fake_sp.reachable_rc = 127
    ok, detail = rd.preflight_remote(HOST)
    assert ok is False
    assert detail is not None and "not found" in detail


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
    reach_i = _index_of(fake_sp, lambda k, a: k == "run" and a[0] == "ssh" and a[-1] == "-s")
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
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
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
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "20260716-235959" in out
    assert f"ssh {HOST} bakar triage 20260716-235959" in out
    # Success path performs the discovery ssh(find).
    assert any(kind == "run" and "find" in argv[-1] for kind, argv in fake_sp.calls)


def test_confirm_failed_preview_aborts(fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]) -> None:
    # A failed dry-run must abort - never confirm rsync --delete (even under
    # --yes) blind to what it would remove.
    fake_sp.dry_rsync_rc = 5
    assert rd.confirm_destructive_sync(WS, HOST, assume_yes=True) is False
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
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
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
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


# --- C6: dispatch-start fence -----------------------------------------------


def test_dispatch_discards_stale_run_id_before_dispatch_start(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    # The build failed before creating its own run dir; discovery finds a
    # PREVIOUS run whose id predates the streamed dispatch-start marker, so it
    # must be discarded rather than surfaced as a misleading stale id.
    fake_sp.popen_rc = 1
    fake_sp.popen_lines = ["BAKAR_DISPATCH_START=20260716-120000\n", "config error, no run dir\n"]
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-000000\n"
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc == 1
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "no remote run dir was created" in out
    # The stale previous run-id is NOT surfaced as this build's run.
    assert f"ssh {HOST} bakar triage 20260716-000000" not in out


def test_dispatch_keeps_run_id_after_dispatch_start(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    # A discovered id NEWER than the dispatch-start marker is this build's run.
    fake_sp.popen_rc = 0
    fake_sp.popen_lines = ["BAKAR_DISPATCH_START=20260716-120000\n", "build succeeded\n"]
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-235959\n"
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc == 0
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "20260716-235959" in out


# --- C3: env forwarding -----------------------------------------------------


def test_dispatch_forwards_bakar_kas_env(fake_sp: FakeSubprocess, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_MACHINE", "imx8mp")
    monkeypatch.setenv("KAS_CONTAINER_IMAGE", "img")
    monkeypatch.setenv("UNRELATED_VAR", "x")
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-000000\n"
    rd.dispatch_remote_build(
        HOST, WS, Path("/home/tiamarin/ws"), ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=True
    )
    assert fake_sp.last_proc is not None
    exec_line = fake_sp.last_proc.stdin.buffer.splitlines()[-1]
    assert "BAKAR_MACHINE=imx8mp" in exec_line
    assert "KAS_CONTAINER_IMAGE=img" in exec_line
    assert "UNRELATED_VAR" not in exec_line
    # The forced sccache-off token is appended LAST so it wins over forwarded env.
    assert exec_line.index("BAKAR_MACHINE=imx8mp") < exec_line.index("BAKAR_SCCACHE_DIST=0")


# --- C7: decode robustness --------------------------------------------------


def test_dispatch_popen_uses_replace_decode(fake_sp: FakeSubprocess) -> None:
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-000000\n"
    rd.dispatch_remote_build(HOST, WS, WS, ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert fake_sp.popen_kwargs.get("encoding") == "utf-8"
    assert fake_sp.popen_kwargs.get("errors") == "replace"


# --- C8: Ctrl-C story -------------------------------------------------------


def test_dispatch_keyboard_interrupt_returns_130(
    fake_sp: FakeSubprocess, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _interrupt(host: str, script: str):
        raise KeyboardInterrupt

    monkeypatch.setattr(rd, "_stream_remote_build", _interrupt)
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc == 130
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "does not stop the remote build" in out
    assert f"ssh {HOST} bakar stop" in out
    assert f"ssh {HOST} bakar triage" in out


# --- C10: broken pipe on stdin.write ----------------------------------------


def test_dispatch_broken_pipe_returns_255_cleanly(fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]) -> None:
    fake_sp.broken_pipe = True
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc == 255
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert f"connection to {HOST} lost" in out


# --- C12b: preflight stderr surfaced ----------------------------------------


def test_dispatch_preflight_failure_surfaces_stderr(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_sp.reachable_rc = 255
    fake_sp.reachable_stderr = "Host key verification failed."
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc == 1
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "Host key verification failed." in out
    assert not any(argv[0] == "rsync" for _, argv in fake_sp.calls)


# --- C12c: hyphen-host injection guard ---------------------------------------


def test_dispatch_rejects_hyphen_prefixed_host(fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]) -> None:
    rc = rd.dispatch_remote_build(
        "-oProxyCommand=evil", WS, WS, ["build", "--on", "-x"], sccache_dist=False, assume_yes=True
    )
    assert rc == 1
    # Rejected before any ssh/rsync is spawned.
    assert fake_sp.calls == []
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "must not begin with" in out


# --- C4: bakar identity parity gate -----------------------------------------


def test_dispatch_aborts_on_mismatch_without_yes(fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]) -> None:
    """A bakar id/version mismatch aborts before any rsync or build."""
    fake_sp.reachable_rc = 0
    fake_sp.remote_version = "bakar 0.22.0 (deadbeef0000)"
    fake_sp.local_version = "bakar 0.22.0 (0123456789ab)"
    rc = rd.dispatch_remote_build(
        HOST, WS, Path("/home/tiamarin/ws"), ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=False
    )
    assert rc == 1
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "bakar mismatch" in out
    assert "rsync" not in out  # aborted before the destructive sync


def test_dispatch_proceeds_on_mismatch_with_yes(fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]) -> None:
    """--yes overrides the mismatch abort and proceeds with a loud note."""
    fake_sp.reachable_rc = 0
    fake_sp.remote_version = "bakar 2.0.0"
    fake_sp.local_version = "bakar 1.0.0"
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-000000\n"
    rc = rd.dispatch_remote_build(
        HOST, WS, Path("/home/tiamarin/ws"), ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=True
    )
    assert rc == 0
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "bakar mismatch" in out
    assert "proceeding despite" in out
    assert "1.0.0" in out
    assert "2.0.0" in out


def test_package_identity_stable_and_content_sensitive(tmp_path: Path) -> None:
    """The id is a deterministic 12-hex digest that moves when a file changes."""
    from bakar import package_identity

    first = package_identity()
    assert first == package_identity()
    assert len(first) == 12
    assert all(c in "0123456789abcdef" for c in first)

    # A byte-different package tree yields a different id (the drift the gate catches).
    pkg = tmp_path / "pkg"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "a.py").write_text("x = 1\n")
    (pkg / "sub" / "o.bbclass").write_text("FOO = 'a'\n")
    import hashlib

    def _id(root: Path) -> str:
        d = hashlib.sha256()
        for p in sorted(root.rglob("*")):
            if p.is_dir():
                continue
            d.update(p.relative_to(root).as_posix().encode())
            d.update(b"\0")
            d.update(p.read_bytes())
            d.update(b"\0")
        return d.hexdigest()[:12]

    before = _id(pkg)
    (pkg / "sub" / "o.bbclass").write_text("FOO = 'b'\n")
    assert _id(pkg) != before


# --- S5: preview filters to deletions ---------------------------------------


def test_confirm_preview_filters_to_deletions(fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]) -> None:
    fake_sp.dry_rsync_stdout = (
        "*deleting stale/file1\n*deleting stale/file2\n>f+++++++++ new/file\ncd+++++++++ new/dir/\n"
    )
    assert rd.confirm_destructive_sync(WS, HOST, assume_yes=True) is True
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "*deleting stale/file1" in out
    assert "*deleting stale/file2" in out
    assert "2 files to create/update" in out
    # The full creation itemization is summarized, not dumped line-by-line.
    assert ">f+++++++++ new/file" not in out


# --- remote-only dir computation (preserve remote checkouts from --delete) ---


class _ListingSubprocess:
    """Minimal subprocess stand-in returning a canned ssh-listing result."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self._result = _Result(returncode, stdout=stdout)
        self.calls: list[list[str]] = []

    def run(self, argv, **kwargs) -> _Result:
        self.calls.append(list(argv))
        return self._result


def test_remote_only_dirs_returns_remote_minus_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Local carries meta-avocado/ and bitbake/; the remote also has an
    # openembedded-core/ checkout the local side lacks -> only that is remote-only.
    (tmp_path / "meta-avocado").mkdir()
    (tmp_path / "bitbake").mkdir()
    fake = _ListingSubprocess(0, "meta-avocado/\nbitbake/\nopenembedded-core/\n")
    monkeypatch.setattr(rd, "subprocess", fake)
    assert rd._remote_only_dirs(tmp_path, HOST) == ["openembedded-core"]
    # Listing is over BatchMode ssh so a missing key fails fast instead of hanging.
    assert fake.calls and fake.calls[0][:3] == ["ssh", "-o", "BatchMode=yes"]


def test_remote_only_dirs_ssh_failure_yields_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A failed listing must not crash the dispatch; fall back to no extra excludes.
    (tmp_path / "meta-avocado").mkdir()
    fake = _ListingSubprocess(255, "")
    monkeypatch.setattr(rd, "subprocess", fake)
    assert rd._remote_only_dirs(tmp_path, HOST) == []
