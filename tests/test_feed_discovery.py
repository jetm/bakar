"""Tests for discovering consolidation sources and sizing the saving.

Two failure modes drive this file.

The first is publishing packages nobody built. dnf vendors its own test fixtures
- 5,804 files named like ``kernel-doc-4.11.0-1.noarch.rpm`` at about 6 KB each -
inside every build's work tree, so anything that treats "a .rpm on disk" as feed
content puts fake packages in a served repository. Discovery is therefore
anchored on a build declaring an ``avocado-repo.map``, never on finding files.

The second is over-reporting the saving. ``~/repos/work/peridio-scarthgap-build``
is a symlink to the shared workspace, so three build trees are reachable by two
paths each, and OpenEmbedded hardlinks ``oe-rootfs-repo`` against
``tmp/deploy/rpm`` inside a single tree. Counting either as reclaimable inflates
the figure - measured once at 203 GB apparent against 123 GB real.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

from bakar.feed_consolidate import consolidation_savings, discover_trees

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

_MAP = "core2_64=$releasever/target/{m}/core2_64\nrepo=$releasever/sdk/all\nrepo=$releasever/target/{m}\n"


def _tree(root: Path, machine: str, *, codename: str | None = "dev/local", rpms: int = 2) -> Path:
    """A build tree declaring a repo map, with optional testdata and RPMs."""
    build = root / f"build-{machine}" / "build"
    deploy = build / "tmp" / "deploy"
    rpm = deploy / "rpm" / "core2_64"
    rpm.mkdir(parents=True)
    (deploy / "rpm" / "avocado-repo.map").write_text(_MAP.format(m=machine))
    for i in range(rpms):
        (rpm / f"pkg{i}-1.0-r0.core2_64.rpm").write_bytes(f"{machine}-{i}".encode())

    if codename is not None:
        images = deploy / "images" / f"avocado-{machine}"
        images.mkdir(parents=True)
        (images / f"avocado-image-rootfs-{machine}.testdata.json").write_text(
            json.dumps({"DISTRO_CODENAME": codename, "MACHINE": f"avocado-{machine}"})
        )
    return build


def _fixture_rpms(root: Path) -> None:
    """dnf's own vendored test fixtures, as they appear inside a work tree."""
    fixtures = (
        root
        / "build-x"
        / "build"
        / "tmp"
        / "work"
        / "x86_64-linux"
        / "dnf-native"
        / "4.24.0"
        / "sources"
        / "dnf-4.24.0"
        / "tests"
        / "modules"
        / "modules"
        / "_all"
        / "x86_64"
    )
    fixtures.mkdir(parents=True)
    for name in ("kernel-doc-4.11.0-1.noarch.rpm", "basesystem-11-3.noarch.rpm"):
        (fixtures / name).write_bytes(b"fixture")


def test_discovery_finds_a_tree_that_declares_a_repo_map(tmp_path) -> None:
    """A build declaring a map is a consolidation source."""
    _tree(tmp_path, "qemux86-64")

    trees = discover_trees([tmp_path])

    assert len(trees) == 1
    assert trees[0].repo_roots == ["sdk/all", "target/qemux86-64"]


def test_discovery_ignores_rpms_that_belong_to_no_declared_map(tmp_path) -> None:
    """A dependency's vendored test fixtures are not feed content.

    They are real ``.rpm`` files with plausible names, so the only thing
    separating them from packages is that no build declared them. Publishing
    them would advertise a kernel-doc nobody built as installable.
    """
    _fixture_rpms(tmp_path)

    assert discover_trees([tmp_path]) == []


def test_discovery_counts_one_tree_reached_by_two_paths_once(tmp_path) -> None:
    """A tree reachable through a symlinked ancestor is one tree, not two.

    This is the shape on the real host: a symlink to the shared workspace makes
    three build trees appear twice, and reporting them as duplicates would
    describe reclaimable space that does not exist.
    """
    real = tmp_path / "real"
    real.mkdir()
    _tree(real, "qemux86-64")
    alias = tmp_path / "alias"
    os.symlink(real, alias)

    trees = discover_trees([real, alias])

    assert len(trees) == 1


def test_discovery_reads_the_release_and_channel_the_build_declared(tmp_path) -> None:
    """Release and channel come from ``DISTRO_CODENAME``, not from the path.

    The map carries a ``$releasever`` placeholder, so a tree's release is not
    recoverable from it. Inferring one from a directory name would file packages
    where no target resolves them.
    """
    _tree(tmp_path, "qemux86-64", codename="2026/edge")

    tree = discover_trees([tmp_path])[0]

    assert (tree.release, tree.channel) == ("2026", "edge")


def test_discovery_reports_an_unknown_release_rather_than_guessing(tmp_path) -> None:
    """A tree with no declared codename reports None, not a default.

    Substituting a plausible release here is the failure this guards: it would
    silently file a tree under a release it never claimed.
    """
    _tree(tmp_path, "qemux86-64", codename=None)

    tree = discover_trees([tmp_path])[0]

    assert tree.release is None
    assert tree.channel is None


def test_discovery_reports_the_machine_it_found(tmp_path) -> None:
    """Each tree carries the machine, for reporting and for index checks."""
    _tree(tmp_path, "imx93-frdm")

    assert discover_trees([tmp_path])[0].machine == "avocado-imx93-frdm"


def test_discovery_of_an_empty_root_is_empty_not_an_error(tmp_path) -> None:
    """Nothing found is reported as nothing, so a caller can say so."""
    assert discover_trees([tmp_path]) == []


def test_savings_counts_distinct_content_not_paths(tmp_path) -> None:
    """Two trees holding identical package content count that content once."""
    a = _tree(tmp_path / "a", "qemux86-64", rpms=0)
    b = _tree(tmp_path / "b", "raspberrypi5", rpms=0)
    for build in (a, b):
        (build / "tmp" / "deploy" / "rpm" / "core2_64" / "shared-1.0-r0.noarch.rpm").write_bytes(b"same")

    saving = consolidation_savings(discover_trees([tmp_path]))

    assert saving["rpm_paths"] == 2
    assert saving["distinct_contents"] == 1


def test_savings_excludes_symlink_aliasing(tmp_path) -> None:
    """A tree seen twice contributes its bytes once."""
    real = tmp_path / "real"
    real.mkdir()
    _tree(real, "qemux86-64", rpms=3)
    os.symlink(real, tmp_path / "alias")

    saving = consolidation_savings(discover_trees([real, tmp_path / "alias"]))

    assert saving["rpm_paths"] == 3


def test_savings_excludes_hardlinks_within_one_tree(tmp_path) -> None:
    """A hardlinked copy inside a tree is not separately reclaimable.

    OpenEmbedded hardlinks its rootfs repo against the deploy directory, so
    counting both would roughly double the reported figure while freeing nothing.
    """
    build = _tree(tmp_path, "qemux86-64", rpms=1)
    rpm_dir = build / "tmp" / "deploy" / "rpm" / "core2_64"
    original = next(rpm_dir.glob("*.rpm"))
    os.link(original, rpm_dir / "hardlinked-copy.rpm")

    saving = consolidation_savings(discover_trees([tmp_path]))

    assert saving["rpm_paths"] == 2
    assert saving["distinct_contents"] == 1
