"""``build_filtered_view``: the hardlink view of a deploy dir limited to named packages.

``repo-stage-rpms.sh`` exits 1 when ``avocado-repo.map`` is missing and only
warns on a missing arch directory, so a view that drops the map (or an arch
directory the map names) turns a subset publish into a hard failure.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from bakar import feed_filter

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _deploy(tmp_path: Path) -> Path:
    deploy = tmp_path / "deploy" / "rpm"
    for arch, names in {
        "core2_64": ["foo-1.0-r0.0.core2_64.rpm", "bar-lib-2.1-r0.1.core2_64.rpm"],
        "qemux86_64": ["baz-3.0-r1.qemux86_64.rpm"],
        "noarch": [],
    }.items():
        (deploy / arch).mkdir(parents=True)
        for n in names:
            (deploy / arch / n).write_bytes(n.encode())
    (deploy / "avocado-repo.map").write_text("target/qemux86-64 core2_64 qemux86_64\n")
    return deploy


def test_filtered_view_keeps_repo_map(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    view = feed_filter.build_filtered_view(deploy, tmp_path / "view", {"foo"})

    assert (view / "avocado-repo.map").read_text() == (deploy / "avocado-repo.map").read_text()


def test_filtered_view_keeps_only_named_packages_and_all_arch_dirs(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    view = feed_filter.build_filtered_view(deploy, tmp_path / "view", {"foo", "bar-lib"})

    assert sorted(p.name for p in (view / "core2_64").iterdir()) == [
        "bar-lib-2.1-r0.1.core2_64.rpm",
        "foo-1.0-r0.0.core2_64.rpm",
    ]
    assert (view / "qemux86_64").is_dir()
    assert list((view / "qemux86_64").iterdir()) == []
    assert (view / "noarch").is_dir()


def test_filtered_view_hardlinks_rather_than_copies(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    view = feed_filter.build_filtered_view(deploy, tmp_path / "view", {"foo"})

    src = deploy / "core2_64" / "foo-1.0-r0.0.core2_64.rpm"
    dst = view / "core2_64" / "foo-1.0-r0.0.core2_64.rpm"
    assert os.path.samefile(src, dst)


def test_filtered_view_matches_dashed_names_exactly(tmp_path: Path) -> None:
    """``bar`` must not select ``bar-lib``: the match is on the parsed name."""
    deploy = _deploy(tmp_path)
    view = feed_filter.build_filtered_view(deploy, tmp_path / "view", {"bar"})

    assert list((view / "core2_64").iterdir()) == []


def test_filtered_view_skips_unparseable_names(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    (deploy / "core2_64" / "junk.rpm").write_bytes(b"x")
    view = feed_filter.build_filtered_view(deploy, tmp_path / "view", {"junk"})

    assert list((view / "core2_64").iterdir()) == []


def test_filtered_view_without_map_is_an_error(tmp_path: Path) -> None:
    deploy = _deploy(tmp_path)
    (deploy / "avocado-repo.map").unlink()

    with pytest.raises(FileNotFoundError):
        feed_filter.build_filtered_view(deploy, tmp_path / "view", {"foo"})
