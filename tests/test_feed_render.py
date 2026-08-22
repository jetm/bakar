"""Tests for staging, head-and-snapshot rendering, and the latest pointer.

The ordering assertions are the point of this file. ``snapshots-latest.json`` is
what ``avocado-cli`` reads to auto-pin a runtime, so a pointer written before its
snapshot has finished rendering names a snapshot that is missing repositories -
and a client that pins it gets a feed it cannot resolve from. The interrupted-sync
test is therefore the load-bearing one: it asserts the pointer is absent rather
than asserting a happy path.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from bakar.feed import (
    render_repo,
    snapshot_id,
    stage_build,
    sync,
)
from tests.conftest import make_build_config

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

_MAP = "core2_64=$releasever/target/qemux86-64/core2_64\nrepo=$releasever/sdk/all\nrepo=$releasever/target/qemux86-64\n"


def _deploy(tmp_path: Path) -> Path:
    """Write a minimal RPM deploy dir carrying an ``avocado-repo.map``."""
    deploy = tmp_path / "build" / "tmp" / "deploy" / "rpm"
    deploy.mkdir(parents=True)
    (deploy / "avocado-repo.map").write_text(_MAP)
    return deploy


def _stage(tmp_path: Path, roots: tuple[str, ...], *, release: str, channel: str) -> None:
    """Create the staged directories the staging script would have made.

    Needed because these tests mock ``subprocess``, so the real staging script
    never runs and leaves nothing on disk. ``sync`` skips a repo root with no
    staged tree - the renderer exits non-zero on a missing one - so a test that
    stubs staging has to stand in for its effect or every root is skipped.
    """
    for root in roots:
        (tmp_path / "feed-stage" / release / channel / root).mkdir(parents=True, exist_ok=True)


def _scripts(tmp_path: Path) -> Path:
    """Create a ``meta-avocado/scripts`` dir holding both driven scripts."""
    scripts = tmp_path / "meta-avocado" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("repo-stage-rpms.sh", "render-pool-local.py"):
        (scripts / name).write_text("#!/bin/sh\nexit 0\n")
        (scripts / name).chmod(0o755)
    return scripts


class _Recorder:
    """Record every ``subprocess.run`` argv, optionally failing the Nth call."""

    def __init__(self, *, fail_on: int | None = None) -> None:
        self.calls: list[list[str]] = []
        self._fail_on = fail_on

    def __call__(self, argv, **kwargs):
        self.calls.append([str(a) for a in argv])
        if self._fail_on is not None and len(self.calls) == self._fail_on:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def subpaths(self) -> list[str]:
        """The ``--subpath`` value of every render call, in order."""
        return [argv[argv.index("--subpath") + 1] for argv in self.calls if "--subpath" in argv]


def test_snapshot_id_is_a_utc_stamp() -> None:
    """The default snapshot id is a sortable UTC stamp."""
    fixed = datetime(2026, 8, 22, 9, 5, 1, tzinfo=UTC)

    assert snapshot_id(now=fixed) == "20260822T090501Z"


def test_stage_build_passes_release_and_channel_as_the_releasever(tmp_path, monkeypatch) -> None:
    """Staging receives ``<release>/<channel>`` so the map's prefix expands.

    The script expands ``$releasever`` inside each map value, so the staged tree
    already carries the release and channel prefix. Passing only the release
    would stage into a tree the renderer is never pointed at.
    """
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    deploy, scripts = _deploy(tmp_path), _scripts(tmp_path)

    staged = stage_build(
        deploy_dir=deploy,
        stage_root=tmp_path / "_feed-stage",
        scripts=scripts,
        release="2026",
        channel="edge",
    )

    assert rec.calls[0][1:] == [str(deploy), str(tmp_path / "_feed-stage"), "2026/edge"]
    assert staged == tmp_path / "_feed-stage" / "2026" / "edge"


def test_render_repo_passes_the_three_required_flags(tmp_path, monkeypatch) -> None:
    """The renderer is driven by ``--staged``, ``--channel-root`` and ``--subpath``."""
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    scripts = _scripts(tmp_path)

    render_repo(
        scripts=scripts,
        staged=tmp_path / "staged",
        channel_root=tmp_path / "feed" / "2026" / "edge",
        subpath="target/qemux86-64",
    )

    argv = rec.calls[0]
    assert argv[argv.index("--staged") + 1] == str(tmp_path / "staged")
    assert argv[argv.index("--channel-root") + 1] == str(tmp_path / "feed" / "2026" / "edge")
    assert argv[argv.index("--subpath") + 1] == "target/qemux86-64"


def test_sync_renders_every_repo_root_head_and_snapshot(tmp_path, monkeypatch) -> None:
    """Each declared root renders twice: the mutable head and the snapshot."""
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    deploy, scripts = _deploy(tmp_path), _scripts(tmp_path)
    cfg = make_build_config(workspace=tmp_path, feed_dir=str(tmp_path / "feed"))
    _stage(tmp_path, ("sdk/all", "target/qemux86-64"), release="2026", channel="edge")

    sync(cfg, deploy_dir=deploy, scripts=scripts, release="2026", channel="edge", snapshot="SNAP")

    assert rec.subpaths() == [
        "sdk/all",
        "snapshots/SNAP/sdk/all",
        "target/qemux86-64",
        "snapshots/SNAP/target/qemux86-64",
    ]


def test_sync_snapshot_subpath_is_deeper_by_exactly_the_snapshot_prefix(tmp_path, monkeypatch) -> None:
    """A snapshot subpath is the head subpath under ``snapshots/<id>/``.

    The renderer derives its pool reference from subpath depth, so this is the
    whole mechanism by which a snapshot references the same pool as the head
    without the script needing to know snapshots exist.
    """
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    deploy, scripts = _deploy(tmp_path), _scripts(tmp_path)
    cfg = make_build_config(workspace=tmp_path, feed_dir=str(tmp_path / "feed"))
    _stage(tmp_path, ("sdk/all", "target/qemux86-64"), release="2026", channel="edge")

    sync(cfg, deploy_dir=deploy, scripts=scripts, release="2026", channel="edge", snapshot="SNAP")

    heads = [s for s in rec.subpaths() if not s.startswith("snapshots/")]
    snaps = [s for s in rec.subpaths() if s.startswith("snapshots/")]
    assert snaps == [f"snapshots/SNAP/{h}" for h in heads]


def test_sync_writes_the_latest_pointer_after_every_render(tmp_path, monkeypatch) -> None:
    """The pointer exists once the sync completes, naming the minted snapshot."""
    monkeypatch.setattr(subprocess, "run", _Recorder())
    deploy, scripts = _deploy(tmp_path), _scripts(tmp_path)
    cfg = make_build_config(workspace=tmp_path, feed_dir=str(tmp_path / "feed"))
    _stage(tmp_path, ("sdk/all", "target/qemux86-64"), release="2026", channel="edge")

    sync(cfg, deploy_dir=deploy, scripts=scripts, release="2026", channel="edge", snapshot="SNAP")

    pointer = tmp_path / "feed" / "2026" / "edge" / "snapshots-latest.json"
    body = json.loads(pointer.read_text())
    assert body["id"] == "SNAP"
    assert body["created"].endswith("Z")


def test_sync_interrupted_mid_render_leaves_no_pointer(tmp_path, monkeypatch) -> None:
    """A sync that dies before the last render must not announce the snapshot.

    This is the ordering guarantee. The pointer is what a client pins against,
    so announcing a snapshot whose repositories are still missing hands out a
    pin that cannot resolve. Absent is the correct state; partial is not.
    """
    # 1 stage call + 4 render calls; fail the last render.
    monkeypatch.setattr(subprocess, "run", _Recorder(fail_on=5))
    deploy, scripts = _deploy(tmp_path), _scripts(tmp_path)
    cfg = make_build_config(workspace=tmp_path, feed_dir=str(tmp_path / "feed"))
    _stage(tmp_path, ("sdk/all", "target/qemux86-64"), release="2026", channel="edge")

    with pytest.raises(subprocess.CalledProcessError):
        sync(cfg, deploy_dir=deploy, scripts=scripts, release="2026", channel="edge", snapshot="SNAP")

    pointer = tmp_path / "feed" / "2026" / "edge" / "snapshots-latest.json"
    assert not pointer.exists()


def test_sync_interrupted_leaves_a_prior_pointer_untouched(tmp_path, monkeypatch) -> None:
    """A failed sync leaves the previously announced snapshot in place.

    Rewriting the pointer first and the snapshot second would strand a client on
    a snapshot that never finished, and lose the last one that did.
    """
    channel = tmp_path / "feed" / "2026" / "edge"
    channel.mkdir(parents=True)
    pointer = channel / "snapshots-latest.json"
    pointer.write_text(json.dumps({"id": "OLD", "created": "2026-08-01T00:00:00Z"}))

    monkeypatch.setattr(subprocess, "run", _Recorder(fail_on=5))
    deploy, scripts = _deploy(tmp_path), _scripts(tmp_path)
    cfg = make_build_config(workspace=tmp_path, feed_dir=str(tmp_path / "feed"))
    _stage(tmp_path, ("sdk/all", "target/qemux86-64"), release="2026", channel="edge")

    with pytest.raises(subprocess.CalledProcessError):
        sync(cfg, deploy_dir=deploy, scripts=scripts, release="2026", channel="edge", snapshot="SNAP")

    assert json.loads(pointer.read_text())["id"] == "OLD"


def test_sync_stages_before_it_renders(tmp_path, monkeypatch) -> None:
    """Staging is the first call; a render over an unstaged tree finds nothing."""
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    deploy, scripts = _deploy(tmp_path), _scripts(tmp_path)
    cfg = make_build_config(workspace=tmp_path, feed_dir=str(tmp_path / "feed"))
    _stage(tmp_path, ("sdk/all", "target/qemux86-64"), release="2026", channel="edge")

    sync(cfg, deploy_dir=deploy, scripts=scripts, release="2026", channel="edge", snapshot="SNAP")

    assert rec.calls[0][0].endswith("repo-stage-rpms.sh")
    assert all("--subpath" not in c for c in rec.calls[:1])


def test_sync_skips_a_repo_root_that_was_never_staged(tmp_path, monkeypatch) -> None:
    """A declared repo with no staged tree is skipped, not rendered.

    A build's map declares every repo root the machine COULD publish, but a root
    whose arch source directories are all absent from that build stages nothing.
    The renderer tolerates an empty staged directory and hard-exits on a missing
    one, so rendering it fails the whole sync over a repo the build simply did
    not produce. Observed on the real imx93 tree, whose map declares
    ``sdk/imx93-frdm`` while its only contributing arch dir does not exist.
    """
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    deploy = tmp_path / "build" / "tmp" / "deploy" / "rpm"
    deploy.mkdir(parents=True)
    (deploy / "avocado-repo.map").write_text(
        "repo=$releasever/sdk/all\nrepo=$releasever/sdk/never-staged\nrepo=$releasever/target/qemux86-64\n"
    )
    scripts = _scripts(tmp_path)
    cfg = make_build_config(workspace=tmp_path, feed_dir=str(tmp_path / "feed"))
    _stage(tmp_path, ("sdk/all", "target/qemux86-64"), release="2026", channel="edge")

    # _stage above created sdk/all and target/qemux86-64 only, so
    # sdk/never-staged has no directory - which is the case under test.
    result = sync(cfg, deploy_dir=deploy, scripts=scripts, release="2026", channel="edge", snapshot="SNAP")

    assert "sdk/never-staged" not in rec.subpaths()
    assert "snapshots/SNAP/sdk/never-staged" not in rec.subpaths()
    assert result["unstaged"] == ["sdk/never-staged"]


def test_sync_still_writes_the_pointer_when_a_repo_was_unstaged(tmp_path, monkeypatch) -> None:
    """An unstaged repo is not a failure, so the snapshot is still announced.

    Treating it as one would make a machine that legitimately publishes fewer
    repos than its map declares unable to sync at all.
    """
    monkeypatch.setattr(subprocess, "run", _Recorder())
    deploy = tmp_path / "build" / "tmp" / "deploy" / "rpm"
    deploy.mkdir(parents=True)
    (deploy / "avocado-repo.map").write_text("repo=$releasever/sdk/all\nrepo=$releasever/sdk/never-staged\n")
    scripts = _scripts(tmp_path)
    cfg = make_build_config(workspace=tmp_path, feed_dir=str(tmp_path / "feed"))
    _stage(tmp_path, ("sdk/all",), release="2026", channel="edge")

    sync(cfg, deploy_dir=deploy, scripts=scripts, release="2026", channel="edge", snapshot="SNAP")

    pointer = tmp_path / "feed" / "2026" / "edge" / "snapshots-latest.json"
    assert pointer.is_file()


def test_sync_points_each_render_at_that_repos_staged_subtree(tmp_path, monkeypatch) -> None:
    """A repo renders from its own staged subtree, not from the stage root.

    Handing the renderer the whole stage root would make every repository
    contain every other repository's packages.
    """
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    deploy, scripts = _deploy(tmp_path), _scripts(tmp_path)
    cfg = make_build_config(workspace=tmp_path, feed_dir=str(tmp_path / "feed"))
    _stage(tmp_path, ("sdk/all", "target/qemux86-64"), release="2026", channel="edge")

    sync(cfg, deploy_dir=deploy, scripts=scripts, release="2026", channel="edge", snapshot="SNAP")

    staged_base = tmp_path / "feed-stage" / "2026" / "edge"
    for argv in rec.calls[1:]:
        subpath = argv[argv.index("--subpath") + 1].removeprefix("snapshots/SNAP/")
        assert argv[argv.index("--staged") + 1] == str(staged_base / subpath)
