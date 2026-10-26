"""Tests for the workspace repository lock that serialises classify through the dump."""

from __future__ import annotations

import contextlib
import fcntl
import shutil
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bakar import build_stop
from bakar.config import BuildConfig
from bakar.observability import RunLogger
from bakar.steps import kas_build, kas_pty, repo_reuse
from bakar.steps.build_ui import BuildUIState
from bakar.steps.repo_reuse import RepoState, workspace_lock

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

PIN = "0123456789abcdef0123456789abcdef01234567"
DUMP_YAML = "header:\n  version: 16\nrepos: {}\n"
_WAIT_S = 2.0

# Holds an exclusive flock on argv[1] until stdin closes.
_HOLDER = (
    "import fcntl, sys\n"
    "fh = open(sys.argv[1], 'a')\n"
    "fcntl.flock(fh, fcntl.LOCK_EX)\n"
    "print('locked', flush=True)\n"
    "sys.stdin.read()\n"
)


def _hold(lock: Path) -> subprocess.Popen[str]:
    lock.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "locked"
    return proc


def _release(proc: subprocess.Popen[str]) -> None:
    assert proc.stdin is not None
    proc.stdin.close()
    proc.wait(timeout=_WAIT_S)


def _is_locked(lock: Path) -> bool:
    with lock.open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False


def test_lock_file_lives_under_dot_bakar(tmp_path: Path) -> None:
    lock = tmp_path / ".bakar" / "repos.lock"
    with workspace_lock(tmp_path):
        assert lock.is_file()
        assert _is_locked(lock)
    assert not _is_locked(lock)


def test_lock_waits_for_another_process(tmp_path: Path) -> None:
    holder = _hold(tmp_path / ".bakar" / "repos.lock")
    entered = threading.Event()

    def take() -> None:
        with workspace_lock(tmp_path):
            entered.set()

    worker = threading.Thread(target=take, daemon=True)
    try:
        worker.start()
        assert not entered.wait(0.3)
    finally:
        _release(holder)
    assert entered.wait(_WAIT_S)
    worker.join(_WAIT_S)


def test_waiter_names_the_lock_then_gives_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = tmp_path / ".bakar" / "repos.lock"
    holder = _hold(lock)
    # The real notice comes after 3 s; the limit here is 1 s.
    monkeypatch.setattr(repo_reuse, "_LOCK_NOTICE_AFTER_S", 0.2)
    try:
        with pytest.raises(RuntimeError) as exc, workspace_lock(tmp_path, wait_limit_s=1):
            raise AssertionError("the body must not run while another process holds the lock")
    finally:
        _release(holder)
    assert str(lock) in str(exc.value)
    assert "1s" in str(exc.value)
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert str(lock) in err
    assert "another bakar build" in err


def test_short_wait_prints_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    holder = _hold(tmp_path / ".bakar" / "repos.lock")
    timer = threading.Timer(0.3, _release, args=(holder,))
    timer.start()
    try:
        with workspace_lock(tmp_path, wait_limit_s=5):
            pass
    finally:
        timer.join(_WAIT_S)
    assert capsys.readouterr().err == ""


def test_lock_is_released_when_the_body_raises(tmp_path: Path) -> None:
    lock = tmp_path / ".bakar" / "repos.lock"
    with pytest.raises(ValueError, match="boom"), workspace_lock(tmp_path):
        assert _is_locked(lock)
        raise ValueError("boom")
    assert not _is_locked(lock)


def _cfg(tmp_path: Path) -> tuple[BuildConfig, Path, Path]:
    kas_yaml = tmp_path / "meta-avocado" / "kas" / "machine" / "m.yml"
    kas_yaml.parent.mkdir(parents=True)
    kas_yaml.write_text("header:\n  version: 16\n", encoding="utf-8")
    cfg = BuildConfig(
        workspace=tmp_path,
        bsp_family="generic",  # type: ignore[arg-type]
        machine="generic",
        distro="generic",
        image="generic",
        manifest="",
        repo_url="",
        repo_branch="",
        kas_container_image="jetm/kas-build-env:latest",
        kas_yaml_override=kas_yaml,
    )
    cfg.bsp_root.mkdir(parents=True, exist_ok=True)
    overlay = tmp_path / "overlay.yml"
    overlay.write_text("header:\n  version: 16\n", encoding="utf-8")
    return cfg, kas_yaml, overlay


def test_build_kas_arg_holds_the_lock_from_classify_through_the_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, kas_yaml, overlay = _cfg(tmp_path)
    lock = tmp_path / ".bakar" / "repos.lock"
    seen: dict[str, bool] = {}

    def classify(_repos: dict, _ws: Path, **_kwargs: object) -> list[RepoState]:
        seen["classify"] = _is_locked(lock)
        return [RepoState("meta-foo", tmp_path / "meta-foo", PIN, "missing")]

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["dump"] = _is_locked(lock)
        return subprocess.CompletedProcess(argv, 0, DUMP_YAML, "")

    holder = _hold(lock)
    done = threading.Event()

    def build() -> None:
        kas_build._build_kas_arg(cfg, kas_yaml, overlay)
        done.set()

    with (
        patch.object(kas_build.kas_pins, "read_merged_config", return_value={"repos": {}}),
        patch.object(kas_build.repo_reuse, "classify", side_effect=classify) as classify_mock,
        patch("bakar.steps.kas_build.subprocess.run", side_effect=run),
    ):
        worker = threading.Thread(target=build, daemon=True)
        try:
            worker.start()
            assert not done.wait(0.3)
            classify_mock.assert_not_called()
        finally:
            _release(holder)
        assert done.wait(_WAIT_S)
        worker.join(_WAIT_S)

    assert seen == {"classify": True, "dump": True}
    assert not _is_locked(lock)


def test_kas_policy_takes_no_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BAKAR_REPO_POLICY", raising=False)
    cfg, kas_yaml, overlay = _cfg(tmp_path)

    with patch(
        "bakar.steps.kas_build.subprocess.run",
        return_value=subprocess.CompletedProcess(["kas"], 0, DUMP_YAML, ""),
    ):
        kas_build._build_kas_arg(cfg, kas_yaml, overlay)

    assert not (tmp_path / ".bakar" / "repos.lock").exists()


def _flock_state(lock: Path, mode: int) -> bool:
    """True when a fresh open file description can take ``mode`` without blocking."""
    with lock.open("a") as fh:
        try:
            fcntl.flock(fh, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fh, fcntl.LOCK_UN)
        return True


def test_reuse_build_holds_in_use_lock_shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, kas_yaml, overlay = _cfg(tmp_path)
    lock = tmp_path / ".bakar" / repo_reuse.IN_USE_LOCK_NAME

    with (
        patch.object(kas_build.kas_pins, "read_merged_config", return_value={"repos": {}}),
        patch.object(
            kas_build.repo_reuse,
            "classify",
            return_value=[RepoState("meta-foo", tmp_path / "meta-foo", PIN, "missing")],
        ),
        patch(
            "bakar.steps.kas_build.subprocess.run",
            return_value=subprocess.CompletedProcess(["kas"], 0, DUMP_YAML, ""),
        ),
    ):
        kas_build._build_kas_arg(cfg, kas_yaml, overlay)

    assert lock.is_file()
    assert not _flock_state(lock, fcntl.LOCK_EX)
    assert _flock_state(lock, fcntl.LOCK_SH)


def test_in_use_lock_waits_then_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = tmp_path / ".bakar" / repo_reuse.IN_USE_LOCK_NAME
    holder = _hold(lock)
    monkeypatch.setattr(repo_reuse, "_LOCK_NOTICE_AFTER_S", 0.2)
    try:
        with pytest.raises(RuntimeError) as exc:
            repo_reuse.hold_workspace_in_use(tmp_path, wait_limit_s=1)
    finally:
        _release(holder)
    assert str(lock) in str(exc.value)
    err = capsys.readouterr().err
    assert f"waiting for {lock}: a bakar sync holds it" in err
    assert err.count("\n") == 1
    assert tmp_path.resolve() not in repo_reuse._IN_USE_HELD


_TAKE_SHARED = (
    "import sys\n"
    "from pathlib import Path\n"
    "from bakar.steps.repo_reuse import hold_workspace_in_use\n"
    "hold_workspace_in_use(Path(sys.argv[1]))\n"
    "print('held', flush=True)\n"
)


def test_in_use_lock_released_when_process_exits(tmp_path: Path) -> None:
    lock = tmp_path / ".bakar" / repo_reuse.IN_USE_LOCK_NAME
    proc = subprocess.run(
        [sys.executable, "-c", _TAKE_SHARED, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert proc.stdout.strip() == "held"
    assert lock.is_file()
    assert _flock_state(lock, fcntl.LOCK_EX)


def test_in_use_lock_holders_share(tmp_path: Path) -> None:
    lock = tmp_path / ".bakar" / repo_reuse.IN_USE_LOCK_NAME
    repo_reuse.hold_workspace_in_use(tmp_path, wait_limit_s=1)
    repo_reuse.hold_workspace_in_use(tmp_path, wait_limit_s=1)
    with lock.open("a") as other:
        fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(other, fcntl.LOCK_UN)
    assert len(repo_reuse._IN_USE_HELD) == 1


def test_keep_in_use_until_exit_outlives_the_front_end(tmp_path: Path) -> None:
    lock = tmp_path / ".bakar" / repo_reuse.IN_USE_LOCK_NAME
    repo_reuse.hold_workspace_in_use(tmp_path, wait_limit_s=1)
    build = subprocess.Popen(["sleep", "30"])
    try:
        repo_reuse.keep_in_use_until_exit(tmp_path, build)
        time.sleep(0.5)  # the detached holder takes its shared hold while the front end still has its own
        repo_reuse._IN_USE_HELD.pop(tmp_path.resolve()).close()  # the front end dies
        assert not _flock_state(lock, fcntl.LOCK_EX)
    finally:
        build.kill()
        build.wait()
    deadline = time.monotonic() + 5
    while not _flock_state(lock, fcntl.LOCK_EX) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _flock_state(lock, fcntl.LOCK_EX)


def test_keep_in_use_until_exit_is_a_noop_without_a_hold(tmp_path: Path) -> None:
    with patch("bakar.steps.repo_reuse.subprocess.Popen") as popen:
        repo_reuse.keep_in_use_until_exit(tmp_path, _FakeProc())
    popen.assert_not_called()


# Inside a fresh user and PID namespace: the build exits, an impostor is forced
# onto its PID through ns_last_pid, and the lock must still come free.
_PID_REUSE = (
    "import fcntl, subprocess, sys, time\n"
    "from pathlib import Path\n"
    "from bakar.steps import repo_reuse\n"
    "ws = Path(sys.argv[1])\n"
    "lock = ws / '.bakar' / repo_reuse.IN_USE_LOCK_NAME\n"
    "repo_reuse.hold_workspace_in_use(ws, wait_limit_s=1)\n"
    "build = subprocess.Popen(['sleep', '30'])\n"
    "repo_reuse.keep_in_use_until_exit(ws, build)\n"
    "time.sleep(0.5)\n"
    "repo_reuse._IN_USE_HELD.pop(ws.resolve()).close()\n"
    "build.kill()\n"
    "build.wait()\n"
    "Path('/proc/sys/kernel/ns_last_pid').write_text(str(build.pid - 1))\n"
    "impostor = subprocess.Popen(['sleep', '30'])\n"
    "print('reused' if impostor.pid == build.pid else 'not-reused')\n"
    "def free():\n"
    "    with lock.open('a') as fh:\n"
    "        try:\n"
    "            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "        except BlockingIOError:\n"
    "            return False\n"
    "        return True\n"
    "deadline = time.monotonic() + 4\n"
    "while not free() and time.monotonic() < deadline:\n"
    "    time.sleep(0.1)\n"
    "print('released' if free() else 'held')\n"
    "impostor.kill()\n"
)


def _can_force_pid_reuse() -> bool:
    if shutil.which("unshare") is None:
        return False
    probe = subprocess.run(
        ["unshare", "-Urpf", "--mount-proc", "sh", "-c", "echo 1 > /proc/sys/kernel/ns_last_pid"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


@pytest.mark.skipif(not _can_force_pid_reuse(), reason="needs an unprivileged user and PID namespace")
def test_keep_in_use_until_exit_ignores_a_reused_pid(tmp_path: Path) -> None:
    proc = subprocess.run(
        ["unshare", "-Urpf", "--mount-proc", sys.executable, "-c", _PID_REUSE, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["reused", "released"]


# ---------------------------------------------------------------------------
# Every kas spawn path that took the whole-run lock hands it to a holder
# ---------------------------------------------------------------------------


class _FakeProc:
    pid = 4242
    returncode: int | None = None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0
        return 0

    def kill(self) -> None:
        pass

    def __enter__(self) -> _FakeProc:
        return self

    def __exit__(self, *_exc: object) -> None:
        pass


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[kas_build.KasBuildContext, list]]:
    """A kas context whose kas arg takes the reuse lock, with every spawn faked."""
    cfg, kas_yaml, overlay = _cfg(tmp_path)

    def build_kas_arg(cfg: BuildConfig, *_args: object) -> str:
        repo_reuse.hold_workspace_in_use(cfg.workspace, wait_limit_s=1)
        return "avocado-bakar.yml"

    @contextlib.contextmanager
    def no_marker(_cfg: object, _log: object) -> Iterator[None]:
        yield

    holders: list[tuple[Path, int]] = []
    monkeypatch.setattr(kas_build, "_build_kas_arg", build_kas_arg)
    monkeypatch.setattr(kas_build, "_build_env", lambda *_a, **_k: {})
    monkeypatch.setattr(kas_build, "cache_mount_refusal", lambda *_a, **_k: None)
    monkeypatch.setattr(
        kas_build, "clear_stale_bitbake_locks", lambda _cfg: build_stop.LockClearOutcome(removed=[], refusal=None)
    )
    monkeypatch.setattr(kas_build, "lock_owner_marker", no_marker)
    monkeypatch.setattr(kas_build.subprocess, "Popen", lambda *_a, **_k: _FakeProc())
    monkeypatch.setattr(
        kas_build.repo_reuse, "keep_in_use_until_exit", lambda ws, child: holders.append((ws, child.pid))
    )
    log = RunLogger(runs_dir=cfg.runs_dir)
    with log:
        yield kas_build.KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay), holders


def test_run_shell_hands_the_lock_to_a_holder(wired: tuple[kas_build.KasBuildContext, list]) -> None:
    ctx, holders = wired
    assert kas_build.run_shell(ctx, [], "true") == 0
    assert holders == [(ctx.cfg.workspace, _FakeProc.pid)]


def test_run_shell_capture_hands_the_lock_to_a_holder(
    wired: tuple[kas_build.KasBuildContext, list], tmp_path: Path
) -> None:
    ctx, holders = wired
    capture = kas_build.ShellCapture(stdout_path=tmp_path / "out.log")
    assert kas_build.run_shell_capture(ctx, "true", capture) == 0
    assert holders == [(ctx.cfg.workspace, _FakeProc.pid)]


@pytest.mark.parametrize("capture", [False, True])
def test_run_kas_subcommand_hands_the_lock_to_a_holder(
    wired: tuple[kas_build.KasBuildContext, list], tmp_path: Path, *, capture: bool
) -> None:
    ctx, holders = wired
    invocation = kas_build.KasSubcommand(
        subcommand="dump", extra_args=[], capture_to=tmp_path / "dump.yml" if capture else None
    )
    assert kas_build.run_kas_subcommand(ctx, invocation) == 0
    assert holders == [(ctx.cfg.workspace, _FakeProc.pid)]


def test_run_kas_subcommand_timeout_kills_the_held_child(
    wired: tuple[kas_build.KasBuildContext, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _holders = wired
    killed: list[bool] = []

    class _Hung(_FakeProc):
        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None and not killed:
                raise subprocess.TimeoutExpired(cmd="kas", timeout=timeout)
            return super().wait()

        def kill(self) -> None:
            killed.append(True)

    monkeypatch.setattr(kas_build.subprocess, "Popen", lambda *_a, **_k: _Hung())
    monkeypatch.setattr(kas_build.build_stop, "escalate_container_tree", lambda _run_id: True)
    invocation = kas_build.KasSubcommand(subcommand="dump", extra_args=[], timeout=5.0)
    assert kas_build.run_kas_subcommand(ctx, invocation) == 124
    assert killed == [True]


class _StopError(Exception):
    pass


def test_pty_build_hands_the_lock_to_a_holder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, _kas_yaml, _overlay = _cfg(tmp_path)
    spawned: list[int] = []
    holders: list[tuple[Path, int]] = []
    real_popen = subprocess.Popen

    def popen(*args: object, **kwargs: object) -> subprocess.Popen:
        proc = real_popen(*args, **kwargs)  # ty: ignore[no-matching-overload]
        spawned.append(proc.pid)
        return proc

    def keep(ws: Path, child: subprocess.Popen) -> None:
        holders.append((ws, child.pid))
        raise _StopError

    def launch_record(*_a: object, **_k: object) -> None:
        raise _StopError

    monkeypatch.setattr(kas_build, "_build_env", lambda *_a, **_k: {})
    monkeypatch.setattr(kas_pty.subprocess, "Popen", popen)
    monkeypatch.setattr(kas_pty.repo_reuse, "keep_in_use_until_exit", keep)
    monkeypatch.setattr(kas_pty.build_stop, "write_launch_record", launch_record)
    with RunLogger(runs_dir=cfg.runs_dir) as log:
        pty_ctx = kas_pty._PtyCtx(
            cmd=["true"],
            cfg=cfg,
            log=log,
            ui=BuildUIState(start_monotonic=time.monotonic()),
            stop_event=threading.Event(),
        )
        with pytest.raises(_StopError):
            kas_pty._run_pty_with_ui(pty_ctx)
    assert spawned
    assert holders == [(cfg.workspace, spawned[0])]
