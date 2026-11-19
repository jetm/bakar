"""apply_links and render_report: links only, never replace or touch."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bakar.steps.dl_link import (
    KEPT_LINK_FAILED,
    KEPT_NO_STAMP,
    KEPT_PRESENT,
    ApplyResult,
    LinkItem,
    LinkPlan,
    apply_links,
    plan_links,
    render_report,
)


def _mirror(tmp_path: Path, names: list[str]) -> Path:
    mdir = tmp_path / "mirror"
    mdir.mkdir()
    for n in names:
        (mdir / n).write_bytes(b"x")
        (mdir / (n + ".done")).write_bytes(b"")
    return mdir


def _dl(tmp_path: Path) -> Path:
    dl = tmp_path / "dl"
    dl.mkdir()
    return dl


def test_link_target_is_exact_path(tmp_path: Path) -> None:
    mdir = _mirror(tmp_path, ["a.tar.gz"])
    dl = _dl(tmp_path)
    plan = plan_links(dl, [mdir])
    result = apply_links(plan, dl)
    assert result.linked == 1
    assert os.readlink(dl / "a.tar.gz") == str(mdir / "a.tar.gz")


def test_only_links_appear(tmp_path: Path) -> None:
    mdir = _mirror(tmp_path, ["a.tgz", "b.tgz"])
    dl = _dl(tmp_path)
    apply_links(plan_links(dl, [mdir]), dl)
    assert sorted(os.listdir(dl)) == ["a.tgz", "b.tgz"]
    assert all((dl / n).is_symlink() for n in os.listdir(dl))


def test_name_created_after_planning_is_left_alone(tmp_path: Path) -> None:
    mdir = _mirror(tmp_path, ["a.tgz", "b.tgz"])
    dl = _dl(tmp_path)
    plan = plan_links(dl, [mdir])
    (dl / "a.tgz").write_bytes(b"bitbake")
    result = apply_links(plan, dl)
    assert (dl / "a.tgz").read_bytes() == b"bitbake"
    assert not (dl / "a.tgz").is_symlink()
    assert result.linked == 1
    assert result.kept == {KEPT_PRESENT: 1}


def test_dangling_link_left_alone(tmp_path: Path) -> None:
    mdir = _mirror(tmp_path, ["a.tgz"])
    dl = _dl(tmp_path)
    plan = plan_links(dl, [mdir])
    os.symlink(tmp_path / "nowhere", dl / "a.tgz")
    result = apply_links(plan, dl)
    assert os.readlink(dl / "a.tgz") == str(tmp_path / "nowhere")
    assert result.linked == 0
    assert result.kept == {KEPT_PRESENT: 1}


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_unwritable_dir_counts_link_failed(tmp_path: Path) -> None:
    mdir = _mirror(tmp_path, ["a.tgz", "b.tgz"])
    dl = _dl(tmp_path)
    plan = plan_links(dl, [mdir])
    dl.chmod(0o555)
    try:
        result = apply_links(plan, dl)
    finally:
        dl.chmod(0o755)
    assert result.linked == 0
    assert result.kept == {KEPT_LINK_FAILED: 2}
    assert os.listdir(dl) == []


def test_second_apply_links_nothing(tmp_path: Path) -> None:
    mdir = _mirror(tmp_path, ["a.tgz"])
    dl = _dl(tmp_path)
    assert apply_links(plan_links(dl, [mdir]), dl).linked == 1
    second = plan_links(dl, [mdir])
    assert second.items == []
    assert apply_links(second, dl).linked == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_mode_000_mirror_file_still_linked(tmp_path: Path) -> None:
    mdir = _mirror(tmp_path, ["a.tgz"])
    dl = _dl(tmp_path)
    (mdir / "a.tgz").chmod(0)
    result = apply_links(plan_links(dl, [mdir]), dl)
    assert result.linked == 1
    assert os.readlink(dl / "a.tgz") == str(mdir / "a.tgz")


def _plan() -> LinkPlan:
    return LinkPlan(
        items=[LinkItem("a", Path("/m/a")), LinkItem("b", Path("/m/b"))],
        kept={KEPT_NO_STAMP: 3, KEPT_PRESENT: 1},
        unlisted=[Path("/m2")],
        examined=7,
        listed=2,
    )


def test_render_dry_run() -> None:
    out = render_report(_plan(), ApplyResult(), apply=False).splitlines()
    assert out == [
        "2 mirror directories listed, 7 payloads examined",
        "unlisted: /m2",
        "would link 2 file(s)",
        "kept 1: already present",
        "kept 3: no stamp",
        "dry run: nothing was linked (pass --apply to link)",
    ]


def test_render_applied_merges_result_reasons() -> None:
    result = ApplyResult(linked=1, kept={KEPT_PRESENT: 1, KEPT_LINK_FAILED: 2})
    out = render_report(_plan(), result, apply=True).splitlines()
    assert out == [
        "2 mirror directories listed, 7 payloads examined",
        "unlisted: /m2",
        "linked 1 file(s)",
        "kept 2: already present",
        "kept 2: link failed",
        "kept 3: no stamp",
        "applied",
    ]
