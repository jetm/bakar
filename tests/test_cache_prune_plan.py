"""Planning half of ``bakar cache prune-downloads``: nothing is removed, only decided."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from bakar.probes import HeadInfo
from bakar.steps.cache_prune import plan_prune

if TYPE_CHECKING:
    from pathlib import Path

MIRROR = "http://hub.example/dl"
NOW = 1_800_000_000.0
DAY = 86400


class FakeHub:
    """A fake ``head``: ``answers`` maps a URL path name to a HeadInfo, None, or absent (404)."""

    def __init__(self, answers: dict[str, HeadInfo | None]) -> None:
        self.answers = answers
        self.urls: list[str] = []

    def __call__(self, url: str, timeout: float) -> HeadInfo | None:
        self.urls.append(url)
        name = url.removeprefix(MIRROR + "/")
        return self.answers.get(name, HeadInfo(404, None))


def _pair(dl: Path, name: str, data: bytes = b"hello") -> None:
    (dl / name).write_bytes(data)
    (dl / (name + ".done")).write_bytes(b"")


def _served(*names: str, size: int = 5) -> dict[str, HeadInfo | None]:
    out: dict[str, HeadInfo | None] = {}
    for n in names:
        out[n] = HeadInfo(200, size)
        out[n + ".done"] = HeadInfo(200, 0)
    return out


def _plan(dl: Path, hub: FakeHub | None = None, **kw):
    kw.setdefault("mirror_url", MIRROR)
    kw.setdefault("clone_older_than_days", None)
    return plan_prune(dl, head=hub or FakeHub({}), now=NOW, **kw)


def _names(plan, kind: str) -> list[str]:
    return [i.name for i in plan.items[kind]]


def _make_clone(dl: Path, n: str, *, stamp: bool = True, head: bool = True) -> Path:
    clone = dl / "git2" / n
    clone.mkdir(parents=True)
    if head:
        (clone / "HEAD").write_text("ref: refs/heads/main\n")
    if stamp:
        (dl / "git2" / (n + ".done")).write_bytes(b"")
    return clone


def test_verified_file_is_planned(tmp_path: Path) -> None:
    _pair(tmp_path, "a.tar.gz")
    plan = _plan(tmp_path, FakeHub(_served("a.tar.gz")))
    (item,) = plan.items["hub"]
    assert item.kind == "hub"
    assert item.paths == (tmp_path / "a.tar.gz.done", tmp_path / "a.tar.gz")
    assert item.size == 5
    assert item.lock == tmp_path / "a.tar.gz.lock"
    st = os.lstat(tmp_path / "a.tar.gz")
    assert item.stat_key == (st.st_size, st.st_mtime_ns)
    assert plan.status["hub"] == "examined"


def test_404_keeps(tmp_path: Path) -> None:
    _pair(tmp_path, "a.tar.gz")
    plan = _plan(tmp_path)
    assert not plan.items["hub"]
    assert plan.kept["hub", "hub lacks it"] == 1


def test_size_mismatch_keeps(tmp_path: Path) -> None:
    _pair(tmp_path, "a.tar.gz")
    plan = _plan(tmp_path, FakeHub(_served("a.tar.gz", size=99)))
    assert not plan.items["hub"]
    assert plan.kept["hub", "size mismatch"] == 1


def test_missing_length_keeps(tmp_path: Path) -> None:
    _pair(tmp_path, "a.tar.gz")
    plan = _plan(tmp_path, FakeHub({"a.tar.gz": HeadInfo(200, None)}))
    assert not plan.items["hub"]
    assert plan.kept["hub", "no length"] == 1


def test_stamp_not_2xx_keeps(tmp_path: Path) -> None:
    _pair(tmp_path, "a.tar.gz")
    hub = FakeHub({"a.tar.gz": HeadInfo(200, 5), "a.tar.gz.done": HeadInfo(404, None)})
    plan = _plan(tmp_path, hub)
    assert not plan.items["hub"]
    assert plan.kept["hub", "hub lacks stamp"] == 1


def test_single_no_answer_keeps_as_unverified(tmp_path: Path) -> None:
    _pair(tmp_path, "a")
    _pair(tmp_path, "b")
    hub = FakeHub({"a": None, **_served("b")})
    plan = _plan(tmp_path, hub)
    assert _names(plan, "hub") == ["b"]
    assert plan.kept["hub", "unverified"] == 1


def test_three_no_answers_stop_and_remove_nothing(tmp_path: Path) -> None:
    for n in ("a", "b", "c", "d", "e"):
        _pair(tmp_path, n)
    hub = FakeHub({"a": None, "b": None, "c": None, **_served("d", "e")})
    plan = _plan(tmp_path, hub)
    assert not plan.items["hub"]
    assert plan.kept["hub", "unverified"] == 5
    assert len(hub.urls) == 3


def test_three_no_answers_withdraw_earlier_verified(tmp_path: Path) -> None:
    for n in ("a", "b", "c", "d", "e"):
        _pair(tmp_path, n)
    hub = FakeHub({**_served("a"), "b": None, "c": None, "d": None, "e": None})
    plan = _plan(tmp_path, hub)
    assert not plan.items["hub"]
    assert plan.kept["hub", "unverified"] == 5


def test_no_answers_not_consecutive_do_not_stop(tmp_path: Path) -> None:
    for n in ("a", "b", "c", "d", "e"):
        _pair(tmp_path, n)
    hub = FakeHub({"a": None, "b": None, **_served("c"), "d": None, **_served("e")})
    plan = _plan(tmp_path, hub)
    assert _names(plan, "hub") == ["c", "e"]


def test_symlink_and_its_stamp_ignored(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "real").write_bytes(b"hello")
    (outside / "real.done").write_bytes(b"")
    dl = tmp_path / "dl"
    dl.mkdir()
    (dl / "a").symlink_to(outside / "real")
    (dl / "a.done").symlink_to(outside / "real.done")
    (dl / "b").symlink_to(outside / "real")
    (dl / "b.done").write_bytes(b"")
    plan = _plan(dl, FakeHub(_served("a", "b")))
    assert not plan.items["hub"]
    assert not plan.kept


def test_git_tmp_lock_names_ignored(tmp_path: Path) -> None:
    names = ["git2_x.tar.gz", "gitshallow_y.tar.gz", "z.tmp", "w.lock"]
    for n in names:
        _pair(tmp_path, n)
    hub = FakeHub(_served(*names))
    plan = _plan(tmp_path, hub)
    assert not plan.items["hub"]
    assert not hub.urls


def test_unsafe_names_rejected(tmp_path: Path) -> None:
    for n in ("#c", ";d", ".hidden"):
        _pair(tmp_path, n)
    hub = FakeHub(_served("#c", ";d", ".hidden"))
    plan = _plan(tmp_path, hub)
    assert not plan.items["hub"]
    assert not hub.urls


def test_payload_without_regular_stamp_ignored(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"hello")
    (tmp_path / "b").write_bytes(b"hello")
    (tmp_path / "b.done").mkdir()
    hub = FakeHub(_served("a", "b"))
    plan = _plan(tmp_path, hub)
    assert not plan.items["hub"]
    assert not hub.urls


def test_tarball_with_clone_planned(tmp_path: Path) -> None:
    _pair(tmp_path, "git2_example.com.proj.tar.gz")
    _make_clone(tmp_path, "example.com.proj")
    plan = _plan(tmp_path)
    (item,) = plan.items["tarball"]
    assert item.kind == "tarball"
    assert item.paths == (
        tmp_path / "git2_example.com.proj.tar.gz.done",
        tmp_path / "git2_example.com.proj.tar.gz",
    )
    assert item.lock == tmp_path / "git2" / "example.com.proj.lock"


def test_tarball_without_clone_kept(tmp_path: Path) -> None:
    _pair(tmp_path, "git2_p.tar.gz")
    plan = _plan(tmp_path)
    assert not plan.items["tarball"]
    assert plan.kept["tarball", "no valid clone"] == 1


def test_tarball_with_stampless_clone_kept(tmp_path: Path) -> None:
    _pair(tmp_path, "git2_p.tar.gz")
    _make_clone(tmp_path, "p", stamp=False)
    plan = _plan(tmp_path)
    assert not plan.items["tarball"]
    assert plan.kept["tarball", "no valid clone"] == 1


def test_tarball_with_clone_lacking_head_kept(tmp_path: Path) -> None:
    _pair(tmp_path, "git2_p.tar.gz")
    _make_clone(tmp_path, "p", head=False)
    plan = _plan(tmp_path)
    assert not plan.items["tarball"]
    assert plan.kept["tarball", "no valid clone"] == 1


def test_tarball_with_symlinked_clone_kept(tmp_path: Path) -> None:
    _pair(tmp_path, "git2_p.tar.gz")
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "HEAD").write_text("x")
    (tmp_path / "git2").mkdir()
    (tmp_path / "git2" / "p").symlink_to(real)
    (tmp_path / "git2" / "p.done").write_bytes(b"")
    plan = _plan(tmp_path)
    assert not plan.items["tarball"]
    assert plan.kept["tarball", "no valid clone"] == 1


def test_clone_age_thresholds(tmp_path: Path) -> None:
    for n, days in (("old", 40), ("new", 10)):
        clone = _make_clone(tmp_path, n)
        (clone / "objects").write_bytes(b"x" * 7)
        stamp = tmp_path / "git2" / (n + ".done")
        os.utime(stamp, (NOW - days * DAY, NOW - days * DAY))
    plan = _plan(tmp_path, clone_older_than_days=30)
    (item,) = plan.items["clone"]
    assert item.name == "old"
    assert item.paths == (tmp_path / "git2" / "old.done", tmp_path / "git2" / "old")
    assert item.lock == tmp_path / "git2" / "old.lock"
    assert item.size == len("ref: refs/heads/main\n") + 7
    assert plan.kept["clone", "too recent"] == 1
    assert plan.status["clone"] == "examined"


def test_clone_without_threshold_not_requested(tmp_path: Path) -> None:
    _make_clone(tmp_path, "old")
    os.utime(tmp_path / "git2" / "old.done", (NOW - 400 * DAY, NOW - 400 * DAY))
    plan = _plan(tmp_path, clone_older_than_days=None)
    assert not plan.items["clone"]
    assert plan.status["clone"] == "not requested"


def test_stampless_clone_kept_whatever_threshold(tmp_path: Path) -> None:
    _make_clone(tmp_path, "p", stamp=False)
    plan = _plan(tmp_path, clone_older_than_days=0)
    assert not plan.items["clone"]
    assert plan.kept["clone", "no stamp"] == 1


def test_clone_size_does_not_follow_links(tmp_path: Path) -> None:
    clone = _make_clone(tmp_path, "p")
    big = tmp_path / "big"
    big.write_bytes(b"x" * 1000)
    (clone / "link").symlink_to(big)
    os.utime(tmp_path / "git2" / "p.done", (NOW - 40 * DAY, NOW - 40 * DAY))
    plan = _plan(tmp_path, clone_older_than_days=30)
    assert plan.items["clone"][0].size == len("ref: refs/heads/main\n")


def test_no_mirror_marks_hub_unavailable_but_tarball_plans(tmp_path: Path) -> None:
    _pair(tmp_path, "a")
    _pair(tmp_path, "git2_p.tar.gz")
    _make_clone(tmp_path, "p")
    hub = FakeHub(_served("a"))
    plan = _plan(tmp_path, hub, mirror_url=None)
    assert plan.status["hub"] == "unavailable"
    assert not plan.items["hub"]
    assert not hub.urls
    assert _names(plan, "tarball") == ["git2_p.tar.gz"]


def test_plan_deletes_nothing(tmp_path: Path) -> None:
    _pair(tmp_path, "a")
    _pair(tmp_path, "git2_p.tar.gz")
    _make_clone(tmp_path, "p")
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    _plan(tmp_path, FakeHub(_served("a")), clone_older_than_days=0)
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before
