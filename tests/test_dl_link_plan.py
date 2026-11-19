"""Tests for the mirror link planner (bakar.steps.dl_link)."""

from __future__ import annotations

import re
from pathlib import Path

from bakar.steps import cache_prune, cache_push, dl_link
from bakar.steps.dl_link import plan_links

M1 = Path("/mirror/a")
M2 = Path("/mirror/b")


def fake(listing: dict[Path, list[str] | None]):
    def lister(dirs, *, deadline_s):
        return {d: listing.get(d) for d in dirs}

    return lister


def stamped(*names: str) -> list[str]:
    return [x for n in names for x in (n, n + ".done")]


def run(tmp_path: Path, listing, dirs=(M1,)):
    return plan_links(tmp_path, list(dirs), lister=fake(listing))


def test_stamped_payload_linked(tmp_path):
    plan = run(tmp_path, {M1: stamped("foo.tar.gz")})
    assert plan.items == [dl_link.LinkItem("foo.tar.gz", M1 / "foo.tar.gz")]
    assert plan.examined == 1
    assert plan.kept == {}


def test_no_stamp_kept(tmp_path):
    plan = run(tmp_path, {M1: ["foo.tar.gz"]})
    assert plan.items == []
    assert plan.kept == {"no stamp": 1}


def test_git_tarballs_kept_even_when_stamped(tmp_path):
    plan = run(tmp_path, {M1: stamped("git2_x.tar.gz", "gitshallow_y.tar.gz")})
    assert plan.items == []
    assert plan.kept == {"git mirror tarball": 2}


def test_skipped_suffixes_not_counted(tmp_path):
    plan = run(tmp_path, {M1: ["a.tmp", "b.lock", ".done"]})
    assert plan.items == []
    assert plan.kept == {}
    assert plan.examined == 0


def test_unsafe_names_kept(tmp_path):
    plan = run(tmp_path, {M1: stamped(".hidden", "#note", ";rc")})
    assert plan.items == []
    assert plan.kept == {"unsafe name": 3}


def test_local_entries_count_as_present(tmp_path):
    (tmp_path / "reg").write_text("x")
    (tmp_path / "dir").mkdir()
    (tmp_path / "target").write_text("x")
    (tmp_path / "live").symlink_to(tmp_path / "target")
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
    names = ["reg", "dir", "live", "dangling"]
    plan = run(tmp_path, {M1: stamped(*names)})
    assert plan.items == []
    assert plan.kept == {"already present": 4}


def test_subdirectory_contents_never_seen(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner").write_text("x")
    plan = run(tmp_path, {M1: stamped("inner")})
    assert [i.name for i in plan.items] == ["inner"]


def test_first_directory_wins_and_counted_once(tmp_path):
    plan = run(tmp_path, {M1: stamped("a", "git2_t"), M2: stamped("a", "b", "git2_t")}, (M1, M2))
    assert [(i.name, i.target) for i in plan.items] == [("a", M1 / "a"), ("b", M2 / "b")]
    assert plan.kept == {"git mirror tarball": 1}
    assert plan.examined == 3


def test_first_directory_wins_when_kept_there(tmp_path):
    plan = run(tmp_path, {M1: ["a"], M2: stamped("a")}, (M1, M2))
    assert plan.items == []
    assert plan.kept == {"no stamp": 1}
    assert plan.examined == 1


def test_unlisted_directory_reported(tmp_path):
    plan = run(tmp_path, {M1: None, M2: stamped("b")}, (M1, M2))
    assert plan.unlisted == [M1]
    assert [i.name for i in plan.items] == ["b"]


def test_item_order_deterministic(tmp_path):
    plan = run(tmp_path, {M1: stamped("c", "a", "b")})
    assert [i.name for i in plan.items] == ["a", "b", "c"]


def test_git_prefixes_match_push_and_prune():
    assert dl_link._GIT_MIRROR_PREFIXES == cache_push._GIT_MIRROR_PREFIXES
    assert dl_link._GIT_MIRROR_PREFIXES == cache_prune._GIT_MIRROR_PREFIXES


def test_overlay_premirror_excludes_git():
    overlay = Path(dl_link.__file__).parent.parent / "overlays" / "bakar-tuning-hub-mount.yml"
    lines = [ln for ln in overlay.read_text().splitlines() if "PREMIRRORS:prepend" in ln]
    assert len(lines) == 1
    m = re.search(r"\(([\w?|]+)\)://", lines[0])
    assert m
    schemes = m.group(1).split("|")
    assert "git" not in schemes and "gitsm" not in schemes
    assert "https?" in schemes


def test_unchecked_tarballs_kept_even_when_stamped(tmp_path):
    names = ("gitsmshallow_a.tar.gz", "gitannexshallow_b.tar.gz", "hg_c.tar.gz", "repo_d.tar.gz")
    plan = run(tmp_path, {M1: stamped(*names)})
    assert plan.items == []
    assert plan.kept == {"unchecked tarball": 4}


def test_reserved_dl_dir_subdirectory_names_kept(tmp_path):
    plan = run(tmp_path, {M1: stamped("git2", "uninative", "svn")})
    assert plan.items == []
    assert plan.kept == {"reserved name": 3}


def test_local_stamp_without_payload_is_not_relinked(tmp_path):
    (tmp_path / "foo.tar.gz.done").write_text("")
    plan = run(tmp_path, {M1: stamped("foo.tar.gz")})
    assert plan.items == []
    assert plan.kept == {"local stamp without payload": 1}
