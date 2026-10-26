"""Tests for the read-only host pin probe (``bakar.steps.pin_probe``)."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from bakar.steps import pin_probe, remote_dispatch
from bakar.steps.pin_probe import PinProbeIncomplete, parse_probe, probe_host, probe_script
from bakar.steps.repo_reuse import PinnedRepo, RepoFacts, gather_local_facts, pinned_entries

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}
_MISSING_PIN = "deadbeef" * 5


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True, env=_ENV)
    return out.stdout.strip()


def _make_repo(path: Path, commits: int = 1) -> list[str]:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    shas = []
    for i in range(commits):
        (path / "f.txt").write_text(f"v{i}\n")
        _git(path, "add", "f.txt")
        _git(path, "commit", "-q", "-m", f"c{i}")
        shas.append(_git(path, "rev-parse", "HEAD"))
    return shas


def _probe_local_raw(entries: list[PinnedRepo]) -> str:
    proc = subprocess.run(
        ["bash", "-s"], input=probe_script(entries), capture_output=True, text=True, check=True, env=_ENV
    )
    return proc.stdout


def _missing(ws: Path) -> dict:
    return {"r": {"url": "https://x/r", "commit": "abcdef1234"}}


def _clean_at_pin(ws: Path) -> dict:
    (sha,) = _make_repo(ws / "r")
    return {"r": {"url": "https://x/r", "commit": sha}}


def _off_pin_with_object(ws: Path) -> dict:
    first, _second = _make_repo(ws / "r", commits=2)
    return {"r": {"url": "https://x/r", "commit": first}}


def _off_pin_without_object(ws: Path) -> dict:
    _make_repo(ws / "r")
    return {"r": {"url": "https://x/r", "commit": _MISSING_PIN}}


def _dirty(ws: Path) -> dict:
    (sha,) = _make_repo(ws / "r")
    (ws / "r" / "untracked").write_text("x")
    return {"r": {"url": "https://x/r", "commit": sha}}


def _linked_worktree(ws: Path) -> dict:
    (sha,) = _make_repo(ws / "main-repo")
    _git(ws / "main-repo", "worktree", "add", "-q", "--detach", str(ws / "r"), sha)
    return {"r": {"url": "https://x/r", "commit": sha}}


def _side(ws: Path, dirty: bool) -> dict:
    first, _second = _make_repo(ws / "r", commits=2)
    side = ws / ".bakar" / "pins" / "r" / first[:12]
    _git(ws / "r", "worktree", "add", "-q", "--detach", str(side), first)
    if dirty:
        (side / "untracked").write_text("x")
    return {"r": {"url": "https://x/r", "commit": first}}


def _no_url(ws: Path) -> dict:
    (sha,) = _make_repo(ws / "r")
    return {"local": {"path": "layers/local"}, "r": {"url": "https://x/r", "commit": sha}}


SCENARIOS = {
    "missing": _missing,
    "clean_at_pin": _clean_at_pin,
    "off_pin_with_object": _off_pin_with_object,
    "off_pin_without_object": _off_pin_without_object,
    "dirty": _dirty,
    "linked_worktree": _linked_worktree,
    "side_clean": lambda ws: _side(ws, dirty=False),
    "side_dirty": lambda ws: _side(ws, dirty=True),
    "no_url": _no_url,
}


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_probe_parity(tmp_path: Path, scenario: str) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    entries = pinned_entries(SCENARIOS[scenario](ws), ws)
    expected = gather_local_facts(entries)
    assert parse_probe(_probe_local_raw(entries), entries) == expected
    if scenario == "no_url":
        assert len(entries) == 1
        assert "local" not in probe_script(entries)
    if scenario == "missing":
        assert expected[0] == RepoFacts.unknown()


def test_probe_incomplete_when_done_count_short(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    entries = pinned_entries(_clean_at_pin(ws), ws)
    out = _probe_local_raw(entries)
    with pytest.raises(PinProbeIncomplete):
        parse_probe(out.replace("BAKAR_PIN_DONE\t1", "BAKAR_PIN_DONE\t0"), entries)
    with pytest.raises(PinProbeIncomplete):
        parse_probe("", entries)
    with pytest.raises(PinProbeIncomplete):
        parse_probe("BAKAR_PIN\t0\tbogus\nBAKAR_PIN_DONE\t1\n", entries)
    dup = "\n".join([*[ln for ln in out.splitlines() if ln.startswith("BAKAR_PIN\t")] * 2, "BAKAR_PIN_DONE\t1"])
    with pytest.raises(PinProbeIncomplete):
        parse_probe(dup, entries)


def test_probe_script_quotes_hostile_paths(tmp_path: Path) -> None:
    hostile = tmp_path / "a$(touch pwned) ; touch pwned2"
    entries = [PinnedRepo("r", "https://x/r", "abcdef1234", hostile, tmp_path / "side $(touch pwned3)")]
    proc = subprocess.run(
        ["bash", "-s"], input=probe_script(entries), capture_output=True, text=True, check=True, cwd=tmp_path
    )
    assert not (tmp_path / "pwned").exists()
    assert not (tmp_path / "pwned2").exists()
    assert not (tmp_path / "pwned3").exists()
    assert not list(tmp_path.glob("pwned*"))
    assert parse_probe(proc.stdout, entries) == [RepoFacts.unknown()]


def test_probe_skips_non_hex_pins(tmp_path: Path) -> None:
    entries = [
        PinnedRepo("a", "u", "main", tmp_path / "a", None),
        PinnedRepo("b", "u", "abc", tmp_path / "b", None),
        PinnedRepo("c", "u", None, tmp_path / "c", None),
        PinnedRepo("d", "u", "abcd", tmp_path / "d", None),
    ]
    script = probe_script(entries)
    out = _probe_local_raw(entries)
    assert out.count("BAKAR_PIN\t") == 1
    assert out.splitlines()[-1] == "BAKAR_PIN_DONE\t1"
    for name in "abc":
        assert f"p={tmp_path}/{name}\n" not in script
    assert f"p={tmp_path}/d\n" in script
    facts = parse_probe(out, entries)
    assert facts == [RepoFacts.unknown()] * 4


def test_probe_host_timeout_is_incomplete(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    timed_out = remote_dispatch.CaptureTimedOut(["ssh"], 255, stdout="", stderr="timed out")
    timed_out.timeout = 12.0
    monkeypatch.setattr(remote_dispatch, "_ssh_bash_capture", lambda host, script, **kw: timed_out)
    entries = [PinnedRepo("r", "u", "abcd", tmp_path / "r", None)]
    with pytest.raises(PinProbeIncomplete, match="builder"):
        probe_host("builder", entries)


def test_run_bash_local_timeout_keeps_partial_output() -> None:
    rc, out, err, timed_out = pin_probe.run_bash(
        "printf 'first\\n'; printf 'oops\\n' >&2; exec sleep 30", host=None, timeout=1.0
    )
    assert timed_out
    assert (rc, out, err) == (255, "first\n", "oops\n")


@pytest.mark.parametrize(("value", "text"), [(b"part\xff\n", "part�\n"), ("part\n", "part\n"), (None, "")])
def test_timeout_output_decodes_bytes_str_or_none(value: bytes | str | None, text: str) -> None:
    assert remote_dispatch.decode_partial(value) == text


def test_run_bash_local_timeout_with_an_escaped_descendant_still_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    # setsid puts the sleep outside the killed group while it holds stdout, so
    # the drain times out and the partial output comes from the timeout itself.
    monkeypatch.setattr(pin_probe, "_DRAIN_TIMEOUT_S", 0.5)
    rc, out, _err, timed_out = pin_probe.run_bash("setsid sleep 30 & echo $!; wait", host=None, timeout=1.0)
    escaped = int(out.split()[0])
    with contextlib.suppress(ProcessLookupError):
        os.kill(escaped, signal.SIGKILL)
    assert (rc, timed_out) == (255, True)
    assert out == f"{escaped}\n"


def _gone(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError, ProcessLookupError:
        # /proc raises ProcessLookupError, not FileNotFoundError, when the pid
        # disappears between open and read.
        return True
    # A reaped-later zombie is dead too: field 3, after the parenthesised comm.
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


def test_run_bash_local_timeout_kills_the_whole_process_group() -> None:
    # The grandchild stands in for a git fetch the script started; it must not
    # outlive the timeout once the workspace locks are dropped.
    rc, out, _err, timed_out = pin_probe.run_bash("sleep 30 & echo $!; wait", host=None, timeout=1.0)
    assert timed_out
    assert rc == 255
    grandchild = int(out.split()[0])
    deadline = time.monotonic() + 5
    while not _gone(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _gone(grandchild)


def test_run_bash_local_drain_timeout_keeps_output_read_during_the_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    # The escaped descendant prints after the first timeout but inside the drain
    # window, so only the drain's own TimeoutExpired carries that line.
    monkeypatch.setattr(pin_probe, "_DRAIN_TIMEOUT_S", 2.0)
    script = "setsid sh -c 'sleep 1.5; echo late; exec sleep 30' & echo $!; wait"
    rc, out, _err, timed_out = pin_probe.run_bash(script, host=None, timeout=1.0)
    escaped = int(out.split()[0])
    with contextlib.suppress(ProcessLookupError):
        os.kill(escaped, signal.SIGKILL)
    assert (rc, timed_out) == (255, True)
    assert out == f"{escaped}\nlate\n"


def _live_group_members(pgid: int) -> list[int]:
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError, ProcessLookupError:
            continue
        # Field 3 is the state, field 5 the process group.
        if fields[0] != "Z" and int(fields[2]) == pgid:
            members.append(int(entry.name))
    return members


def test_run_bash_local_interrupt_kills_the_whole_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A Ctrl-C mid-sync must not leave the script running once the caller has
    # dropped its locks; start_new_session keeps the terminal's SIGINT away from it.
    bash_pid_file, child_pid_file = tmp_path / "bash", tmp_path / "child"
    script = f"sleep 30 & echo $! >{child_pid_file}; echo $$ >{bash_pid_file}; wait"
    real_communicate = subprocess.Popen.communicate

    def interrupted(self: subprocess.Popen[str], input: str | None = None, timeout: float | None = None) -> object:
        with contextlib.suppress(subprocess.TimeoutExpired):
            real_communicate(self, input, timeout=0.5)
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    try:
        with pytest.raises(KeyboardInterrupt):
            pin_probe.run_bash(script, host=None, timeout=20)
        bash_pid = int(bash_pid_file.read_text())
        child_pid = int(child_pid_file.read_text())
        deadline = time.monotonic() + 5
        while not (_gone(bash_pid) and _gone(child_pid)) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _gone(bash_pid)
        assert _gone(child_pid)
        assert _live_group_members(bash_pid) == []
    finally:
        for pid_file in (child_pid_file, bash_pid_file):
            if pid_file.exists() and not _gone(int(pid_file.read_text())):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)


def _fake_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(f"#!/bin/sh\n{body}\n")
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_run_bash_host_timeout_keeps_partial_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The real producer: an ssh that has printed rows and then hangs.
    _fake_ssh(tmp_path, monkeypatch, "printf 'MOVED\\tr\\n'; printf 'warn\\n' >&2; exec sleep 30")
    assert pin_probe.run_bash("true", host="h", timeout=1.0) == (255, "MOVED\tr\n", "warn\n", True)


def test_probe_host_nonzero_exit_is_incomplete(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    failed = subprocess.CompletedProcess(["ssh"], 255, stdout="", stderr="no route")
    monkeypatch.setattr(remote_dispatch, "_ssh_bash_capture", lambda host, script, **kw: failed)
    with pytest.raises(PinProbeIncomplete, match="no route"):
        probe_host("builder", [PinnedRepo("r", "u", "abcd", tmp_path / "r", None)])


def _snapshot(repo: Path) -> dict:
    git_dir = repo / ".git"
    listing = sorted(str(p.relative_to(git_dir)) for p in git_dir.rglob("*"))
    index = git_dir / "index"
    return {
        "listing": listing,
        "index_mtime": index.stat().st_mtime_ns if index.exists() else None,
        "refs": _git(repo, "for-each-ref"),
    }


def test_probe_script_is_read_only(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    entries = pinned_entries(_dirty(ws), ws)
    before = _snapshot(ws / "r")
    pin_probe_out = _probe_local_raw(entries)
    assert "BAKAR_PIN\t0\t1" in pin_probe_out
    assert _snapshot(ws / "r") == before
