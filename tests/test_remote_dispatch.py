"""Unit tests for bakar.steps.remote_dispatch pure builders.

These cover the host-free builders only: the exclude set, the rsync argv
constructor, the ``--on`` stripper, the remote bash-script generator, and the
``rsync --delete`` workspace guard. Orchestration (ssh/rsync subprocess) is a
later task; nothing here touches a live host.
"""

from __future__ import annotations

import inspect
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from bakar import build_scope, observability
from bakar.steps.remote_dispatch import (
    RSYNC_EXCLUDES,
    assert_safe_workspace,
    build_remote_script,
    build_rsync_argv,
    remote_log_expr,
    strip_dispatch_options,
)

pytestmark = pytest.mark.unit

WS = Path("/home/tiamarin/repos/work/peridio-scarthgap-build")
HOST = "pc2"


def _bash_syntax_error(script: str) -> str | None:
    """Return bash's complaint about ``script``, or None when it parses.

    Every script this module generates is delivered to a REMOTE bash, so nothing
    local ever executes it and a substring assertion is the only thing standing
    between a generated script and the host. That is exactly how the stop script
    shipped as bash prose handed to a fish login shell: `"loginctl" in script`
    passes whatever interpreter the string is eventually fed to.
    """
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - bash is present on every supported host
        pytest.skip("bash is not installed")
    result = subprocess.run([bash, "-n"], input=script, text=True, capture_output=True, check=False)
    return None if result.returncode == 0 else (result.stderr.strip() or f"bash -n exit {result.returncode}")


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
    assert lines[1] == 'echo "BAKAR_DISPATCH_START=$(date +%Y%m%d-%H%M%S)"'


def test_dispatch_start_marker_uses_the_same_clock_as_run_ids() -> None:
    # The marker is string-compared against a run DIRECTORY NAME, and those are
    # named by RunLog.run_id from `datetime.now()` - the remote's LOCAL clock,
    # not UTC. So the marker has to read the same clock or the comparison spans
    # two of them.
    #
    # It used to say `date -u`. West of UTC that makes every run dir the remote
    # just created sort BELOW the marker, so a perfectly good build is discarded
    # as stale and reported as "no remote run dir was created - the build failed
    # before starting" while it is still running. Observed at UTC-6: run dir
    # 20260827-081347 against marker 20260827-141347.
    #
    # East of UTC the same bug fails the other way and is quieter: a genuinely
    # stale run dir sorts ABOVE the marker and gets surfaced as this build's.
    script = build_remote_script(["build"], Path("/tmp/ws"), {}, sccache_off=True)
    assert "date -u" not in script
    assert "$(date +%Y%m%d-%H%M%S)" in script

    # Pin the other half of the invariant: if run ids ever move to UTC, this
    # test should fail rather than let the two drift apart again.
    assert "datetime.now()" in inspect.getsource(observability.RunLogger)


# ---------------------------------------------------------------------------
# build_remote_script: detachment from the ssh session
# ---------------------------------------------------------------------------


def test_remote_script_detaches_the_build_from_the_ssh_session() -> None:
    # The build must NOT be a plain child of `ssh <host> bash -s`: when the
    # local dispatcher dies, sshd SIGHUPs the session and a child build dies
    # with it (observed: a 50-minute cryptsetup-var build killed by "Keyboard
    # Interrupt, closing down" / bitbake exit -15). Under a transient user unit
    # the build is reparented to the user manager and survives.
    script = build_remote_script(["build", "my.yml"], Path("/tmp/ws"), {}, sccache_off=True, unit="bakar-dispatch-u1")
    assert "systemd-run --user --unit=bakar-dispatch-u1" in script
    assert "--collect" in script
    assert "--same-dir" in script


def test_remote_script_emits_parseable_unit_and_log_markers() -> None:
    # The local side references the unit (to poll it and to stop it) and tails
    # the log, so both have to arrive over the launch stream in a parseable form.
    # printf with a LITERAL format, not an interpolating `echo "...=$unit"`: the
    # unit is public API and inside double quotes a `$(...)` in it would be
    # command-substituted on the remote.
    script = build_remote_script(["build"], Path("/tmp/ws"), {}, sccache_off=True, unit="bakar-dispatch-u2")
    log = remote_log_expr("bakar-dispatch-u2")
    assert "printf 'BAKAR_DISPATCH_UNIT=%s\\n' bakar-dispatch-u2" in script
    assert f"printf 'BAKAR_DISPATCH_LOG=%s\\n' {log}" in script


def test_remote_script_quotes_the_unit_everywhere_it_interpolates_it() -> None:
    # `unit` is public API and reaches a `systemd-run --unit=`, a printf argument
    # and a `>` redirect. Its siblings in the same function (cwd, env tokens,
    # remote_argv) are all shlex-quoted; leaving this one bare was the odd one
    # out, and only tests pass a hand-written unit today.
    evil = "u$(touch /tmp/pwned) x"
    script = build_remote_script(["build"], Path("/tmp/ws"), {}, sccache_off=True, unit=evil)
    assert f"--unit={shlex.quote(evil)}" in script
    assert f"printf 'BAKAR_DISPATCH_UNIT=%s\\n' {shlex.quote(evil)}" in script
    assert shlex.quote(f"{evil}.log") in script
    assert _bash_syntax_error(script) is None


def test_remote_script_records_the_build_exit_code_in_a_sentinel() -> None:
    # The unit is `--collect`ed away once it exits, so `systemctl show` cannot be
    # trusted to still hold the exit status. The wrapper writes it to a sentinel
    # file beside the log instead, which is also what ends the local follower.
    script = build_remote_script(["build"], Path("/tmp/ws"), {}, sccache_off=True, unit="bakar-dispatch-u3")
    log = remote_log_expr("bakar-dispatch-u3")
    assert f"{log}.rc" in script


def test_dispatch_log_lives_under_the_per_user_runtime_dir_not_tmp() -> None:
    # /tmp is drwxrwxrwt and the log path is disclosed in the unit's argv
    # (world-readable via /proc), while the `.rc` sentinel does not exist until
    # the build ENDS - so any other local user had the whole build duration to
    # `printf '0\n' > /tmp/<name>.log.rc` and make a failed build report success,
    # or to pre-place a symlink there for a truncate primitive.
    # $XDG_RUNTIME_DIR is 0700 and per-user.
    assert remote_log_expr("bakar-dispatch-u7").startswith('"$XDG_RUNTIME_DIR"/')
    script = build_remote_script(["build"], Path("/tmp/ws"), {}, sccache_off=True, unit="bakar-dispatch-u7")
    assert "/tmp/bakar-dispatch-u7.log" not in script
    # The unit's own bash writes the log, so it must resolve the same value the
    # follower does rather than betting on the user manager carrying it.
    assert '--setenv=XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR"' in script


def test_remote_script_forwards_session_env_the_transient_unit_would_lose() -> None:
    # A transient unit inherits the USER MANAGER's environment, not the ssh
    # session's. The old coupled `exec` form carried these silently; the detached
    # form dropped them just as silently, so an agent-forwarded ssh key or a
    # corporate proxy broke on the detached path only - i.e. exactly on the hosts
    # this feature targets.
    script = build_remote_script(["build"], Path("/tmp/ws"), {}, sccache_off=True, unit="bakar-dispatch-u8")
    for name in ("SSH_AUTH_SOCK", "https_proxy", "NO_PROXY", "SSL_CERT_FILE", "LC_ALL"):
        assert name in script
    # Guarded on non-empty: an unset var must be left alone, not turned into an
    # empty-string OVERRIDE (an empty https_proxy disables a proxy rather than
    # deferring to whatever the manager environment holds).
    assert 'if [ -n "${!v:-}" ]; then setenv+=("--setenv=$v=${!v}"); fi' in script
    assert '"${setenv[@]}"' in script


def test_remote_script_falls_back_to_exec_when_systemd_run_unavailable() -> None:
    # A remote with no systemd-run or no user runtime dir (WSL, a minimal
    # container) must still build - falling back to the old coupled `exec` form
    # rather than failing the dispatch. The fallback is the script's last line,
    # reached when the availability probe's `if` does not exec.
    script = build_remote_script(["build", "my.yml"], Path("/tmp/ws"), {}, sccache_off=True, unit="bakar-dispatch-u4")
    lines = script.splitlines()
    assert lines[-1] == "exec env BAKAR_SCCACHE_DIST=0 bakar build my.yml"
    assert lines[-2] == "fi"


def test_remote_script_probe_mirrors_the_local_availability_check() -> None:
    # The remote probe cannot call systemd_run_available() - it runs in bash on
    # another host - so it mirrors it. Pin the mirror: both check the binary,
    # XDG_RUNTIME_DIR, and then actually create a throwaway scope, because on
    # WSL and in minimal containers the first two pass while --user cannot reach
    # the manager bus. If the Python probe grows a fourth precondition this test
    # should fail rather than let the two drift apart.
    script = build_remote_script(["build"], Path("/tmp/ws"), {}, sccache_off=True, unit="bakar-dispatch-u5")
    # Sliced out of the module source rather than read off the attribute: the
    # suite-wide autouse fixture in conftest replaces systemd_run_available with
    # a `lambda: False`, so inspect.getsource on the attribute returns the stub.
    probe_src = inspect.getsource(build_scope).split("def systemd_run_available", 1)[1].split("\ndef ", 1)[0]
    for token in ("systemd-run", "XDG_RUNTIME_DIR"):
        assert token in probe_src
        assert token in script
    assert "--user --scope --quiet" in script


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
        # Second and later Popens are the detached-dispatch log follower; left
        # None every Popen replays the launch stream (the fallback path, which
        # only ever opens one).
        self.follow_lines: list[str] | None = None
        self.follow_rc = 0
        self.procs: list[_FakeProc] = []
        self.systemctl_stdout = ""
        self.systemctl_rc = 0
        self.stop_rc = 0
        # Every `subprocess.run` call's `input=` kwarg, positionally aligned with
        # `calls`. The scripts that matter are delivered over `bash -s` stdin, not
        # as an ssh argument, so argv alone no longer shows what was run.
        self.run_inputs: list[str | None] = []

    def run(self, argv, **kwargs) -> _Result:
        argv = list(argv)
        self.calls.append(("run", argv))
        stdin_script = kwargs.get("input")
        self.run_inputs.append(stdin_script)
        # Both the preflight probe and the stop script ride `ssh <host> bash -s`;
        # they are told apart by what is written to stdin, exactly as the remote
        # host would tell them apart.
        if argv[0] == "ssh" and argv[-1] == "-s":
            if stdin_script and "systemctl --user" in stdin_script:
                return _Result(self.stop_rc)
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
        if argv[0] == "ssh" and "systemctl" in argv[-1]:
            return _Result(self.systemctl_rc, stdout=self.systemctl_stdout)
        return _Result(0)

    def Popen(self, argv, **kwargs) -> _FakeProc:  # noqa: N802
        self.calls.append(("Popen", list(argv)))
        self.popen_kwargs = kwargs
        if self.procs and self.follow_lines is not None:
            proc = _FakeProc(self.follow_lines, self.follow_rc, broken=self.broken_pipe)
        else:
            proc = _FakeProc(self.popen_lines, self.popen_rc, broken=self.broken_pipe)
        self.procs.append(proc)
        self.last_proc = proc
        return proc


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


def _last_remote_stdin(fake: FakeSubprocess) -> str:
    """Return the last script delivered over an ``ssh ... bash -s`` stdin."""
    pairs = list(zip(_run_call_argvs(fake), fake.run_inputs, strict=True))
    for argv, stdin_script in reversed(pairs):
        if argv[0] == "ssh" and argv[-1] == "-s" and stdin_script:
            return stdin_script
    raise AssertionError("no script was delivered over ssh bash -s stdin")


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


# --- detached dispatch: the local side follows the log, it does not own it ---


_DETACHED_LAUNCH = [
    "BAKAR_DISPATCH_START=20260716-120000\n",
    "BAKAR_DISPATCH_UNIT=bakar-dispatch-20260716-120000-aabbcc\n",
    "BAKAR_DISPATCH_LOG=/tmp/bakar-dispatch-20260716-120000-aabbcc.log\n",
]


def test_dispatch_detached_launch_tails_the_remote_log(fake_sp: FakeSubprocess) -> None:
    # The launch ssh returns as soon as the unit is started, so the build's
    # output arrives over a SECOND ssh that tails the log. Losing that follower
    # costs the stream and nothing else - which is the whole point of detaching.
    fake_sp.popen_lines = _DETACHED_LAUNCH
    fake_sp.follow_lines = ["compiling\n", "BAKAR_DISPATCH_RC=0\n"]
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-235959\n"
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc == 0
    popens = [argv for kind, argv in fake_sp.calls if kind == "Popen"]
    assert len(popens) == 2
    follow_script = fake_sp.procs[1].stdin.buffer
    assert "tail -n +1 -F /tmp/bakar-dispatch-20260716-120000-aabbcc.log" in follow_script
    assert "bakar-dispatch-20260716-120000-aabbcc.service" in follow_script


def test_dispatch_detached_exit_code_comes_from_the_sentinel(fake_sp: FakeSubprocess) -> None:
    # The launch ssh exits 0 the moment the unit starts, so its exit code says
    # nothing about the build. The build's own code rides back in the follower's
    # BAKAR_DISPATCH_RC line, written from the remote rc sentinel.
    fake_sp.popen_lines = _DETACHED_LAUNCH
    fake_sp.popen_rc = 0
    fake_sp.follow_lines = ["boom\n", "BAKAR_DISPATCH_RC=42\n"]
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "my.yml", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc == 42


def test_dispatch_detached_rc_line_is_not_echoed_to_the_user(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    # The sentinel line is transport, not build output.
    fake_sp.popen_lines = _DETACHED_LAUNCH
    fake_sp.follow_lines = ["real build output\n", "BAKAR_DISPATCH_RC=0\n"]
    rd.dispatch_remote_build(HOST, WS, WS, ["build", "--on", HOST], sccache_dist=False, assume_yes=True)
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "real build output" in out
    assert "BAKAR_DISPATCH_RC" not in out


def test_dispatch_detached_lost_follower_says_the_build_survives(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    # A dropped link ends the follower with no sentinel line. That is NOT a build
    # failure, and reporting it as one would send someone hunting a build error
    # that never happened - so say plainly that the build is still running and
    # name the command that stops it.
    #
    # This used to assert `rc == 255`, which contradicted its own comment: 255 is
    # the exit code a remote build can genuinely produce, so a caller could not
    # tell "the build failed with 255" from "I lost the stream and do not know how
    # it ended". _DISPATCH_LOST_EXIT is outside that space.
    fake_sp.popen_lines = _DETACHED_LAUNCH
    fake_sp.follow_lines = ["partial output\n"]
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc == rd._DISPATCH_LOST_EXIT
    assert rc != 255
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "keeps running" in out
    assert f"bakar stop --on {HOST}" in out


def test_dispatch_lost_stream_does_not_narrate_a_build_failure(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    # _surface_run_id takes its `rc != 0` branch on a lost stream and, finding no
    # newer run dir, prints "no remote run dir was created - the build failed
    # before starting" about a build that is healthy and still running. A lost
    # stream must not reach it at all.
    fake_sp.popen_lines = _DETACHED_LAUNCH
    fake_sp.follow_lines = ["compiling\n"]
    fake_sp.find_stdout = ""
    rd.dispatch_remote_build(HOST, WS, WS, ["build", "--on", HOST], sccache_dist=False, assume_yes=True)
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "the build failed before starting" not in out
    assert "remote run-id" not in out


def test_follower_broken_pipe_is_a_lost_stream_not_a_build_failure(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    # The follower's OWN stdin write can break - the link drops between launching
    # the unit and attaching the tail. The build is already running at that point,
    # so this is the same lost-stream case as an absent sentinel, and the only
    # path that was previously untested.
    fake_sp.broken_pipe = True
    _rc, tail, finished = rd._follow_remote_log(HOST, "u", "/run/user/1000/u.log")
    assert finished is False
    assert tail == []
    _cap = capsys.readouterr()
    assert f"connection to {HOST} lost" in _cap.out + _cap.err


@pytest.mark.parametrize("sentinel_line", ["BAKAR_DISPATCH_LOST=1\n", "not-a-number\n"])
def test_dispatch_malformed_rc_sentinel_is_a_lost_stream(fake_sp: FakeSubprocess, sentinel_line: str) -> None:
    # A truncated or garbage `.rc` means the build's exit status is UNKNOWN. It
    # used to split two ways, both wrong: an empty sentinel defaulted to 255 and
    # was reported as a build that failed with 255, while garbage produced no
    # match and was reported as a lost connection. Neither is a build result, so
    # both take the lost-stream path.
    fake_sp.popen_lines = _DETACHED_LAUNCH
    fake_sp.follow_lines = ["output\n", sentinel_line]
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc == rd._DISPATCH_LOST_EXIT


def test_follow_script_treats_an_activating_unit_as_live() -> None:
    # systemd-run returns when the job is ENQUEUED, so the first poll routinely
    # lands on a unit that has not reached `active` yet. `is-active --quiet` is
    # non-zero for `activating`, which declared a healthy build lost about four
    # seconds after dispatch. _running_dispatch_units already counts it as live
    # for the same reason.
    script = rd.build_follow_script("u", "/run/user/1000/u.log")
    assert "activating" in script
    assert "is-active --quiet" not in script


def test_follow_script_does_not_conclude_gone_from_one_failed_probe() -> None:
    # The window between the unit exiting and the wrapper flushing the sentinel
    # reads as "gone" exactly once on a perfectly healthy build.
    script = rd.build_follow_script("u", "/run/user/1000/u.log")
    assert "misses=$((misses+1))" in script
    assert f'[ "$misses" -ge {rd._FOLLOW_LIVENESS_MISSES} ]' in script
    assert rd._FOLLOW_LIVENESS_MISSES > 1


def test_follow_script_carries_the_launch_scripts_manager_bus_guards() -> None:
    # The backstop is the same probe the launch script gates on. Without
    # XDG_RUNTIME_DIR or a reachable manager bus the liveness signal cannot be
    # READ, which is not the same as the unit being gone - and a follower that
    # confuses the two declares a live build lost on every poll.
    script = rd.build_follow_script("u", "/run/user/1000/u.log")
    assert "XDG_RUNTIME_DIR:-" in script
    assert "probe=0" in script


def test_follow_script_reports_a_malformed_sentinel_as_lost_not_as_an_exit_code() -> None:
    # The remote half of the case above: `${rc:-255}` turned an EMPTY sentinel
    # into the string "255", which the local side then read as a real exit code.
    script = rd.build_follow_script("u", "/run/user/1000/u.log")
    assert "BAKAR_DISPATCH_LOST=1" in script
    assert "${rc:-255}" not in script


def test_build_stream_ignores_dispatch_markers_after_the_launch_phase(fake_sp: FakeSubprocess) -> None:
    # On the fallback path this stream IS the build's own output. A bitbake
    # environment dump, or a recipe that greps bakar's sources, prints
    # `BAKAR_DISPATCH_UNIT=...` - and honouring it made the code discard the real
    # proc.wait() rc, announce a detach that never happened, and open a follower
    # against a unit that does not exist.
    fake_sp.popen_lines = [
        "BAKAR_DISPATCH_START=20260716-120000\n",
        "NOTE: recipe foo: compiling\n",
        "BAKAR_DISPATCH_UNIT=forged\n",
        "BAKAR_DISPATCH_LOG=/tmp/forged.log\n",
    ]
    fake_sp.popen_rc = 3
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc == 3
    # One Popen: no follower was opened against the forged unit.
    assert len([argv for kind, argv in fake_sp.calls if kind == "Popen"]) == 1


def test_follower_ignores_a_forged_rc_line_in_the_build_output(fake_sp: FakeSubprocess) -> None:
    # The followed stream IS the build's log, so an anchored pattern alone is not
    # enough - only the stream's LAST line, which the follow script writes after
    # tail is dead, is eligible to be the sentinel.
    fake_sp.popen_lines = _DETACHED_LAUNCH
    fake_sp.follow_lines = ["BAKAR_DISPATCH_RC=0\n", "ERROR: build failed\n", "BAKAR_DISPATCH_RC=1\n"]
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc == 1


def test_launch_buffer_stays_bounded_on_the_fallback_path(fake_sp: FakeSubprocess) -> None:
    # The `launch` list used to receive EVERY build line. On the detached path it
    # holds five; on the systemd-run-unavailable fallback it received the whole
    # Yocto stream, bypassing the sibling deque(maxlen=200) whose comment is
    # exactly "cap memory on a long/verbose Yocto build stream".
    fake_sp.popen_lines = ["BAKAR_DISPATCH_START=20260716-120000\n", *[f"line {i}\n" for i in range(5000)]]
    rc, captured, finished = rd._stream_remote_build(HOST, "script")
    assert finished is True
    assert rc == 0
    # The dispatch-start fence survives (it rides the launch phase) while the
    # build stream itself stays capped at the deque bound.
    assert captured[0].startswith("BAKAR_DISPATCH_START=")
    assert len(captured) <= 201


def test_remote_script_warns_when_the_remote_user_has_no_linger() -> None:
    # Without `loginctl enable-linger`, the user manager is stopped when the
    # user's last session ends - taking its transient units, and the detached
    # build, with it. That is the exact failure this change exists to remove, so
    # it must be visible rather than silent: the script reports the condition and
    # the local side turns it into an actionable hint.
    script = build_remote_script(["build"], Path("/tmp/ws"), {}, sccache_off=True, unit="bakar-dispatch-u6")
    assert "loginctl" in script
    assert "BAKAR_DISPATCH_WARN=linger-disabled" in script


def test_dispatch_detached_surfaces_the_linger_warning(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_sp.popen_lines = [*_DETACHED_LAUNCH, "BAKAR_DISPATCH_WARN=linger-disabled\n"]
    fake_sp.follow_lines = ["BAKAR_DISPATCH_RC=0\n"]
    rd.dispatch_remote_build(HOST, WS, WS, ["build", "--on", HOST], sccache_dist=False, assume_yes=True)
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert f"ssh {HOST} loginctl enable-linger" in out
    # The marker itself is transport, not something to echo raw.
    assert "BAKAR_DISPATCH_WARN" not in out


def test_dispatch_detached_failed_launch_is_not_followed(fake_sp: FakeSubprocess) -> None:
    # The markers are echoed BEFORE systemd-run runs, so they are present even
    # when the unit fails to start. A nonzero launch means there is no unit to
    # follow; tailing a log that will never appear would hang forever.
    fake_sp.popen_lines = _DETACHED_LAUNCH
    fake_sp.popen_rc = 1
    fake_sp.follow_lines = ["must not be reached\n"]
    rc = rd.dispatch_remote_build(HOST, WS, WS, ["build", "--on", HOST], sccache_dist=False, assume_yes=True)
    assert rc == 1
    assert len([argv for kind, argv in fake_sp.calls if kind == "Popen"]) == 1


def test_dispatch_detached_keeps_the_dispatch_start_fence(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    # The marker rides on the LAUNCH stream while the build output rides on the
    # follower's, and only the follower's tail is bounded. Both have to reach
    # run-id surfacing or a long build would evict the fence and resurrect the
    # stale-run-id bug the fence exists to prevent.
    fake_sp.popen_lines = _DETACHED_LAUNCH
    fake_sp.follow_lines = [*[f"line {i}\n" for i in range(500)], "BAKAR_DISPATCH_RC=0\n"]
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-000000\n"
    rd.dispatch_remote_build(HOST, WS, WS, ["build", "--on", HOST], sccache_dist=False, assume_yes=True)
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    # 20260716-000000 predates the 20260716-120000 dispatch-start marker.
    assert "no remote run dir was created" in out


# --- bakar stop --on <host>: the kill path for a detached build --------------


def test_stop_remote_dispatch_signals_then_stops_running_units(fake_sp: FakeSubprocess) -> None:
    fake_sp.systemctl_stdout = "bakar-dispatch-20260716-120000-aabbcc.service loaded active running\n"
    assert rd.stop_remote_dispatch(HOST, force=False, grace_seconds=30) is True
    stop_cmd = _last_remote_stdin(fake_sp)
    # SIGINT first (bitbake drains gracefully on it), `systemctl stop` only after
    # the grace period - the same ladder the local `bakar stop` walks.
    assert "--signal=SIGINT" in stop_cmd
    assert "systemctl --user stop" in stop_cmd
    assert stop_cmd.index("--signal=SIGINT") < stop_cmd.index("systemctl --user stop")
    assert "bakar-dispatch-20260716-120000-aabbcc.service" in stop_cmd


def test_stop_script_is_delivered_over_bash_stdin_not_as_an_ssh_argument(fake_sp: FakeSubprocess) -> None:
    # The remote LOGIN shell is fish, so `ssh <host> '<script>'` runs it as
    # `fish -c '<script>'`. Measured against real fish: `waited=0` is
    # "fish: Unsupported use of '='" -> exit 127, and fish validates the whole
    # buffer BEFORE executing, so nothing ran at all - not even the SIGINT. The
    # only kill path a detached build has was completely non-functional.
    fake_sp.systemctl_stdout = "bakar-dispatch-20260716-120000-aabbcc.service loaded active running\n"
    assert rd.stop_remote_dispatch(HOST, force=False, grace_seconds=30) is True
    ssh_calls = [argv for argv in _run_call_argvs(fake_sp) if argv[0] == "ssh"]
    delivery = ssh_calls[-1]
    assert delivery == ["ssh", "-o", "BatchMode=yes", HOST, "bash", "-s"]
    # And the script itself went to stdin, not into argv.
    assert "systemctl --user" in _last_remote_stdin(fake_sp)


def test_stop_remote_dispatch_force_skips_the_grace_period(fake_sp: FakeSubprocess) -> None:
    fake_sp.systemctl_stdout = "bakar-dispatch-20260716-120000-aabbcc.service loaded active running\n"
    assert rd.stop_remote_dispatch(HOST, force=True, grace_seconds=30) is True
    stop_cmd = _last_remote_stdin(fake_sp)
    assert "--signal=SIGINT" not in stop_cmd
    assert "systemctl --user stop" in stop_cmd


def test_stop_remote_dispatch_reports_when_nothing_is_running(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_sp.systemctl_stdout = ""
    assert rd.stop_remote_dispatch(HOST, force=False, grace_seconds=30) is False
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "no detached bakar build" in out


def test_stop_remote_dispatch_catches_a_still_starting_unit(fake_sp: FakeSubprocess) -> None:
    # `activating` is a unit that has been queued but not yet reached active.
    # Skipping it reports "nothing to stop" for a build that is about to run,
    # and the user reasonably concludes it is already gone.
    fake_sp.systemctl_stdout = "bakar-dispatch-20260716-120000-aabbcc.service loaded activating start\n"
    assert rd.stop_remote_dispatch(HOST, force=False, grace_seconds=30) is True


def test_stop_remote_dispatch_reads_the_active_column_not_the_description(fake_sp: FakeSubprocess) -> None:
    # The DESCRIPTION column is the unit's own command line, so for a bakar
    # dispatch it carries the whole build invocation - and a recipe or path with
    # "active" in it would otherwise resurrect a dead unit as a running one.
    fake_sp.systemctl_stdout = (
        "bakar-dispatch-20260716-120000-aabbcc.service loaded inactive dead "
        "[systemd-run] bakar bitbake -c build active\n"
    )
    assert rd.stop_remote_dispatch(HOST, force=False, grace_seconds=30) is False


def test_stop_script_waits_unbounded_when_grace_is_zero() -> None:
    # `--timeout 0` documents an unbounded graceful wait (docs/stop.md), and the
    # local stop honours it. A bounded loop with a 0 limit means the opposite -
    # no wait at all - so the SIGINT would be followed instantly by the hard
    # stop, costing the run log the graceful path exists to preserve.
    script = rd.build_stop_units_script(["bakar-dispatch-u.service"], force=False, grace_seconds=0)
    assert "--signal=SIGINT" in script
    assert "-lt 0" not in script
    assert "waited" not in script


def test_stop_remote_dispatch_ignores_inactive_units(fake_sp: FakeSubprocess) -> None:
    # `list-units --all` lists a unit that has already exited too; stopping one
    # is a no-op that would report success for a build nobody stopped.
    fake_sp.systemctl_stdout = "bakar-dispatch-20260716-120000-aabbcc.service loaded inactive dead\n"
    assert rd.stop_remote_dispatch(HOST, force=False, grace_seconds=30) is False


def test_stop_remote_dispatch_rejects_hyphen_prefixed_host(fake_sp: FakeSubprocess) -> None:
    # Same injection guard the dispatch path carries: a leading '-' would parse
    # as an ssh option.
    assert rd.stop_remote_dispatch("-oProxyCommand=evil", force=False, grace_seconds=30) is False
    assert fake_sp.calls == []


def test_stop_remote_dispatch_ssh_failure_is_not_a_success(fake_sp: FakeSubprocess) -> None:
    fake_sp.systemctl_rc = 255
    assert rd.stop_remote_dispatch(HOST, force=False, grace_seconds=30) is False


_TWO_UNITS = (
    "bakar-dispatch-20260716-120000-aabbcc.service loaded active running\n"
    "bakar-dispatch-20260716-130000-ddeeff.service loaded active running\n"
)


def test_stop_remote_dispatch_refuses_to_kill_more_than_one_build(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    # A host that accepts `--on` dispatches is a shared builder by definition, so
    # the second running unit is somebody else's build. `bakar stop --on <host>`
    # advertises "the detached build", singular, and used to kill every one of
    # them with no confirmation.
    fake_sp.systemctl_stdout = _TWO_UNITS
    assert rd.stop_remote_dispatch(HOST, force=False, grace_seconds=30) is False
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "aabbcc.service" in out
    assert "ddeeff.service" in out
    assert "--all" in out
    # Nothing was signalled: the only ssh call is the read-only listing.
    assert [argv for argv in _run_call_argvs(fake_sp) if argv[-1] == "-s"] == []


def test_stop_remote_dispatch_all_opts_into_stopping_every_build(fake_sp: FakeSubprocess) -> None:
    fake_sp.systemctl_stdout = _TWO_UNITS
    assert rd.stop_remote_dispatch(HOST, force=False, grace_seconds=30, stop_all=True) is True
    script = _last_remote_stdin(fake_sp)
    assert "aabbcc.service" in script
    assert "ddeeff.service" in script


def test_stop_script_still_waits_for_a_fractional_grace() -> None:
    # `int(0.5)` is 0, so `--timeout 0.5` generated `[ "$waited" -lt 0 ]` - a loop
    # that never runs. The graceful wait was skipped entirely and the hard stop
    # landed a moment after the SIGINT, which is what `--timeout 0` documents and
    # the opposite of what a positive grace asks for.
    script = rd.build_stop_units_script(["u.service"], force=False, grace_seconds=0.5)
    assert "-lt 0 ]" not in script
    assert "waited=0" in script
    assert "sleep 0.5" in script
    assert _bash_syntax_error(script) is None


def test_stop_script_keeps_its_two_second_cadence_for_the_default_grace() -> None:
    # The fractional fix counts in tenths; the common 30s case must not turn into
    # 150 systemctl forks.
    script = rd.build_stop_units_script(["u.service"], force=False, grace_seconds=30)
    assert "sleep 2\n" in script
    assert "waited=$((waited+20))" in script
    assert "-lt 300 ]" in script


# --- generated scripts must PARSE, not merely contain the right substrings ----


@pytest.mark.parametrize(
    ("name", "script_factory"),
    [
        (
            "launch",
            lambda: rd.build_remote_script(
                ["build", "my.yml"],
                Path("/tmp/ws"),
                {"BAKAR_MACHINE": "imx8mp", "KAS_WORK_DIR": "/tmp/k"},
                sccache_off=True,
                unit="bakar-dispatch-u9",
            ),
        ),
        ("follow", lambda: rd.build_follow_script("bakar-dispatch-u9", "/run/user/1000/bakar-dispatch-u9.log")),
        ("stop", lambda: rd.build_stop_units_script(["a.service", "b.service"], force=False, grace_seconds=30)),
        ("stop-force", lambda: rd.build_stop_units_script(["a.service"], force=True, grace_seconds=30)),
        ("stop-unbounded", lambda: rd.build_stop_units_script(["a.service"], force=False, grace_seconds=0)),
    ],
)
def test_every_generated_script_is_valid_bash(name: str, script_factory) -> None:
    # Nothing local ever runs these - they are written to a remote bash's stdin -
    # so without this the only check on them is `"loginctl" in script`. That is
    # precisely how the stop script shipped as bash prose handed to a fish login
    # shell: a substring assertion passes whatever interpreter eventually reads
    # the string.
    error = _bash_syntax_error(script_factory())
    assert error is None, f"{name} script is not valid bash: {error}"


def test_remote_only_dirs_missing_local_workspace_yields_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A local workspace that does not exist (or is unreadable) must not crash the
    # dispatch: iterdir() raises FileNotFoundError, which is swallowed the same
    # way a failed remote listing is. Regression for the CI failure where the
    # hardcoded WS path exists on the dev box but not on the runner.
    missing = tmp_path / "does-not-exist"
    fake = _ListingSubprocess(0, "meta-avocado/\nopenembedded-core/\n")
    monkeypatch.setattr(rd, "subprocess", fake)
    assert rd._remote_only_dirs(missing, HOST) == []
