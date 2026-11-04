"""Tests for the hang capture, using a fake /proc tree and a fake runner."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bakar.steps import cache_capture as cc


def _proc(root: Path, pid: int, cmdline: list[str], stack: str | None = "", fd_targets: dict[str, str] | None = None):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in cmdline) + b"\0")
    if stack is not None:
        (d / "stack").write_text(stack)
    (d / "syscall").write_text("202 0x0 0x0\n")
    (d / "fd").mkdir()
    for name, target in (fd_targets or {}).items():
        (d / "fd" / name).symlink_to(target)


def _runner(ps_out: str, *, missing: set[str] | None = None):
    def run(argv, **kwargs):
        prog = argv[0]
        if prog in (missing or set()):
            raise FileNotFoundError(f"[Errno 2] No such file: {prog!r}")
        out = {
            "ps": ps_out,
            "lslocks": (
                "COMMAND PID TYPE MODE PATH\nbitbake 1 FLOCK WRITE /downloads/x.lock\nother 2 FLOCK WRITE /etc/y\n"
            ),
            "nfsstat": "Client rpc stats: calls 5\n",
            "journalctl": "kernel: nfs: server not responding\nkernel: unrelated\nkernel: LEASE expired\n",
        }[prog]
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    return run


_TCK = 100


def _stat(ticks: int) -> str:
    # pid (comm with space and paren) state ppid ... utime(14) stime(15) ...
    fields = ["0"] * 40
    fields[11] = str(ticks)
    fields[12] = "0"
    return "100 (bit bake)) " + " ".join(["S", *fields]) + "\n"


def _run_capture(tmp_path: Path, ps_out: str, stack: str | None, **kw):
    """Capture with a fake clock; ``busy`` is the CPU share over the sample interval."""
    _proc(tmp_path, 100, ["python3", "/x/bitbake", "foo"], stack, kw.get("fds"))
    (tmp_path / "locks").write_text("1: DELEG ADVISORY\n2: POSIX\n3: LEASE\n")
    stat = tmp_path / "100" / "stat"
    stat.write_text(_stat(5000))
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds
        stat.write_text(_stat(5000 + int(kw.get("busy", 0.0) / 100 * _TCK * seconds)))

    return cc.capture(
        [Path("/cache")],
        proc_root=tmp_path,
        runner=_runner(ps_out, missing=kw.get("missing")),
        sleep=sleep,
        clock=lambda: now[0],
        clk_tck=_TCK,
    )


def _summaries(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("summary:")]


def test_find_bitbake_pids_matches_argv0_and_argv1(tmp_path: Path) -> None:
    _proc(tmp_path, 10, ["/usr/bin/bitbake-server", "x"])
    _proc(tmp_path, 11, ["python3", "/opt/bin/bitbake"])
    _proc(tmp_path, 12, ["bash", "-c", "bitbake"])
    assert cc.find_bitbake_pids(tmp_path) == [10, 11]


@pytest.mark.parametrize("state", ["D", "S"])
def test_delegation_recall(tmp_path: Path, state: str) -> None:
    cap = _run_capture(tmp_path, f"{state} 0.0 -", "[<0>] __break_lease+0x1/0x2\n")
    assert cc.classify(cap) == cc.Verdict("delegation-recall", 100)
    assert _summaries(cc.render(cap)) == ["summary: delegation-recall pid=100"]


def test_delegation_recall_from_wait_channel(tmp_path: Path) -> None:
    cap = _run_capture(tmp_path, "S 0.0 __break_lease", "")
    assert cc.classify(cap) == cc.Verdict("delegation-recall", 100)


def test_delegation_recall_precedes_held_lock(tmp_path: Path) -> None:
    cap = _run_capture(tmp_path, "S 0.0 -", "[<0>] nfs4_proc_lock+0x1\n[<0>] __break_lease+0x1\n")
    assert cc.classify(cap).kind == "delegation-recall"


@pytest.mark.parametrize("state", ["S", "D"])
def test_held_lock(tmp_path: Path, state: str) -> None:
    cap = _run_capture(tmp_path, f"{state} 0.0 -", "[<0>] nfs4_proc_lock+0x1/0x2\n")
    assert cc.classify(cap) == cc.Verdict("held-lock", 100)


def test_held_lock_needs_sleeping_state(tmp_path: Path) -> None:
    cap = _run_capture(tmp_path, "R 0.0 -", "[<0>] nfs4_proc_lock+0x1/0x2\n")
    assert cc.classify(cap).kind == "unclassified"


def test_spin(tmp_path: Path) -> None:
    cap = _run_capture(tmp_path, "R 0.0 -", "", busy=95.0)
    assert cap.procs[0].cpu_pct == pytest.approx(95.0)
    assert cc.classify(cap) == cc.Verdict("spin", 100)


def test_spin_uses_interval_not_ps_lifetime_average(tmp_path: Path) -> None:
    # ps pcpu says 99 (lifetime), but the process was idle over the sample.
    idle = _run_capture(tmp_path, "R 99.0 -", "", busy=5.0)
    assert cc.classify(idle).kind == "unclassified"


def test_spin_with_ps_average_low_but_interval_busy(tmp_path: Path) -> None:
    cap = _run_capture(tmp_path, "R 50.0 -", "", busy=100.0)
    assert cc.classify(cap) == cc.Verdict("spin", 100)


def test_unreadable_stat_means_no_spin(tmp_path: Path) -> None:
    cap = _run_capture(tmp_path, "R 99.0 -", "", busy=100.0)
    (tmp_path / "100" / "stat").unlink()
    cap2 = cc.Capture(
        procs=[cc.ProcCapture(100, cap.procs[0].ps, cap.procs[0].stack, cap.procs[0].syscall, cpu_pct=None)],
        lslocks=cap.lslocks,
        deleg_lease_count=cap.deleg_lease_count,
        nfsstat=cap.nfsstat,
        journal=cap.journal,
    )
    assert cc.classify(cap2).kind == "unclassified"


def test_unclassified(tmp_path: Path) -> None:
    cap = _run_capture(tmp_path, "S 1.0 poll_schedule_timeout", "")
    assert cc.classify(cap) == cc.Verdict("unclassified")
    assert _summaries(cc.render(cap)) == ["summary: unclassified"]


def test_unreadable_stack_does_not_raise(tmp_path: Path) -> None:
    cap = _run_capture(tmp_path, "S 1.0 -", None)
    text = cc.render(cap)
    assert "unreadable:" in text and "stack" in text
    assert "nfsstat -c" in text and len(_summaries(text)) == 1


def test_missing_lslocks_is_unreadable(tmp_path: Path) -> None:
    cap = _run_capture(tmp_path, "S 1.0 -", "", missing={"lslocks"})
    text = cc.render(cap)
    assert "lslocks" in text and "unreadable: lslocks" in text
    assert "Client rpc stats" in text


def test_no_bitbake_process(tmp_path: Path) -> None:
    (tmp_path / "locks").write_text("")
    cap = cc.capture([Path("/cache")], proc_root=tmp_path, runner=_runner(""))
    text = cc.render(cap)
    assert "no running BitBake process" in text
    assert _summaries(text) == ["summary: unclassified"]


def test_fd_under_downloads_listed(tmp_path: Path) -> None:
    cap = _run_capture(tmp_path, "S 1.0 -", "", fds={"3": "/downloads/foo.tar.gz", "4": "/etc/passwd", "5": "/cache/a"})
    text = cc.render(cap)
    assert "3 -> /downloads/foo.tar.gz" in text
    assert "5 -> /cache/a" in text
    assert "/etc/passwd" not in text


def test_global_sections_filtered(tmp_path: Path) -> None:
    cap = _run_capture(tmp_path, "S 1.0 -", "")
    text = cc.render(cap)
    assert "/downloads/x.lock" in text and "/etc/y" not in text
    assert "server not responding" in text and "unrelated" not in text
    assert cap.deleg_lease_count.text == "2"


def test_timeout_is_unreadable(tmp_path: Path) -> None:
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 10)

    _proc(tmp_path, 7, ["bitbake"])
    (tmp_path / "locks").write_text("")
    cap = cc.capture([], proc_root=tmp_path, runner=run, sleep=lambda _s: None)
    assert "timed out" in cc.render(cap)
    assert len(_summaries(cc.render(cap))) == 1


@pytest.mark.parametrize("ps", ["", "garbage"])
def test_bad_ps_output_is_unclassified(tmp_path: Path, ps: str) -> None:
    cap = _run_capture(tmp_path, ps, "")
    assert cc.classify(cap).kind == "unclassified"
