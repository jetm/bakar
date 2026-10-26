"""Tests for the workspace repository lock that serialises classify through the dump."""

from __future__ import annotations

import fcntl
import subprocess
import sys
import threading
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bakar.config import BuildConfig
from bakar.steps import kas_build, repo_reuse
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

    def classify(_repos: dict, _ws: Path) -> list[RepoState]:
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


@pytest.fixture(autouse=True)
def _drop_in_use_holds() -> Iterator[None]:
    yield
    for fh in repo_reuse._IN_USE_HELD.values():
        fh.close()
    repo_reuse._IN_USE_HELD.clear()


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
