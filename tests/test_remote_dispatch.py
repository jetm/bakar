"""Unit tests for bakar.steps.remote_dispatch pure builders.

These cover the host-free builders only: the exclude set, the rsync argv
constructor, the ``--on`` stripper, the remote bash-script generator, and the
mirror workspace guard. Orchestration (ssh/rsync subprocess) is a
later task; nothing here touches a live host.
"""

from __future__ import annotations

import inspect
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bakar import build_scope, observability
from bakar.steps.remote_dispatch import (
    RSYNC_EXCLUDES,
    RemoteScriptSpec,
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
    assert "--delete" not in argv
    assert "-n" not in argv
    assert "-i" not in argv


def test_rsync_argv_one_exclude_per_pattern() -> None:
    argv = build_rsync_argv(WS, HOST)
    for pat in RSYNC_EXCLUDES:
        assert f"--exclude={pat}" in argv
    excludes = [a for a in argv if a.startswith("--exclude=")]
    assert len(excludes) == len(RSYNC_EXCLUDES)
    # The former worktree-admin include rule went with the deleting mirror.
    assert not [a for a in argv if a.startswith("--include=")]


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
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build", "my.yml"], cwd=Path("/home/tiamarin/ws"), env_vars={}, sccache_off=True)
    )
    lines = script.splitlines()
    assert lines[0] == f"cd {shlex.quote('/home/tiamarin/ws')} || exit 1"
    assert lines[-1] == "exec env BAKAR_SCCACHE_DIST=0 bakar build my.yml"


def test_remote_script_sccache_on_omits_token() -> None:
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build", "my.yml"], cwd=Path("/home/tiamarin/ws"), env_vars={}, sccache_off=False)
    )
    assert "BAKAR_SCCACHE_DIST=0" not in script
    assert script.splitlines()[-1] == "exec env bakar build my.yml"


def test_remote_script_never_uses_bash_lc() -> None:
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build", "my.yml"], cwd=Path("/tmp/ws"), env_vars={}, sccache_off=True)
    )
    assert "bash -lc" not in script


def test_remote_script_no_bare_name_value_prefix() -> None:
    # The env assignment must live behind env(1), never as a bare shell prefix.
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build", "my.yml"], cwd=Path("/tmp/ws"), env_vars={}, sccache_off=True)
    )
    exec_line = script.splitlines()[-1]
    assert exec_line.startswith("exec env ")
    assert not exec_line.startswith("BAKAR_SCCACHE_DIST=0")


def test_remote_script_quotes_cwd_with_spaces() -> None:
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build"], cwd=Path("/home/tia marin/ws"), env_vars={}, sccache_off=True)
    )
    assert script.splitlines()[0] == "cd '/home/tia marin/ws' || exit 1"


def test_remote_script_shlex_joins_argv() -> None:
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build", "kas/my file.yml"], cwd=Path("/tmp/ws"), env_vars={}, sccache_off=True)
    )
    assert "'kas/my file.yml'" in script.splitlines()[-1]


def test_remote_script_forwards_env_as_sorted_tokens() -> None:
    # BAKAR_*/KAS_* env is forwarded as sorted, shlex-quoted NAME=value tokens
    # after `env`, so the remote resolves the same build as the local one.
    env = {"KAS_CONTAINER_IMAGE": "img", "BAKAR_MACHINE": "imx8mp"}
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build"], cwd=Path("/tmp/ws"), env_vars=env, sccache_off=False)
    )
    exec_line = script.splitlines()[-1]
    assert exec_line == "exec env BAKAR_MACHINE=imx8mp KAS_CONTAINER_IMAGE=img bakar build"


def test_remote_script_sccache_off_token_wins_over_forwarded() -> None:
    # A forwarded BAKAR_SCCACHE_DIST=1 is overridden by the appended =0 (env(1)
    # applies tokens left-to-right, last wins), so the token must come LAST.
    env = {"BAKAR_SCCACHE_DIST": "1"}
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build"], cwd=Path("/tmp/ws"), env_vars=env, sccache_off=True)
    )
    exec_line = script.splitlines()[-1]
    assert exec_line == "exec env BAKAR_SCCACHE_DIST=1 BAKAR_SCCACHE_DIST=0 bakar build"
    assert exec_line.index("BAKAR_SCCACHE_DIST=1") < exec_line.index("BAKAR_SCCACHE_DIST=0")


def test_remote_script_emits_dispatch_start_marker() -> None:
    # The dispatch-start marker is echoed before exec so it streams back and
    # fences run-id discovery against a stale previous run.
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build"], cwd=Path("/tmp/ws"), env_vars={}, sccache_off=True)
    )
    lines = script.splitlines()
    assert lines[1] == 'echo "BAKAR_DISPATCH_START=$(date -u +%Y%m%d-%H%M%S)"'


def test_dispatch_start_marker_uses_the_same_clock_as_run_ids() -> None:
    # The marker is string-compared against a run DIRECTORY NAME, and those are
    # named by RunLog.run_id from `datetime.now(UTC)`. So the marker has to read
    # UTC too, or the comparison spans two clocks: in a DST fall-back's repeated
    # hour a local-time fence let a run created before the dispatch sort at or
    # after it.
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build"], cwd=Path("/tmp/ws"), env_vars={}, sccache_off=True)
    )
    assert "$(date -u +%Y%m%d-%H%M%S)" in script

    # Pin the other half of the invariant: if run ids ever leave UTC, this
    # test should fail rather than let the two drift apart again.
    assert "datetime.now(UTC)" in inspect.getsource(observability.RunLogger)


def test_fence_and_ids_compare_as_utc_in_the_repeated_hour(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 2026-11-01 is the US fall-back day: 01:00-02:00 America/New_York happens
    # twice. Ids and the fence are both UTC, so the earlier run sorts below the
    # fence whatever the host's TZ says.
    monkeypatch.setenv("TZ", "America/New_York")
    monkeypatch.setattr(
        rd,
        "_ssh_bash_capture",
        lambda host, script, *, batch_mode=True: subprocess.run(
            ["bash", "-s"], input=script, capture_output=True, text=True, check=False
        ),
    )
    _run_dir(tmp_path, "build", "20261101-061500-1")
    _run_dir(tmp_path, "build", "20261101-063500-2")
    assert rd._discover_newest_run_id(HOST, tmp_path, "20261101-063000") == "20261101-063500-2"


# ---------------------------------------------------------------------------
# build_remote_script: detachment from the ssh session
# ---------------------------------------------------------------------------


def test_remote_script_detaches_the_build_from_the_ssh_session() -> None:
    # The build must NOT be a plain child of `ssh <host> bash -s`: when the
    # local dispatcher dies, sshd SIGHUPs the session and a child build dies
    # with it (observed: a 50-minute cryptsetup-var build killed by "Keyboard
    # Interrupt, closing down" / bitbake exit -15). Under a transient user unit
    # the build is reparented to the user manager and survives.
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build", "my.yml"], cwd=Path("/tmp/ws"), env_vars={}, sccache_off=True),
        unit="bakar-dispatch-u1",
    )
    assert "systemd-run --user --unit=bakar-dispatch-u1" in script
    assert "--collect" in script
    assert "--same-dir" in script


def test_remote_script_emits_parseable_unit_and_log_markers() -> None:
    # The local side references the unit (to poll it and to stop it) and tails
    # the log, so both have to arrive over the launch stream in a parseable form.
    # printf with a LITERAL format, not an interpolating `echo "...=$unit"`: the
    # unit is public API and inside double quotes a `$(...)` in it would be
    # command-substituted on the remote.
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build"], cwd=Path("/tmp/ws"), env_vars={}, sccache_off=True),
        unit="bakar-dispatch-u2",
    )
    log = remote_log_expr("bakar-dispatch-u2")
    assert "printf 'BAKAR_DISPATCH_UNIT=%s\\n' bakar-dispatch-u2" in script
    assert f"printf 'BAKAR_DISPATCH_LOG=%s\\n' {log}" in script


def test_remote_script_quotes_the_unit_everywhere_it_interpolates_it() -> None:
    # `unit` is public API and reaches a `systemd-run --unit=`, a printf argument
    # and a `>` redirect. Its siblings in the same function (cwd, env tokens,
    # remote_argv) are all shlex-quoted; leaving this one bare was the odd one
    # out, and only tests pass a hand-written unit today.
    evil = "u$(touch /tmp/pwned) x"
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build"], cwd=Path("/tmp/ws"), env_vars={}, sccache_off=True), unit=evil
    )
    assert f"--unit={shlex.quote(evil)}" in script
    assert f"printf 'BAKAR_DISPATCH_UNIT=%s\\n' {shlex.quote(evil)}" in script
    assert shlex.quote(f"{evil}.log") in script
    assert _bash_syntax_error(script) is None


def test_remote_script_records_the_build_exit_code_in_a_sentinel() -> None:
    # The unit is `--collect`ed away once it exits, so `systemctl show` cannot be
    # trusted to still hold the exit status. The wrapper writes it to a sentinel
    # file beside the log instead, which is also what ends the local follower.
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build"], cwd=Path("/tmp/ws"), env_vars={}, sccache_off=True),
        unit="bakar-dispatch-u3",
    )
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
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build"], cwd=Path("/tmp/ws"), env_vars={}, sccache_off=True),
        unit="bakar-dispatch-u7",
    )
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
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build"], cwd=Path("/tmp/ws"), env_vars={}, sccache_off=True),
        unit="bakar-dispatch-u8",
    )
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
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build", "my.yml"], cwd=Path("/tmp/ws"), env_vars={}, sccache_off=True),
        unit="bakar-dispatch-u4",
    )
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
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build"], cwd=Path("/tmp/ws"), env_vars={}, sccache_off=True),
        unit="bakar-dispatch-u5",
    )
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
# Orchestration: preflight_remote / dispatch_remote_build
# (mocked subprocess, no live host)
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
    TimeoutExpired = subprocess.TimeoutExpired
    CompletedProcess = subprocess.CompletedProcess

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.reachable_rc = 0
        self.reachable_stderr = ""
        self.remote_version = ""
        self.local_version = ""
        self.rsync_rc = 0
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
        # Every `subprocess.run` call's kwargs, aligned with `run_inputs`.
        self.run_kwargs: list[dict] = []
        # When set, every ssh `run` raises TimeoutExpired as a hung link would.
        self.run_times_out = False

    def run(self, argv, **kwargs) -> _Result:
        argv = list(argv)
        self.calls.append(("run", argv))
        self.run_kwargs.append(kwargs)
        if self.run_times_out and argv[0] == "ssh":
            self.run_inputs.append(kwargs.get("input"))
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        stdin_script = kwargs.get("input")
        self.run_inputs.append(stdin_script)
        # Both the preflight probe and the stop script ride `ssh <host> bash -s`;
        # they are told apart by what is written to stdin, exactly as the remote
        # host would tell them apart.
        # EVERY remote payload now rides `ssh <host> bash -s` stdin, so argv is
        # identical for all of them and stdin is the only discriminator - which
        # is exactly how the remote host tells them apart too. Order matters:
        # the unit LISTING and the stop SCRIPT both contain "systemctl --user",
        # and matching the stop branch first made a listing return no stdout,
        # so a live dispatch reported "no detached bakar build is running".
        if argv[0] == "ssh" and argv[-1] == "-s":
            if stdin_script and "list-units" in stdin_script:
                return _Result(self.systemctl_rc, stdout=self.systemctl_stdout)
            if stdin_script and "systemctl --user" in stdin_script:
                return _Result(self.stop_rc)
            if stdin_script and stdin_script.lstrip().startswith("find "):
                return _Result(0, stdout=self.find_stdout)
            return _Result(self.reachable_rc, stdout=self.remote_version, stderr=self.reachable_stderr)
        if argv[0] == "bakar" and "--version" in argv:
            return _Result(0, stdout=self.local_version)
        if argv[0] == "rsync":
            return _Result(self.rsync_rc)
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
    """Clear ambient BAKAR_*/KAS_* env so forwarded-env assertions are deterministic.

    RSYNC_RSH goes too: ``build_rsync_argv`` omits ``-e`` when it is set.
    """
    import os

    monkeypatch.delenv("RSYNC_RSH", raising=False)
    for key in list(os.environ):
        if key.startswith(("BAKAR_", "KAS_")):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def fake_sp(monkeypatch: pytest.MonkeyPatch) -> FakeSubprocess:
    fake = FakeSubprocess()
    monkeypatch.setattr(rd, "subprocess", fake)
    return fake


_SSH = [
    "ssh",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=4",
    "-o",
    "ConnectTimeout=15",
]
_BATCH = ["-o", "BatchMode=yes"]


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
    assert _run_call_argvs(fake_sp)[0] == [*_SSH, *_BATCH, HOST, "bash", "-s"]


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


# --- dispatch_remote_build: guards and ordering -----------------------------


def test_dispatch_unreachable_aborts_before_rsync(fake_sp: FakeSubprocess) -> None:
    fake_sp.reachable_rc = 255
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    assert rc != 0
    # No rsync and no remote Popen may run.
    assert not any(argv[0] == "rsync" for _, argv in fake_sp.calls)
    assert not any(kind == "Popen" for kind, _ in fake_sp.calls)


def test_dispatch_strict_ordering(fake_sp: FakeSubprocess) -> None:
    fake_sp.popen_rc = 0
    fake_sp.find_stdout = (
        "1699999999.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-235959-4242\n"
    )
    rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    reach_i = _index_of(fake_sp, lambda k, a: k == "run" and a[0] == "ssh" and a[-1] == "-s")
    real_i = _real_rsync_index(fake_sp)
    popen_i = _index_of(fake_sp, lambda k, a: k == "Popen")
    assert -1 < reach_i < real_i < popen_i
    # The additive mirror runs no dry-run preview any more: there is nothing to confirm.
    assert not any(a[0] == "rsync" and "-n" in a for _k, a in fake_sp.calls)


def test_dispatch_rsync_failure_skips_remote_build(fake_sp: FakeSubprocess) -> None:
    fake_sp.rsync_rc = 23
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    assert rc == 23
    assert not any(kind == "Popen" for kind, _ in fake_sp.calls)


# --- dispatch_remote_build: exit propagation and script construction --------


@pytest.mark.parametrize("remote_rc", [0, 1, 2, 42])
def test_dispatch_propagates_remote_exit(fake_sp: FakeSubprocess, remote_rc: int) -> None:
    fake_sp.popen_rc = remote_rc
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-000000-4242\n"
    fake_sp.popen_lines = ["Run `bakar triage 20260716-000000` for details.\n"] if remote_rc else []
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    assert rc == remote_rc


def test_dispatch_script_strips_on_and_sets_sccache_off(fake_sp: FakeSubprocess) -> None:
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-010101-4242\n"
    rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=Path("/home/tiamarin/ws"), local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    assert fake_sp.last_proc is not None
    script = fake_sp.last_proc.stdin.buffer
    # ssh bash -s stdin, never bash -lc.
    assert ("Popen", [*_SSH, HOST, "bash", "-s"]) in fake_sp.calls
    assert "bash -lc" not in script
    # --on stripped, sccache forced off.
    assert "--on" not in script
    assert "exec env BAKAR_SCCACHE_DIST=0 bakar build my.yml" in script


def test_dispatch_sccache_dist_opt_in_omits_env_token(fake_sp: FakeSubprocess) -> None:
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-020202-4242\n"
    rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=Path("/home/tiamarin/ws"), local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=True, assume_yes=True),
    )
    assert fake_sp.last_proc is not None
    script = fake_sp.last_proc.stdin.buffer
    assert "BAKAR_SCCACHE_DIST=0" not in script
    assert "exec env bakar build my.yml" in script


# --- dispatch_remote_build: run-id surfacing --------------------------------


def test_dispatch_run_id_from_failure_stream(fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]) -> None:
    fake_sp.popen_rc = 1
    fake_sp.popen_lines = ["some build output\n", "Run `bakar triage 20260716-120000` for details.\n"]
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    assert rc == 1
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "20260716-120000" in out
    assert f"ssh {HOST} bakar triage 20260716-120000" in out
    # A failure must NOT trigger the newest-run-dir find discovery.
    assert not any((inp or "").lstrip().startswith("find ") for inp in fake_sp.run_inputs)


def test_dispatch_run_id_from_success_discovery(fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]) -> None:
    fake_sp.popen_rc = 0
    fake_sp.popen_lines = ["build succeeded\n"]
    fake_sp.find_stdout = (
        "1699999999.5 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-235959-4242\n"
    )
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    assert rc == 0
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "20260716-235959" in out
    assert f"ssh {HOST} bakar triage 20260716-235959" in out
    # Success path performs the discovery ssh(find).
    assert any((inp or "").lstrip().startswith("find ") for inp in fake_sp.run_inputs)


def test_dispatch_failure_without_triage_falls_back_to_discovery(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    # On failure, if the triage-hint line is absent from the stream (e.g. lost to
    # Rich's non-TTY line-wrap), the run-id is recovered via newest-run-dir discovery.
    fake_sp.popen_rc = 1
    fake_sp.popen_lines = ["some build output with no triage hint\n"]
    fake_sp.find_stdout = (
        "1699999999.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-333333-4242\n"
    )
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    assert rc == 1
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "20260716-333333" in out
    # Discovery ran because the stream did not yield the run-id.
    assert any((inp or "").lstrip().startswith("find ") for inp in fake_sp.run_inputs)


def test_dispatch_unsafe_workspace_aborts_cleanly(fake_sp: FakeSubprocess) -> None:
    # An unsafe workspace (filesystem root) must abort with exit 1 via a caught
    # ValueError - no traceback, and no ssh/rsync touched.
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=Path("/"), cwd=Path("/"), local_args=["build", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
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
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-000000-4242\n"
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
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
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-235959-4242\n"
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    assert rc == 0
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "20260716-235959" in out


# --- C3: env forwarding -----------------------------------------------------


def test_dispatch_forwards_bakar_kas_env(fake_sp: FakeSubprocess, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_MACHINE", "imx8mp")
    monkeypatch.setenv("KAS_CONTAINER_IMAGE", "img")
    monkeypatch.setenv("UNRELATED_VAR", "x")
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-000000-4242\n"
    rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=Path("/home/tiamarin/ws"), local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
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
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-000000-4242\n"
    rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    assert fake_sp.popen_kwargs.get("encoding") == "utf-8"
    assert fake_sp.popen_kwargs.get("errors") == "replace"


# --- C8: Ctrl-C story -------------------------------------------------------


def test_dispatch_keyboard_interrupt_returns_130(
    fake_sp: FakeSubprocess, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(rd._stream_state, "unit_seen", False)

    def _interrupt(host: str, script: str):
        rd._stream_state.unit_seen = True
        raise KeyboardInterrupt

    monkeypatch.setattr(rd, "_stream_remote_build", _interrupt)
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    assert rc == 130
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "does not stop the remote build" in out
    assert f"ssh {HOST} bakar stop" in out
    assert f"ssh {HOST} bakar triage" in out


def test_interrupt_before_any_unit_marker_does_not_promise_a_running_build(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _interrupt(host: str, script: str):
        raise KeyboardInterrupt

    monkeypatch.setattr(rd, "_stream_remote_build", _interrupt)
    assert rd.run_remote_build(HOST, "script", WS) == 130
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "interrupted before the remote build detached" in out
    assert "bakar stop --on" not in out
    assert "nothing is left running" not in out
    assert "bakar ps" in out


def test_unit_seen_is_recorded_by_the_stream(fake_sp: FakeSubprocess, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sp.popen_lines = ["BAKAR_DISPATCH_UNIT=bakar-dispatch-x\n", "BAKAR_DISPATCH_LOG=/tmp/x.log\n"]
    fake_sp.popen_rc = 1
    monkeypatch.setattr(rd._stream_state, "unit_seen", False)
    rd._stream_remote_build(HOST, "script")
    assert rd._stream_state.unit_seen is True


def test_stream_without_unit_marker_leaves_unit_unseen(
    fake_sp: FakeSubprocess, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_sp.popen_lines = ["plain build output\n"]
    monkeypatch.setattr(rd._stream_state, "unit_seen", False)
    rd._stream_remote_build(HOST, "script")
    assert rd._stream_state.unit_seen is False


# --- run-id lookup outcomes -----------------------------------------------------


def _lookup_returning(monkeypatch: pytest.MonkeyPatch, rc: int, stdout: str = "", stderr: str = "") -> list[str]:
    scripts: list[str] = []

    def capture(host: str, script: str, *, batch_mode: bool = True) -> _Result:
        scripts.append(script)
        return _Result(rc, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(rd, "_ssh_bash_capture", capture)
    return scripts


def test_lookup_failure_with_rc_zero_never_claims_the_build_failed_before_starting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _lookup_returning(monkeypatch, 255, stderr="Connection timed out\nmore\n")
    rd._surface_run_id(HOST, WS, ["build ok\n"], 0)
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "could not look up the remote run-id (ssh exit 255): Connection timed out" in out
    assert "more" not in out
    assert "failed before starting" not in out


def test_lookup_failure_on_a_failed_build_still_reports_the_lookup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _lookup_returning(monkeypatch, 255, stderr="denied")
    rd._surface_run_id(HOST, WS, ["boom\n"], 2)
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "could not look up the remote run-id (ssh exit 255): denied" in out
    assert "failed before starting" not in out


def test_no_run_dir_with_nonzero_rc_keeps_the_failed_before_starting_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _lookup_returning(monkeypatch, 0, stdout="")
    rd._surface_run_id(HOST, WS, ["boom\n"], 1)
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "no remote run dir was created - the build failed before starting" in out


def test_no_run_dir_with_rc_zero_does_not_claim_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _lookup_returning(monkeypatch, 0, stdout="")
    rd._surface_run_id(HOST, WS, ["ok\n"], 0)
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "failed before starting" not in out
    assert "no remote run directory was found" in out


def test_lookup_that_hangs_is_a_lookup_failure(fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]) -> None:
    fake_sp.run_times_out = True
    result = rd._discover_newest_run_id(HOST, WS)
    assert isinstance(result, rd.RunLookupFailed)
    assert result.rc == 255
    assert "no response within 60s" in result.detail
    rd._surface_run_id(HOST, WS, ["ok\n"], 0)
    _cap = capsys.readouterr()
    assert "could not look up the remote run-id (ssh exit 255)" in _cap.out + _cap.err
    for _, kwargs in _ssh_runs(fake_sp):
        assert kwargs["timeout"] == 60


def test_lookup_runs_under_batch_mode(fake_sp: FakeSubprocess) -> None:
    fake_sp.find_stdout = "1.0 /x/build/runs/20260716-000000-4242\n"
    assert rd._discover_newest_run_id(HOST, WS) == "20260716-000000-4242"
    ((argv, _),) = _ssh_runs(fake_sp)
    assert "BatchMode=yes" in argv


def test_lookup_matches_a_qcom_build_directory_not_only_build(monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = _lookup_returning(monkeypatch, 0, stdout="1.0 /ws/build-qcom-wayland/runs/20260716-000000-4242\n")
    assert rd._discover_newest_run_id(HOST, WS) == "20260716-000000-4242"
    assert "'*/runs/20*'" in scripts[0]
    assert "build/runs" not in scripts[0]


def test_lookup_find_pattern_finds_a_qcom_run_dir_on_a_real_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "build-qcom-wayland" / "runs" / "20260716-120000-4242"
    run.mkdir(parents=True)
    (tmp_path / "build" / "tmp" / "runs" / "20990101-000000-1").mkdir(parents=True)
    scripts: list[str] = []

    def capture(host: str, script: str, *, batch_mode: bool = True) -> subprocess.CompletedProcess[str]:
        scripts.append(script)
        return subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True, check=False)

    monkeypatch.setattr(rd, "_ssh_bash_capture", capture)
    assert rd._discover_newest_run_id(HOST, tmp_path) == "20260716-120000-4242"


# --- C10: broken pipe on stdin.write ----------------------------------------


def test_dispatch_broken_pipe_returns_255_cleanly(fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]) -> None:
    fake_sp.broken_pipe = True
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
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
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    assert rc == 1
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "Host key verification failed." in out
    assert not any(argv[0] == "rsync" for _, argv in fake_sp.calls)


# --- C12c: hyphen-host injection guard ---------------------------------------


def test_dispatch_rejects_hyphen_prefixed_host(fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]) -> None:
    rc = rd.dispatch_remote_build(
        "-oProxyCommand=evil",
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "--on", "-x"]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
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
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=Path("/home/tiamarin/ws"), local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=False),
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
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-000000-4242\n"
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=Path("/home/tiamarin/ws"), local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    assert rc == 0
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "bakar mismatch" in out
    assert "proceeding despite" in out
    assert "1.0.0" in out
    assert "2.0.0" in out


class _StderrVersionSubprocess(FakeSubprocess):
    """``bakar --version`` writes to stderr, as the real console does, on both hosts."""

    def __init__(self, local: str, remote: str) -> None:
        super().__init__()
        self._local = local
        self._remote = remote

    def run(self, argv, **kwargs) -> _Result:
        argv = list(argv)
        if argv[0] == "bakar" and "--version" in argv:
            self.calls.append(("run", argv))
            self.run_inputs.append(kwargs.get("input"))
            return _Result(0, stdout="", stderr=self._local)
        if argv[0] == "ssh" and "bakar --version" in (kwargs.get("input") or ""):
            self.calls.append(("run", argv))
            self.run_inputs.append(kwargs.get("input"))
            return _Result(0, stdout="", stderr=self._remote)
        return super().run(argv, **kwargs)


def test_versions_printed_on_stderr_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _StderrVersionSubprocess("bakar 1.0 (aaaa)\n", "bakar 1.0 (bbbb)\n")
    monkeypatch.setattr(rd, "subprocess", fake)
    assert rd._local_bakar_version() == "bakar 1.0 (aaaa)"
    assert rd.preflight_remote(HOST) == (True, "bakar 1.0 (bbbb)")


def test_stderr_version_mismatch_refuses_the_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Reading stdout alone saw None on both sides, so this mismatch used to pass.
    fake = _StderrVersionSubprocess("bakar 1.0 (aaaa)\n", "bakar 0.9 (bbbb)\n")
    monkeypatch.setattr(rd, "subprocess", fake)
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=False),
    )
    assert rc == 1
    assert "bakar mismatch" in capsys.readouterr().err
    assert not any(argv[0] == "rsync" for _, argv in fake.calls)


def test_preflight_probe_folds_the_remote_version_into_stdout(fake_sp: FakeSubprocess) -> None:
    rd.preflight_remote(HOST)
    assert "bakar --version 2>&1" in (fake_sp.run_inputs[0] or "")


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
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-235959-4242\n"
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
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
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "my.yml", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    assert rc == 42


def test_dispatch_detached_rc_line_is_not_echoed_to_the_user(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    # The sentinel line is transport, not build output.
    fake_sp.popen_lines = _DETACHED_LAUNCH
    fake_sp.follow_lines = ["real build output\n", "BAKAR_DISPATCH_RC=0\n"]
    rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    assert "real build output" in out
    assert "BAKAR_DISPATCH_RC" not in out


def test_dispatch_detached_stream_goes_to_stderr_not_stdout(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    # stdout is reserved for machine-readable payloads, and the kas families
    # write no build output there. A remote dispatch that echoed to stdout would
    # make `bakar build --on <host> > payload` mean something different from the
    # same redirect run locally.
    fake_sp.popen_lines = _DETACHED_LAUNCH
    fake_sp.follow_lines = ["compiling glibc\n", "BAKAR_DISPATCH_RC=0\n"]
    rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    _cap = capsys.readouterr()
    assert _cap.out == ""
    assert "compiling glibc" in _cap.err
    assert "BAKAR_DISPATCH_START" in _cap.err


def test_dispatch_coupled_stream_goes_to_stderr_not_stdout(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    # The fallback `exec` form (no BAKAR_DISPATCH_UNIT marker) makes the launch
    # stream the build's own. It takes the same stream as the detached path.
    fake_sp.popen_lines = ["NOTE: Executing Tasks\n", "NOTE: Tasks Summary\n"]
    rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    _cap = capsys.readouterr()
    assert _cap.out == ""
    assert "NOTE: Executing Tasks" in _cap.err


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
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
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
    rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
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
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
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
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    assert rc == 3
    # One Popen: no follower was opened against the forged unit.
    assert len([argv for kind, argv in fake_sp.calls if kind == "Popen"]) == 1


def test_follower_ignores_a_forged_rc_line_in_the_build_output(fake_sp: FakeSubprocess) -> None:
    # The followed stream IS the build's log, so an anchored pattern alone is not
    # enough - only the stream's LAST line, which the follow script writes after
    # tail is dead, is eligible to be the sentinel.
    fake_sp.popen_lines = _DETACHED_LAUNCH
    fake_sp.follow_lines = ["BAKAR_DISPATCH_RC=0\n", "ERROR: build failed\n", "BAKAR_DISPATCH_RC=1\n"]
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
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
    script = build_remote_script(
        RemoteScriptSpec(remote_argv=["build"], cwd=Path("/tmp/ws"), env_vars={}, sccache_off=True),
        unit="bakar-dispatch-u6",
    )
    assert "loginctl" in script
    assert "BAKAR_DISPATCH_WARN=linger-disabled" in script


def test_dispatch_detached_surfaces_the_linger_warning(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_sp.popen_lines = [*_DETACHED_LAUNCH, "BAKAR_DISPATCH_WARN=linger-disabled\n"]
    fake_sp.follow_lines = ["BAKAR_DISPATCH_RC=0\n"]
    rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
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
    rc = rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
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
    fake_sp.find_stdout = "1.0 /home/tiamarin/repos/work/peridio-scarthgap-build/build/runs/20260716-000000-4242\n"
    rd.dispatch_remote_build(
        HOST,
        rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "--on", HOST]),
        rd.DispatchFlags(sccache_dist=False, assume_yes=True),
    )
    _cap = capsys.readouterr()
    out = _cap.out + _cap.err
    # 20260716-000000 predates the 20260716-120000 dispatch-start marker, so it is
    # discarded; the build exited 0, so the message must not call it a failure.
    assert "20260716-000000" not in out
    assert "no remote run directory was found" in out
    assert "failed before starting" not in out


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
    assert delivery == [*_SSH, *_BATCH, HOST, "bash", "-s"]
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
    assert not any("systemctl --user stop" in (inp or "") for inp in fake_sp.run_inputs)


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
                rd.RemoteScriptSpec(
                    remote_argv=["build", "my.yml"],
                    cwd=Path("/tmp/ws"),
                    env_vars={"BAKAR_MACHINE": "imx8mp", "KAS_WORK_DIR": "/tmp/k"},
                    sccache_off=True,
                ),
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


def test_every_ssh_invocation_delivers_its_payload_via_bash_s() -> None:
    """No remote payload may ride as an ``ssh <host> <cmd>`` argument.

    The remote login shell is fish, so a command string handed to ssh is
    executed by fish rather than bash. Anything past a single simple command
    then misbehaves silently: `_discover_newest_run_id`'s find - with `-o`,
    `-prune`, `-printf` and a pipe - produced NO output under fish while
    working under bash, so run-id discovery returned None and a build that had
    just succeeded was announced as "no remote run dir was created - the build
    failed before starting".

    This walks the module AST rather than grepping for the sites that were
    wrong when it was written, so it fails on a NEW call site too. An earlier
    "drift detector" in this file only asserted that two hardcoded tokens
    appeared in both places and could not actually detect drift; this one can.
    """
    import ast

    from bakar.steps import remote_dispatch as rd

    tree = ast.parse(Path(rd.__file__).read_text())
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) not in {"run", "Popen"}:
            continue
        if not node.args or not isinstance(node.args[0], ast.List):
            continue
        consts = [e.value for e in node.args[0].elts if isinstance(e, ast.Constant)]
        if not consts or consts[0] != "ssh":
            continue
        offenders.append(ast.unparse(node.args[0]))
    # Every ssh argv is built by `_ssh_argv`, whose call must name `bash -s`.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_ssh_argv":
            consts = [a.value for a in node.args if isinstance(a, ast.Constant)]
            if "bash" not in consts or "-s" not in consts:
                offenders.append(ast.unparse(node))

    assert offenders == [], (
        "ssh invocation(s) pass a command string to the remote LOGIN shell (fish) "
        f"instead of delivering it to `bash -s` over stdin: {offenders}"
    )


# --- never delete: property ---------------------------------------------------

_SEGMENT = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789._-", min_size=1, max_size=12).filter(
    lambda s: s not in {".", ".."}
)


def _is_delete_option(token: str) -> bool:
    # `--del` is rsync's alias for --delete-during.
    return token in {"--del", "--delete", "--remove-source-files"} or token.startswith("--delete-")


@given(segments=st.lists(_SEGMENT, min_size=1, max_size=4), host=_SEGMENT)
def test_rsync_argv_never_emits_a_delete_or_remove_option(segments: list[str], host: str) -> None:
    argv = build_rsync_argv(Path("/", *segments), host)
    assert not [tok for tok in argv if _is_delete_option(tok)]


@settings(max_examples=25, deadline=None)
@given(segments=st.lists(_SEGMENT, min_size=2, max_size=4), host=_SEGMENT, assume_yes=st.booleans())
def test_every_dispatch_plan_has_no_deletes(segments: list[str], host: str, assume_yes: bool) -> None:
    built: list[rd.DispatchPlan] = []

    # __init__ rather than __post_init__: the base dataclass's generated __init__
    # only calls a __post_init__ that existed when it was decorated.
    class _Recording(rd.DispatchPlan):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            built.append(self)

    fake = FakeSubprocess()
    fake.find_stdout = "1.0 /x/build/runs/20260716-000000-4242\n"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rd, "subprocess", fake)
        mp.setattr(rd, "DispatchPlan", _Recording)
        ws = Path("/var/tmp/bakar-prop", *segments)
        rd.dispatch_remote_build(
            host,
            rd.MirrorRequest(ws_root=ws, cwd=ws, local_args=["build", "my.yml", "--on", host]),
            rd.DispatchFlags(sccache_dist=False, assume_yes=assume_yes),
        )
    # A dispatch refused before transferring (a version mismatch without --yes)
    # prints no plan and runs no rsync; any dispatch that does transfer must have
    # printed a plan first.
    rsync_ran = any(argv and argv[0] == "rsync" for _kind, argv in fake.calls)
    assert built or not rsync_ran, "rsync ran without a printed plan"
    assert all(plan.deletes == [] for plan in built)
    for _kind, argv in fake.calls:
        assert not [tok for tok in argv if _is_delete_option(tok)]


# --- dispatch prints a plan and never prompts ---------------------------------


def test_dispatch_prints_the_plan_and_never_prompts(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    import typer

    def _boom(*a, **k):
        raise AssertionError("dispatch must not prompt")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(typer, "confirm", _boom)
        mp.setattr("builtins.input", _boom)
        fake_sp.find_stdout = "1.0 /x/build/runs/20260716-000000-4242\n"
        rc = rd.dispatch_remote_build(
            HOST,
            rd.MirrorRequest(ws_root=WS, cwd=WS, local_args=["build", "my.yml", "--on", HOST]),
            rd.DispatchFlags(sccache_dist=False, assume_yes=False),
        )
    assert rc == 0
    err = capsys.readouterr().err
    assert f"dispatch plan -> {HOST}" in err
    assert f"{HOST}:{WS}/" in err
    assert "nothing is deleted on the host" in err
    flat = " ".join(err.split())
    assert "never removes a path that exists only on the host" in flat
    assert "a path whose type differs from the local one (file, directory, symlink) is replaced" in flat
    assert "additive rsync, nothing deleted" not in flat
    assert not hasattr(rd, "typer")


# --- check_remote_workspace -----------------------------------------------------

RWS = "/home/u/work/peridio-wrynose"


def _layer_conf(series: str) -> str:
    return f'BBFILE_COLLECTIONS += "core"\nLAYERSERIES_CORENAMES = "{series}"\n'


def _check(monkeypatch: pytest.MonkeyPatch, stdout: str, rc: int = 0, stderr: str = "") -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def capture(host: str, script: str, *, batch_mode: bool = True) -> _Result:
        calls.append((host, script))
        return _Result(rc, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(rd, "_ssh_bash_capture", capture)
    return calls


_OK_HEAD = "BAKAR_WS_EXISTS=1\nBAKAR_WS_GIT=1\nBAKAR_WS_LAYERCONF=1\n"


def test_check_remote_workspace_accepts_a_matching_series(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _check(monkeypatch, _OK_HEAD + _layer_conf("wrynose"))
    rd.check_remote_workspace(HOST, RWS, "wrynose")
    assert len(calls) == 1
    assert calls[0][0] == HOST
    assert shlex.quote(RWS) in calls[0][1]


def test_check_remote_workspace_refuses_a_series_mismatch_naming_both(monkeypatch: pytest.MonkeyPatch) -> None:
    # The check takes no assume_yes parameter at all, so --yes cannot bypass it.
    assert "assume_yes" not in inspect.signature(rd.check_remote_workspace).parameters
    _check(monkeypatch, _OK_HEAD + _layer_conf("wrynose"))
    with pytest.raises(rd.DispatchError) as excinfo:
        rd.check_remote_workspace(HOST, RWS, "scarthgap")
    assert "'scarthgap'" in str(excinfo.value)
    assert "'wrynose'" in str(excinfo.value)


def test_check_remote_workspace_missing_lists_the_one_time_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    _check(monkeypatch, "BAKAR_WS_EXISTS=0\n")
    with pytest.raises(rd.DispatchError) as excinfo:
        rd.check_remote_workspace(HOST, RWS, "wrynose")
    msg = str(excinfo.value)
    assert f"mkdir -p {RWS}" in msg
    assert f"git clone --branch wrynose <meta-avocado origin URL> {RWS}/meta-avocado" in msg
    assert f"env KAS_WORK_DIR={RWS} kas checkout {RWS}/meta-avocado/kas/machine/" in msg


def test_check_remote_workspace_refuses_a_non_git_meta_avocado(monkeypatch: pytest.MonkeyPatch) -> None:
    _check(monkeypatch, "BAKAR_WS_EXISTS=1\nBAKAR_WS_GIT=0\nBAKAR_WS_LAYERCONF=1\n" + _layer_conf("wrynose"))
    with pytest.raises(rd.DispatchError, match=f"{RWS}/meta-avocado"):
        rd.check_remote_workspace(HOST, RWS, "wrynose")


def test_check_remote_workspace_refuses_a_missing_layer_conf(monkeypatch: pytest.MonkeyPatch) -> None:
    _check(monkeypatch, "BAKAR_WS_EXISTS=1\nBAKAR_WS_GIT=1\nBAKAR_WS_LAYERCONF=0\n")
    with pytest.raises(rd.DispatchError, match=r"missing or unreadable"):
        rd.check_remote_workspace(HOST, RWS, "wrynose")


def test_check_remote_workspace_refuses_a_layer_conf_without_a_series(monkeypatch: pytest.MonkeyPatch) -> None:
    _check(monkeypatch, _OK_HEAD + 'BBFILE_COLLECTIONS += "core"\n')
    with pytest.raises(rd.DispatchError, match="LAYERSERIES_CORENAMES"):
        rd.check_remote_workspace(HOST, RWS, "wrynose")


def test_check_remote_workspace_surfaces_an_ssh_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _check(monkeypatch, "", rc=255, stderr="Permission denied (publickey).")
    with pytest.raises(rd.DispatchError, match="Permission denied"):
        rd.check_remote_workspace(HOST, RWS, "wrynose")


def test_check_remote_workspace_refuses_garbled_output(monkeypatch: pytest.MonkeyPatch) -> None:
    _check(monkeypatch, "Welcome to fish\n")
    with pytest.raises(rd.DispatchError, match="unexpected output"):
        rd.check_remote_workspace(HOST, RWS, "wrynose")


def _run_check_script_locally(ws: Path) -> str:
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - bash is present on every supported host
        pytest.skip("bash is not installed")
    result = subprocess.run(
        [bash, "-s"], input=rd.build_workspace_check_script(str(ws)), capture_output=True, text=True, check=True
    )
    return result.stdout


def test_workspace_check_script_reads_a_real_layout_and_writes_nothing(tmp_path: Path) -> None:
    ws = tmp_path / "peridio-wrynose"
    assert _run_check_script_locally(ws) == "BAKAR_WS_EXISTS=0\n"
    assert not ws.exists()

    (ws / "meta-avocado").mkdir(parents=True)
    conf = ws / "openembedded-core" / "meta" / "conf"
    conf.mkdir(parents=True)
    (conf / "layer.conf").write_text(_layer_conf("wrynose"))
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    out = _run_check_script_locally(ws)
    assert out.startswith("BAKAR_WS_EXISTS=1\nBAKAR_WS_GIT=0\nBAKAR_WS_LAYERCONF=1\n")
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before

    subprocess.run(["git", "init", "-q", str(ws / "meta-avocado")], check=True, capture_output=True)
    out = _run_check_script_locally(ws)
    assert out.splitlines()[1] == "BAKAR_WS_GIT=1"
    assert rd.parse_layerseries_corenames("\n".join(out.splitlines()[3:])) == "wrynose"


def test_workspace_check_script_is_valid_bash() -> None:
    error = _bash_syntax_error(rd.build_workspace_check_script("/home/u/work/peridio wrynose"))
    assert error is None, error


# --- bounded dispatch network operations --------------------------------------


def _ssh_runs(fake: FakeSubprocess) -> list[tuple[list[str], dict]]:
    return [
        (a, k)
        for (kind, a), k in zip((c for c in fake.calls if c[0] == "run"), fake.run_kwargs, strict=True)
        if a[0] == "ssh"
    ]


def test_every_ssh_argv_carries_keepalive_and_connect_bounds(fake_sp: FakeSubprocess) -> None:
    rd._ssh_bash_capture(HOST, "true")
    rd._ssh_bash_capture(HOST, "true", batch_mode=False)
    rd.preflight_remote(HOST)
    rd._ssh_bash_popen(HOST)
    argvs = [a for _, a in fake_sp.calls]
    batch = ["-o", "BatchMode=yes"]
    # Options first, then the host, then the remote command - nothing in between.
    # Order of the four call paths: batched capture, unbatched capture, preflight, popen.
    assert argvs == [
        [*_SSH, *batch, HOST, "bash", "-s"],
        [*_SSH, HOST, "bash", "-s"],
        [*_SSH, *batch, HOST, "bash", "-s"],
        [*_SSH, HOST, "bash", "-s"],
    ]


def test_batch_mode_survives_where_it_was_set_and_stays_off_where_it_was_not(fake_sp: FakeSubprocess) -> None:
    rd._ssh_bash_capture(HOST, "true")
    rd._ssh_bash_capture(HOST, "true", batch_mode=False)
    rd._ssh_bash_popen(HOST)
    rd.preflight_remote(HOST)
    (batched, _), (unbatched, _), (pre, _) = _ssh_runs(fake_sp)
    assert "BatchMode=yes" in batched
    assert "BatchMode=yes" not in unbatched
    assert "BatchMode=yes" in pre
    popen_argv = next(a for k, a in fake_sp.calls if k == "Popen")
    assert "BatchMode=yes" not in popen_argv


def test_rsync_argv_leaves_rsync_rsh_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RSYNC_RSH", "ssh -i mykey")
    argv = rd.build_rsync_argv(Path("/ws"), HOST)
    assert "-e" not in argv
    assert "--timeout=60" in argv


def test_ssh_bash_capture_timeout_is_overridable(fake_sp: FakeSubprocess) -> None:
    rd._ssh_bash_capture(HOST, "true", timeout=600)
    ((_, kwargs),) = _ssh_runs(fake_sp)
    assert kwargs["timeout"] == 600


def test_rsync_argv_passes_ssh_options_and_io_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RSYNC_RSH", raising=False)
    argv = rd.build_rsync_argv(Path("/ws"), HOST)
    assert "--timeout=60" in argv
    assert argv[argv.index("-e") + 1] == shlex.join(_SSH)
    assert not any(a.startswith("--delete") for a in argv)


def test_probes_carry_a_subprocess_timeout_and_the_stream_does_not(fake_sp: FakeSubprocess) -> None:
    rd._ssh_bash_capture(HOST, "true")
    rd.preflight_remote(HOST)
    for _, kwargs in _ssh_runs(fake_sp):
        assert kwargs["timeout"] == 60
    rd._ssh_bash_popen(HOST)
    assert "timeout" not in fake_sp.popen_kwargs


def test_preflight_timeout_reads_as_unreachable(fake_sp: FakeSubprocess) -> None:
    fake_sp.run_times_out = True
    ok, detail = rd.preflight_remote(HOST)
    assert ok is False
    assert detail is not None
    assert f"could not reach {HOST}" in detail


def test_capture_timeout_takes_the_existing_failure_path(fake_sp: FakeSubprocess) -> None:
    fake_sp.run_times_out = True
    with pytest.raises(rd.DispatchError, match=f"could not reach {HOST}"):
        rd.check_remote_workspace(HOST, "/ws", "wrynose")
    assert rd._running_dispatch_units(HOST) is None
    # No write was attempted: only ssh probes ran, no rsync.
    assert not any(a[0] == "rsync" for _, a in fake_sp.calls)


# --- run-id lookup: pick by id, prune every tmp* ---------------------------------


def _real_find(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Run the lookup's script through a local ``bash -s`` standing in for the remote."""
    scripts: list[str] = []

    def capture(host: str, script: str, *, batch_mode: bool = True) -> subprocess.CompletedProcess[str]:
        scripts.append(script)
        return subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True, check=False)

    monkeypatch.setattr(rd, "_ssh_bash_capture", capture)
    return scripts


def _run_dir(root: Path, build: str, run_id: str, mtime: int | None = None) -> Path:
    path = root / build / "runs" / run_id
    path.mkdir(parents=True)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_lookup_picks_the_greatest_id_not_the_newest_mtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _real_find(monkeypatch)
    # This dispatch's run, then an older run in another build dir that was touched
    # afterwards (its mtime is the newest on the host).
    _run_dir(tmp_path, "build", "20260716-120500-111", mtime=1_000)
    _run_dir(tmp_path, "build-qcom", "20260716-110000-222", mtime=9_000)
    assert rd._discover_newest_run_id(HOST, tmp_path, "20260716-120000") == "20260716-120500-111"


def test_lookup_with_a_bumped_older_run_still_finds_this_dispatchs_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_find(monkeypatch)
    _run_dir(tmp_path, "build", "20260716-100000-1", mtime=9_000)
    _run_dir(tmp_path, "build", "20260716-120500-2", mtime=1_000)
    assert rd._discover_newest_run_id(HOST, tmp_path, "20260716-120000") == "20260716-120500-2"


def test_lookup_returns_none_when_every_run_predates_the_fence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _real_find(monkeypatch)
    _run_dir(tmp_path, "build", "20260716-100000-1", mtime=9_000)
    assert rd._discover_newest_run_id(HOST, tmp_path, "20260716-120000") is None
    assert rd._discover_newest_run_id(HOST, tmp_path) == "20260716-100000-1"


def test_lookup_fence_accepts_a_run_id_in_the_fence_second(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _real_find(monkeypatch)
    _run_dir(tmp_path, "build", "20260716-120000-77")
    assert rd._discover_newest_run_id(HOST, tmp_path, "20260716-120000") == "20260716-120000-77"


def test_lookup_ignores_leftover_dirs_named_after_a_run_in_the_same_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_find(monkeypatch)
    _run_dir(tmp_path, "build", "20260716-120500-1")
    # Both sort after the real id as strings.
    _run_dir(tmp_path, "build", "20260716-120500-1.bak")
    _run_dir(tmp_path, "build", "20260716-120500-x-old")
    assert rd._discover_newest_run_id(HOST, tmp_path, "20260716-120000") == "20260716-120500-1"
    assert rd._discover_newest_run_id(HOST, tmp_path) == "20260716-120500-1"


def test_lookup_with_fence_reports_the_earliest_run_not_a_later_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_find(monkeypatch)
    _run_dir(tmp_path, "build", "20260716-120500-111", mtime=1_000)
    # A later build in another build dir with the newest mtime.
    _run_dir(tmp_path, "build-qcom", "20260716-130000-222", mtime=9_000)
    assert rd._discover_newest_run_id(HOST, tmp_path, "20260716-120000") == "20260716-120500-111"


def test_lookup_without_fence_still_reports_the_greatest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _real_find(monkeypatch)
    _run_dir(tmp_path, "build", "20260716-120500-111", mtime=9_000)
    _run_dir(tmp_path, "build-qcom", "20260716-130000-222", mtime=1_000)
    assert rd._discover_newest_run_id(HOST, tmp_path) == "20260716-130000-222"


def test_lookup_same_second_runs_report_the_name_that_sorts_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_find(monkeypatch)
    _run_dir(tmp_path, "build", "20260716-120500-9")
    _run_dir(tmp_path, "build-qcom", "20260716-120500-10")
    assert rd._discover_newest_run_id(HOST, tmp_path, "20260716-120000") == "20260716-120500-10"


def test_lookup_does_not_walk_a_tmp_glibc_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = _real_find(monkeypatch)
    _run_dir(tmp_path, "build-qcom", "20260716-120000-1")
    # A decoy that only a walk into the TMPDIR would find.
    _run_dir(tmp_path / "build-qcom", "tmp-glibc/work/x", "29990101-000000-9")
    assert rd._discover_newest_run_id(HOST, tmp_path) == "20260716-120000-1"
    assert "-name 'tmp*' -prune" in scripts[0]


def test_lookup_timeout_is_reported_as_a_lookup_timeout_not_unreachable(
    fake_sp: FakeSubprocess, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_sp.run_times_out = True
    result = rd._discover_newest_run_id(HOST, WS)
    assert isinstance(result, rd.RunLookupFailed)
    assert "lookup timed out" in result.detail
    assert "could not reach" not in result.detail
    rd._surface_run_id(HOST, WS, ["ok\n"], 0)
    out = capsys.readouterr()
    assert "could not reach" not in out.out + out.err


def test_surface_run_id_passes_the_dispatch_start_as_the_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    def discover(host: str, ws_root: Path, not_before: str | None = None) -> str | None:
        seen.append(not_before)
        return None

    monkeypatch.setattr(rd, "_discover_newest_run_id", discover)
    rd._surface_run_id(HOST, WS, ["BAKAR_DISPATCH_START=20260716-120000\n"], 0)
    assert seen == ["20260716-120000"]


# --- capture timeout is a typed result --------------------------------------------


def test_capture_timeout_is_typed_and_keeps_the_probe_wording(fake_sp: FakeSubprocess) -> None:
    fake_sp.run_times_out = True
    result = rd._ssh_bash_capture(HOST, "true", timeout=600)
    assert isinstance(result, rd.CaptureTimedOut)
    assert result.returncode == 255
    assert result.timeout == 600
    assert f"could not reach {HOST}" in result.stderr


def test_interrupt_message_after_a_unit_marker_does_not_claim_it_is_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _interrupt(host: str, script: str):
        rd._stream_state.unit_seen = True
        raise KeyboardInterrupt

    monkeypatch.setattr(rd, "_stream_remote_build", _interrupt)
    assert rd.run_remote_build(HOST, "script", WS) == 130
    cap = capsys.readouterr()
    out = cap.out + cap.err
    assert "a unit was requested" in out
    assert f"`bakar ps` / `bakar stop` on {HOST}" in out
    assert "if it started" in out
