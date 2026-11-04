from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from bakar.steps import cache_push as cp
from bakar.steps.cache_queue import PushQueue


def _mk(dl: Path, name: str, content: str = "x") -> None:
    (dl / name).write_text(content)


def test_regular_file_pair(tmp_path: Path) -> None:
    _mk(tmp_path, "a.tar.gz")
    _mk(tmp_path, "a.tar.gz.done")
    assert cp.eligible_pairs(tmp_path, ["a.tar.gz.done"]) == ["a.tar.gz", "a.tar.gz.done"]
    assert cp.scan_candidates(tmp_path) == ["a.tar.gz", "a.tar.gz.done"]


def test_directory_stamp_none(tmp_path: Path) -> None:
    (tmp_path / "repo.git").mkdir()
    _mk(tmp_path, "repo.git.done")
    assert cp.eligible_pairs(tmp_path, ["repo.git.done"]) == []


def test_symlink_none(tmp_path: Path) -> None:
    _mk(tmp_path, "real")
    (tmp_path / "a").symlink_to("real")
    _mk(tmp_path, "a.done")
    assert cp.eligible_pairs(tmp_path, ["a.done"]) == []
    (tmp_path / "b").write_text("x")
    (tmp_path / "b.done").symlink_to("real")
    assert cp.eligible_pairs(tmp_path, ["b.done"]) == []


@pytest.mark.parametrize("name", ["a.tmp", "a.lock", "a.tmp.done", "a.lock.done"])
def test_tmp_lock_none(tmp_path: Path, name: str) -> None:
    _mk(tmp_path, name)
    if not name.endswith(".done"):
        assert cp.scan_candidates(tmp_path) == []
    else:
        _mk(tmp_path, name[:-5])
        assert cp.eligible_pairs(tmp_path, ["a.tmp"]) == []


def test_git2_tarball_yields_nothing_and_no_recursion(tmp_path: Path) -> None:
    _mk(tmp_path, "git2_x.tar.gz")
    _mk(tmp_path, "git2_x.tar.gz.done")
    sub = tmp_path / "git2"
    sub.mkdir()
    _mk(sub, "y")
    _mk(sub, "y.done")
    assert cp.scan_candidates(tmp_path) == []
    assert cp.eligible_pairs(tmp_path, ["git2_x.tar.gz.done"]) == []
    _mk(tmp_path, "gitshallow_x.tar.gz")
    _mk(tmp_path, "gitshallow_x.tar.gz.done")
    assert cp.eligible_pairs(tmp_path, ["gitshallow_x.tar.gz.done"]) == []
    assert cp.scan_candidates(tmp_path) == []
    assert cp.eligible_pairs(tmp_path, ["git2/y.done", ".hidden.done"]) == []


def test_push_argv(tmp_path: Path) -> None:
    argv = cp.build_push_argv(tmp_path, "hub:/dl", Path("/k/id"), tmp_path / "l")
    joined = " ".join(argv)
    assert "--ignore-existing" in argv
    assert "--from0" in argv
    assert any(a.startswith("--files-from=") for a in argv)
    assert "/k/id" in joined
    assert "IdentitiesOnly=yes" in joined
    assert "--timeout=" in joined
    for bad in ("--delete", "--inplace", "--remove-source-files", "-r", "-a"):
        assert bad not in argv
    assert argv[-1] == "hub:/dl/"
    recon = cp.build_reconcile_argv(tmp_path, "hub:/dl", Path("/k/id"), tmp_path / "l")
    assert "--dry-run" in recon and "--out-format=%n" in recon


def test_push_and_reconcile_authenticate_with_the_push_key_alone(tmp_path: Path) -> None:
    # A user ssh config can offer an agent key and an identity file for the store host. On pc2
    # and pc3 it did, the store host accepted the user's full key first, and no push ever used
    # the write-only rrsync key. IdentityAgent=none is what makes the push key the only identity.
    for argv in (
        cp.build_push_argv(tmp_path, "hub:/dl", Path("/k/id"), tmp_path / "l"),
        cp.build_reconcile_argv(tmp_path, "hub:/dl", Path("/k/id"), tmp_path / "l"),
    ):
        transport = argv[argv.index("-e") + 1].split()
        assert transport[0] == "ssh"
        assert "IdentityAgent=none" in transport
        assert "IdentitiesOnly=yes" in transport
        assert transport[transport.index("-i") + 1] == "/k/id"
        assert "ForwardAgent=yes" not in transport


@pytest.mark.parametrize(
    ("rc", "err", "want"),
    [
        (0, "", cp.Outcome.OK),
        (11, "", cp.Outcome.FULL),
        (23, "write failed: No space left on device", cp.Outcome.FULL),
        (255, "", cp.Outcome.UNREACHABLE),
        (10, "", cp.Outcome.UNREACHABLE),
        (12, "", cp.Outcome.UNREACHABLE),
        (30, "", cp.Outcome.UNREACHABLE),
        (35, "", cp.Outcome.UNREACHABLE),
        (23, "other", cp.Outcome.FAILED),
    ],
)
def test_classify(rc: int, err: str, want: cp.Outcome) -> None:
    assert cp.classify_rsync(rc, err) is want


def _setup(tmp_path: Path) -> tuple[PushQueue, Path]:
    dl = tmp_path / "dl"
    dl.mkdir()
    _mk(dl, "a")
    _mk(dl, "a.done")
    q = PushQueue(tmp_path / "state")
    q.enqueue(["a", "a.done"], now=1.0)
    return q, dl


def _runner(rc: int, err: str = ""):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, rc, "", err)

    return run


def test_drain_ok_removes(tmp_path: Path) -> None:
    q, dl = _setup(tmp_path)
    q.set_paused("x")
    assert cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=_runner(0)) is cp.Outcome.OK
    assert q.pending() == []
    assert not q.snapshot().paused


def test_drain_full_pauses_keeps(tmp_path: Path) -> None:
    q, dl = _setup(tmp_path)
    assert cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=_runner(11)) is cp.Outcome.FULL
    assert q.pending() == ["a", "a.done"]
    snap = q.snapshot()
    assert snap.paused and snap.paused_reason == "target disk full"


def test_drain_unreachable_records_failure(tmp_path: Path) -> None:
    q, dl = _setup(tmp_path)
    assert cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=_runner(255, "ssh: refused")) is cp.Outcome.UNREACHABLE
    assert q.pending() == ["a", "a.done"]
    attempts = q._conn.execute("SELECT attempts, last_error FROM entries WHERE name='a'").fetchone()
    assert attempts == (1, "ssh: refused")


def test_drain_drops_vanished(tmp_path: Path) -> None:
    q, dl = _setup(tmp_path)
    (dl / "a").unlink()
    calls: list[object] = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=run) is cp.Outcome.OK
    assert q.pending() == []
    assert calls == []


def _pairs_queue(tmp_path: Path, n: int) -> tuple[PushQueue, Path]:
    dl = tmp_path / "dl"
    dl.mkdir()
    q = PushQueue(tmp_path / "state")
    for i in range(n):
        _mk(dl, f"f{i}")
        _mk(dl, f"f{i}.done")
        q.enqueue([f"f{i}", f"f{i}.done"], now=float(i))
    return q, dl


def _hub(monkeypatch: pytest.MonkeyPatch, answers: dict[str, int | None]) -> list[tuple[str, float]]:
    """Stub the HEAD probe; ``answers`` maps a URL to a status (None = no answer, absent = 404)."""
    calls: list[tuple[str, float]] = []

    def head(url: str, timeout: float) -> int | None:
        calls.append((url, timeout))
        return answers.get(url, 404)

    monkeypatch.setattr(cp.probes, "head_status", head)
    return calls


def _counting_runner(calls: list[list[str]]):
    def run(argv, **kw):
        list_file = next(a for a in argv if a.startswith("--files-from=")).split("=", 1)[1]
        calls.append(Path(list_file).read_bytes().split(b"\0")[:-1])
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


def test_drain_pair_on_hub_is_removed_without_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    q, dl = _setup(tmp_path)
    checks = _hub(monkeypatch, {"http://hub.test/dl/a": 200, "http://hub.test/dl/a.done": 200})
    pushes: list[list[str]] = []
    out = cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=_counting_runner(pushes), mirror_url="http://hub.test/dl/")
    assert out is cp.Outcome.OK
    assert q.pending() == []
    assert pushes == []
    assert checks == [("http://hub.test/dl/a", 2.0), ("http://hub.test/dl/a.done", 2.0)]


def test_drain_payload_on_hub_but_stamp_missing_is_pushed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    q, dl = _setup(tmp_path)
    checks = _hub(monkeypatch, {"http://hub.test/a": 200})
    pushes: list[list[str]] = []
    cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=_counting_runner(pushes), mirror_url="http://hub.test")
    assert len(checks) == 2
    assert pushes == [[b"a", b"a.done"]]


def test_drain_payload_404_skips_the_stamp_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    q, dl = _setup(tmp_path)
    checks = _hub(monkeypatch, {"http://hub.test/a.done": 200})
    cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=_counting_runner([]), mirror_url="http://hub.test")
    assert [u for u, _ in checks] == ["http://hub.test/a"]


def test_drain_stamp_error_stops_checking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    q, dl = _pairs_queue(tmp_path, 3)
    checks = _hub(monkeypatch, {"http://hub.test/f0": 200, "http://hub.test/f0.done": None})
    pushes: list[list[str]] = []
    cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=_counting_runner(pushes), mirror_url="http://hub.test")
    assert len(checks) == 2
    assert len(pushes[0]) == 6


@pytest.mark.parametrize("status", [403, 500])
def test_drain_non_2xx_non_404_payload_is_pushed_and_checks_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    q, dl = _pairs_queue(tmp_path, 3)
    checks = _hub(
        monkeypatch,
        {
            "http://hub.test/f0": status,
            "http://hub.test/f1": 200,
            "http://hub.test/f1.done": 200,
            "http://hub.test/f2": 200,
            "http://hub.test/f2.done": 200,
        },
    )
    pushes: list[list[str]] = []
    cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=_counting_runner(pushes), mirror_url="http://hub.test")
    assert len(checks) == 5
    assert len(pushes) == 1
    assert sorted(pushes[0]) == [b"f0", b"f0.done"]
    assert q.pending() == []


def test_drain_check_budget_exhausted_pushes_the_rest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    q, dl = _pairs_queue(tmp_path, 4)
    now = [0.0]
    checks: list[str] = []

    def head(url: str, timeout: float) -> int | None:
        checks.append(url)
        now[0] += 6.0
        return 200

    monkeypatch.setattr(cp.probes, "head_status", head)
    pushes: list[list[str]] = []
    cp.drain_once(
        q, dl, "hub:/d", Path("/k"), runner=_counting_runner(pushes), mirror_url="http://hub.test", clock=lambda: now[0]
    )
    assert checks == ["http://hub.test/f0", "http://hub.test/f0.done"]
    assert len(pushes) == 1
    assert sorted(pushes[0]) == [b"f1", b"f1.done", b"f2", b"f2.done", b"f3", b"f3.done"]


def test_drain_stop_set_makes_no_further_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    q, dl = _pairs_queue(tmp_path, 3)
    stop = threading.Event()
    checks: list[str] = []

    def head(url: str, timeout: float) -> int | None:
        checks.append(url)
        stop.set()
        return 200

    monkeypatch.setattr(cp.probes, "head_status", head)
    pushes: list[list[str]] = []
    cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=_counting_runner(pushes), mirror_url="http://hub.test", stop=stop)
    assert checks == ["http://hub.test/f0"]
    assert len(pushes[0]) == 6


def test_drain_pair_missing_on_hub_is_pushed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    q, dl = _setup(tmp_path)
    _hub(monkeypatch, {})
    pushes: list[list[str]] = []
    cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=_counting_runner(pushes), mirror_url="http://hub.test/dl")
    assert pushes == [[b"a", b"a.done"]]
    assert q.pending() == []


def test_drain_hub_error_pushes_everything_and_stops_checking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    q, dl = _pairs_queue(tmp_path, 3)
    checks = _hub(monkeypatch, {"http://hub.test/f0": None, "http://hub.test/f1": 200})
    pushes: list[list[str]] = []
    cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=_counting_runner(pushes), mirror_url="http://hub.test")
    assert len(checks) == 1
    assert len(pushes) == 1
    assert sorted(pushes[0]) == [b"f0", b"f0.done", b"f1", b"f1.done", b"f2", b"f2.done"]
    assert q.pending() == []


def test_drain_without_mirror_url_makes_no_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    q, dl = _setup(tmp_path)
    checks = _hub(monkeypatch, {"http://hub.test/a": 200})
    pushes: list[list[str]] = []
    cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=_counting_runner(pushes))
    assert checks == []
    assert pushes == [[b"a", b"a.done"]]


def test_drain_percent_encodes_payload_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dl = tmp_path / "dl"
    dl.mkdir()
    _mk(dl, "a b+c%.tar.gz")
    _mk(dl, "a b+c%.tar.gz.done")
    q = PushQueue(tmp_path / "state")
    q.enqueue(["a b+c%.tar.gz", "a b+c%.tar.gz.done"], now=1.0)
    checks = _hub(monkeypatch, {})
    cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=_counting_runner([]), mirror_url="http://hub.test")
    assert [u for u, _ in checks] == ["http://hub.test/a%20b%2Bc%25.tar.gz"]


def test_drain_mixed_batch_pushes_only_missing_pairs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    q, dl = _pairs_queue(tmp_path, 3)
    checks = _hub(
        monkeypatch,
        {
            "http://hub.test/f0": 200,
            "http://hub.test/f0.done": 200,
            "http://hub.test/f2": 204,
            "http://hub.test/f2.done": 200,
        },
    )
    pushes: list[list[str]] = []
    cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=_counting_runner(pushes), mirror_url="http://hub.test")
    assert len(checks) == 5
    assert len(pushes) == 1
    assert sorted(pushes[0]) == [b"f1", b"f1.done"]
    assert q.pending() == []


def test_drain_hub_hit_is_not_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    q, dl = _setup(tmp_path)
    _hub(monkeypatch, {"http://hub.test/a": 200, "http://hub.test/a.done": 200})
    cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=_counting_runner([]), mirror_url="http://hub.test")
    assert q._conn.execute("SELECT count(*) FROM entries").fetchone() == (0,)


def _listed(list_file: str) -> list[str]:
    return Path(list_file).read_text().split("\0")[:-1]


def test_one_bad_pair_in_64_is_found_by_bisection(tmp_path: Path) -> None:
    q, dl = _pairs_queue(tmp_path, 64)
    calls = 0

    def run(argv, **kw):
        nonlocal calls
        calls += 1
        lf = next(a for a in argv if a.startswith("--files-from=")).split("=", 1)[1]
        rc = 23 if "f37" in _listed(lf) else 0
        return subprocess.CompletedProcess(argv, rc, "", "bad file" if rc else "")

    assert cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=run, batch=200) is cp.Outcome.FAILED
    assert q.pending() == ["f37", "f37.done"]
    assert calls <= 2 * 6 + 1
    assert calls < 64


def test_lock_and_tmp_base_names_with_stamp_yield_nothing(tmp_path: Path) -> None:
    _mk(tmp_path, "x.lock")
    _mk(tmp_path, "x.lock.done")
    _mk(tmp_path, "y.tmp")
    _mk(tmp_path, "y.tmp.done")
    assert cp.eligible_pairs(tmp_path, ["x.lock.done", "y.tmp.done"]) == []
    assert cp.scan_candidates(tmp_path) == []


def test_control_character_names_are_rejected(tmp_path: Path) -> None:
    _mk(tmp_path, "a\nb")
    _mk(tmp_path, "a\nb.done")
    assert cp.eligible_pairs(tmp_path, ["a\nb.done"]) == []
    assert cp._valid_name("a\nb") is False
    assert cp._valid_name("a\tb") is False
    assert cp._valid_name("a\x7fb") is False
    assert cp._valid_name("ab") is True
    q = PushQueue(tmp_path / "state")
    with pytest.raises(ValueError, match="invalid cache entry name"):
        q.enqueue(["a\nb"], now=1.0)


def test_list_files_are_nul_separated(tmp_path: Path) -> None:
    q, dl = _setup(tmp_path)
    seen: list[str] = []

    def run(argv, **kw):
        assert "--from0" in argv
        lf = next(a for a in argv if a.startswith("--files-from=")).split("=", 1)[1]
        seen.append(Path(lf).read_bytes().decode())
        return subprocess.CompletedProcess(argv, 0, "", "")

    cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=run)
    assert seen == ["a\0a.done\0"]


def test_reconcile_output_lines_failing_validation_are_skipped(tmp_path: Path) -> None:
    q = PushQueue(tmp_path / "state")
    dl = tmp_path / "dl"
    dl.mkdir()

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "ok.tar\nok.tar.done\nbad\x01name\n.hidden\n", "")

    assert cp._reconcile(q, dl, "hub:/d", Path("/k"), run, 1.0) is cp.Outcome.OK
    assert sorted(q.pending()) == ["ok.tar", "ok.tar.done"]


def test_failed_batch_is_isolated_per_pair(tmp_path: Path) -> None:
    q, dl = _pairs_queue(tmp_path, 3)

    def run(argv, **kw):
        lf = next(a for a in argv if a.startswith("--files-from=")).split("=", 1)[1]
        names = _listed(lf)
        rc = 23 if "f1" in names else 0
        return subprocess.CompletedProcess(argv, rc, "", "bad file" if rc else "")

    assert cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=run) is cp.Outcome.FAILED
    assert q.pending() == ["f1", "f1.done"]
    assert q._conn.execute("SELECT attempts FROM entries WHERE name='f1'").fetchone() == (1,)


def test_poison_pair_does_not_block_later_entries(tmp_path: Path) -> None:
    q, dl = _pairs_queue(tmp_path, 3)
    q.record_failure(["f0", "f0.done"], "poison")
    seen: list[list[str]] = []

    def run(argv, **kw):
        lf = next(a for a in argv if a.startswith("--files-from=")).split("=", 1)[1]
        seen.append(_listed(lf))
        return subprocess.CompletedProcess(argv, 0, "", "")

    cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=run, batch=2)
    assert seen == [["f1", "f1.done"]]


def test_unreachable_during_isolation_keeps_everything(tmp_path: Path) -> None:
    q, dl = _pairs_queue(tmp_path, 2)
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        rc = 23 if len(calls) == 1 else 255
        return subprocess.CompletedProcess(argv, rc, "", "x")

    assert cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=run) is cp.Outcome.UNREACHABLE
    assert len(q.pending()) == 4
    attempts = q._conn.execute("SELECT COUNT(*) FROM entries WHERE attempts != 0").fetchone()
    assert attempts == (0,)


def test_systemic_failure_is_bounded(tmp_path: Path) -> None:
    q, dl = _pairs_queue(tmp_path, 128)  # 256 names
    calls = 0

    def run(argv, **kw):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(argv, 23, "", "read-only target")

    assert cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=run, batch=256) is cp.Outcome.FAILED
    assert calls <= 1 + cp._ISOLATE_CALL_BUDGET
    assert len(q.pending()) == 256
    assert q._conn.execute("SELECT COUNT(*) FROM entries WHERE attempts = 1").fetchone() == (256,)


def test_stop_event_ends_isolation_between_pushes(tmp_path: Path) -> None:
    import threading

    q, dl = _pairs_queue(tmp_path, 8)
    stop = threading.Event()
    calls = 0

    def run(argv, **kw):
        nonlocal calls
        calls += 1
        if calls == 2:
            stop.set()
        return subprocess.CompletedProcess(argv, 23 if calls == 1 else 0, "", "x")

    cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=run, batch=16, stop=stop)
    assert calls == 2


@pytest.mark.parametrize("name", ["#a", ";a", "#a.done"])
def test_comment_prefixed_names_are_never_queued(tmp_path: Path, name: str) -> None:
    assert cp._valid_name(name) is False
    q = PushQueue(tmp_path / "state")
    with pytest.raises(ValueError, match="invalid cache entry name"):
        q.enqueue([name], now=1.0)
    stem = name.removesuffix(".done")
    _mk(tmp_path, stem)
    _mk(tmp_path, stem + ".done")
    assert cp.eligible_pairs(tmp_path, [stem + ".done"]) == []


def test_non_utf8_name_does_not_raise(tmp_path: Path) -> None:
    bad = os.fsdecode(b"caf\xe9.tar")
    q = PushQueue(tmp_path / "state")
    seen: list[bytes] = []

    def run(argv, **kw):
        lf = next(a for a in argv if a.startswith("--files-from=")).split("=", 1)[1]
        seen.append(Path(lf).read_bytes())
        return subprocess.CompletedProcess(argv, 0, f"{bad}\nok.tar\n", "")

    dl = tmp_path / "dl"
    dl.mkdir()
    (dl / bad).write_text("x")
    (dl / (bad + ".done")).write_text("x")
    assert cp._reconcile(q, dl, "hub:/d", Path("/k"), run, 1.0) is cp.Outcome.OK
    assert q.pending() == ["ok.tar"]
    assert cp._push_names([bad], dl, "hub:/d", Path("/k"), run)[0] is cp.Outcome.OK
    assert seen[-1] == b"caf\xe9.tar\0"


def test_two_poison_pairs_in_opposite_halves_leave_only_themselves(tmp_path: Path) -> None:
    q, dl = _pairs_queue(tmp_path, 128)
    poison = {"f3", "f120"}

    def run(argv, **kw):
        lf = next(a for a in argv if a.startswith("--files-from=")).split("=", 1)[1]
        bad = poison & set(_listed(lf))
        return subprocess.CompletedProcess(argv, 23 if bad else 0, "", "bad file" if bad else "")

    assert cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=run, batch=256) is cp.Outcome.FAILED
    expected = sorted(["f3", "f3.done", "f120", "f120.done"])
    assert sorted(q.pending()) == expected
    rows = q._conn.execute("SELECT name FROM entries WHERE attempts = 1").fetchall()
    assert sorted(r[0] for r in rows) == expected


def test_always_failing_runner_is_bounded_by_the_call_budget(tmp_path: Path) -> None:
    q, dl = _pairs_queue(tmp_path, 128)
    calls = 0

    def run(argv, **kw):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(argv, 23, "", "read-only")

    assert cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=run, batch=256) is cp.Outcome.FAILED
    assert calls <= 1 + cp._ISOLATE_CALL_BUDGET
    assert len(q.pending()) == 256
    # Each name is recorded exactly once.
    assert q._conn.execute("SELECT COUNT(*) FROM entries WHERE attempts = 1").fetchone() == (256,)


def test_reconcile_argv_and_roundtrip_of_names(tmp_path: Path) -> None:
    argv = cp.build_reconcile_argv(tmp_path, "hub:/dl", Path("/k/id"), tmp_path / "l")
    assert "-8" in argv
    q = PushQueue(tmp_path / "state")
    dl = tmp_path / "dl"
    dl.mkdir()

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "café.tar\ntrail .tar \n", "")

    cp._reconcile(q, dl, "hub:/d", Path("/k"), run, 1.0)
    assert sorted(q.pending()) == sorted(["café.tar", "trail .tar "])


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == cp.logger.name and r.levelname == "WARNING"]


def test_failed_drain_logs_rsync_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    q, dl = _setup(tmp_path)
    err = "Bad owner or permissions on /etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf"
    with caplog.at_level("WARNING", logger=cp.logger.name):
        cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=_runner(255, err))
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "hub:/d" in warnings[0]
    assert "unreachable" in warnings[0]
    assert err in warnings[0]


def test_successful_drain_logs_nothing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    q, dl = _setup(tmp_path)
    with caplog.at_level("WARNING", logger=cp.logger.name):
        cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=_runner(0))
    assert _warnings(caplog) == []


def test_failed_reconcile_logs_rsync_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    q = PushQueue(tmp_path / "state")
    dl = tmp_path / "dl"
    dl.mkdir()
    with caplog.at_level("WARNING", logger=cp.logger.name):
        outcome = cp._reconcile(q, dl, "hub:/d", Path("/k"), _runner(255, "ssh: connect refused"), 1.0)
    assert outcome is cp.Outcome.UNREACHABLE
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "reconcile" in warnings[0]
    assert "ssh: connect refused" in warnings[0]
    assert q.snapshot().last_reconcile is None


def test_failed_reconcile_without_stderr_logs_the_exit_code(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    q = PushQueue(tmp_path / "state")
    dl = tmp_path / "dl"
    dl.mkdir()
    with caplog.at_level("WARNING", logger=cp.logger.name):
        cp._reconcile(q, dl, "hub:/d", Path("/k"), _runner(255), 1.0)
    assert "rsync exit 255" in _warnings(caplog)[0]


def test_isolated_pair_failure_names_the_pair(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    q, dl = _pairs_queue(tmp_path, 3)

    def run(argv, **kw):
        lf = next(a for a in argv if a.startswith("--files-from=")).split("=", 1)[1]
        rc = 23 if "f1" in _listed(lf) else 0
        return subprocess.CompletedProcess(argv, rc, "", "bad file" if rc else "")

    with caplog.at_level("WARNING", logger=cp.logger.name):
        cp.drain_once(q, dl, "hub:/d", Path("/k"), runner=run)
    assert any("f1" in w and "bad file" in w for w in _warnings(caplog))
