"""Tests that consolidating many trees preserves every distinct content.

The property is not "no duplicates". It is that nothing is lost, and the case
that makes those different is a package name appearing twice with different
bytes behind it. That is not hypothetical here: ``tzdata-*``,
``ca-certificates`` and ``alsa-topology-conf`` all occur with two distinct sizes
across these trees, and two ``avocado-ext-*`` packages differ across releases at
11,094 against 11,441 bytes. A consolidation keyed on file name would keep one
of each pair and silently discard the other.

Bakar's own contribution to preservation is routing: every tree gets synced, and
each one under the release it declared rather than a neighbour's. That is what
these tests assert. Content-addressing itself belongs to the renderer's pool,
which stores by sha256 and is exercised against real same-name packages
separately, because a byte-level claim needs real RPMs rather than fixtures.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from bakar.feed_consolidate import consolidate, discover_trees

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

_MAP = "core2_64=$releasever/target/{m}/core2_64\nrepo=$releasever/target/{m}\n"


def _tree(root: Path, machine: str, *, codename: str | None) -> None:
    """A build tree declaring one target repo, optionally naming its release."""
    deploy = root / f"build-{machine}" / "build" / "tmp" / "deploy"
    rpm = deploy / "rpm" / "core2_64"
    rpm.mkdir(parents=True)
    (deploy / "rpm" / "avocado-repo.map").write_text(_MAP.format(m=machine))
    (rpm / f"pkg-{machine}-1.0-r0.core2_64.rpm").write_bytes(machine.encode())
    if codename is not None:
        images = deploy / "images" / f"avocado-{machine}"
        images.mkdir(parents=True)
        (images / f"avocado-image-rootfs-{machine}.testdata.json").write_text(
            json.dumps({"DISTRO_CODENAME": codename, "MACHINE": f"avocado-{machine}"})
        )


def _scripts(tmp_path: Path) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("repo-stage-rpms.sh", "render-pool-local.py"):
        (scripts / name).write_text("#!/bin/sh\nexit 0\n")
        (scripts / name).chmod(0o755)
    return scripts


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 0, "", "")

    def releasevers(self) -> list[str]:
        """The releasever argument of every staging call, in order."""
        return [argv[3] for argv in self.calls if argv[0].endswith("repo-stage-rpms.sh")]


def test_consolidate_syncs_every_discovered_tree(tmp_path, monkeypatch) -> None:
    """No tree is dropped: a source left unsynced is content lost."""
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    src = tmp_path / "src"
    for machine in ("qemux86-64", "raspberrypi5", "imx93-frdm"):
        _tree(src, machine, codename="dev/local")

    results = consolidate(
        discover_trees([src]),
        feed_root=tmp_path / "feed",
        stage_root=tmp_path / "feed-stage",
        scripts=_scripts(tmp_path),
    )

    assert len(results) == 3
    assert all("skipped" not in r for r in results)
    assert len(rec.releasevers()) == 3


def test_consolidate_files_each_tree_under_the_release_it_declared(tmp_path, monkeypatch) -> None:
    """Two trees from different releases do not land on each other.

    Filing them together would make one release's target resolve the other's
    packages, which is the failure the per-release layout exists to prevent.
    """
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    src = tmp_path / "src"
    _tree(src, "qemux86-64", codename="2024/edge")
    _tree(src, "raspberrypi5", codename="2026/edge")

    consolidate(
        discover_trees([src]),
        feed_root=tmp_path / "feed",
        stage_root=tmp_path / "feed-stage",
        scripts=_scripts(tmp_path),
    )

    assert sorted(rec.releasevers()) == ["2024/edge", "2026/edge"]


def test_consolidate_skips_a_tree_that_declared_no_release(tmp_path, monkeypatch) -> None:
    """An undeclared tree is skipped with a reason, never filed under a guess.

    Guessing publishes its packages at a path no target resolves, which looks
    like a successful consolidation and serves nothing - the worst shape of
    failure available here.
    """
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    src = tmp_path / "src"
    _tree(src, "qemux86-64", codename=None)

    results = consolidate(
        discover_trees([src]),
        feed_root=tmp_path / "feed",
        stage_root=tmp_path / "feed-stage",
        scripts=_scripts(tmp_path),
    )

    assert "skipped" in results[0]
    assert rec.calls == []


def test_consolidate_override_files_undeclared_trees_under_one_channel(tmp_path, monkeypatch) -> None:
    """An explicit release lets an operator place trees that declare none.

    This is the real path for local builds: every one of them declares
    ``dev/local``, so consolidating into a channel a client fetches needs the
    operator to say which - and saying it is a decision, not a default.
    """
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    src = tmp_path / "src"
    _tree(src, "qemux86-64", codename=None)

    results = consolidate(
        discover_trees([src]),
        feed_root=tmp_path / "feed",
        stage_root=tmp_path / "feed-stage",
        scripts=_scripts(tmp_path),
        release="2026",
        channel="edge",
    )

    assert "skipped" not in results[0]
    assert rec.releasevers() == ["2026/edge"]


def test_consolidate_shares_one_stage_root_across_every_tree(tmp_path, monkeypatch) -> None:
    """All trees stage into one root, so the release-global repo unions.

    The same property task 3.2 pins for two syncs of one workspace, asserted
    here for the consolidation path that drives many trees at once.
    """
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    src = tmp_path / "src"
    for machine in ("qemux86-64", "raspberrypi5"):
        _tree(src, machine, codename="dev/local")

    consolidate(
        discover_trees([src]),
        feed_root=tmp_path / "feed",
        stage_root=tmp_path / "feed-stage",
        scripts=_scripts(tmp_path),
    )

    stage_args = {argv[2] for argv in rec.calls if argv[0].endswith("repo-stage-rpms.sh")}
    assert stage_args == {str(tmp_path / "feed-stage")}


def test_consolidate_reports_the_machine_for_each_result(tmp_path, monkeypatch) -> None:
    """Results are attributable, so a partial run says which trees landed."""
    monkeypatch.setattr(subprocess, "run", _Recorder())
    src = tmp_path / "src"
    _tree(src, "qemux86-64", codename="dev/local")

    results = consolidate(
        discover_trees([src]),
        feed_root=tmp_path / "feed",
        stage_root=tmp_path / "feed-stage",
        scripts=_scripts(tmp_path),
    )

    assert results[0]["machine"] == "avocado-qemux86-64"
