"""Tests for deriving the target index from the rendered feed.

Derived from what was RENDERED, never from the build-time map fragments the
historical method used. The difference matters because a map declares what a
machine could publish while the tree records what it did: a root that was
declared and never staged has no repodata, and advertising it tells a client to
fetch a repository that does not exist.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bakar.feed_index import canonical_repos, derive_targets, write_targets_index

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _rendered(channel: Path, subpath: str) -> None:
    """Mark ``subpath`` as rendered by giving it a metadata index."""
    repodata = channel / subpath / "repodata"
    repodata.mkdir(parents=True, exist_ok=True)
    (repodata / "repomd.xml").write_text("<repomd/>")


def test_a_rendered_machine_appears_in_the_index(tmp_path) -> None:
    """A machine with rendered target metadata is advertised."""
    _rendered(tmp_path, "target/qemux86-64")

    assert list(derive_targets(tmp_path)) == ["qemux86-64"]


def test_a_directory_without_repodata_is_not_advertised(tmp_path) -> None:
    """An unrendered machine directory is omitted.

    Nothing can be installed from it, so advertising it hands a client a
    repository that will 404 on its metadata.
    """
    (tmp_path / "target" / "half-staged").mkdir(parents=True)
    _rendered(tmp_path, "target/qemux86-64")

    assert list(derive_targets(tmp_path)) == ["qemux86-64"]


def test_an_extension_repo_is_not_a_machine(tmp_path) -> None:
    """``target/<machine>-ext`` is a repository of a machine, not another machine.

    Treating it as one would advertise ``qemux86-64-ext`` as a target and then
    emit ``target/qemux86-64-ext-ext`` in its canonical list.
    """
    _rendered(tmp_path, "target/qemux86-64")
    _rendered(tmp_path, "target/qemux86-64-ext")

    assert list(derive_targets(tmp_path)) == ["qemux86-64"]


def test_machines_are_ordered_deterministically(tmp_path) -> None:
    """The index is sorted, so re-deriving an unchanged feed is a no-op diff."""
    for machine in ("raspberrypi5", "imx93-frdm", "qemux86-64"):
        _rendered(tmp_path, f"target/{machine}")

    assert list(derive_targets(tmp_path)) == ["imx93-frdm", "qemux86-64", "raspberrypi5"]


def test_canonical_repos_lists_the_four_repos_that_lock_a_target(tmp_path) -> None:
    """A target is defined by four repositories, in production's own order.

    The release-global toolchain repo comes first because it is shared; the
    extension repo is last and is advertised even when empty, which matches
    production - an empty extension repo is valid.
    """
    assert canonical_repos("qemux86-64") == [
        "sdk/all",
        "target/qemux86-64",
        "sdk/qemux86-64",
        "target/qemux86-64-ext",
    ]


def test_write_targets_index_emits_a_machine_keyed_document(tmp_path) -> None:
    """``targets.json`` maps each machine to its four repositories."""
    _rendered(tmp_path, "target/qemux86-64")
    _rendered(tmp_path, "target/imx93-frdm")

    path = write_targets_index(tmp_path)

    body = json.loads(path.read_text())
    assert sorted(body) == ["imx93-frdm", "qemux86-64"]
    assert body["qemux86-64"] == canonical_repos("qemux86-64")


def test_write_targets_index_is_idempotent(tmp_path) -> None:
    """Re-deriving an unchanged tree rewrites identical bytes.

    A feed is re-indexed after every sync, so churn here would show up as a
    change in every diff and hide the real ones.
    """
    _rendered(tmp_path, "target/qemux86-64")

    first = write_targets_index(tmp_path).read_bytes()
    second = write_targets_index(tmp_path).read_bytes()

    assert first == second


def test_write_targets_index_on_an_empty_feed_writes_an_empty_document(tmp_path) -> None:
    """An empty feed indexes to an empty mapping, not a missing file.

    A client fetching the index gets a valid empty answer rather than a 404 it
    would have to interpret.
    """
    path = write_targets_index(tmp_path)

    assert json.loads(path.read_text()) == {}


def test_the_index_ignores_snapshot_subtrees(tmp_path) -> None:
    """Machines are read from the head, not from inside snapshots.

    Snapshots carry the same repository layout, so walking into them would
    advertise every machine once per retained snapshot.
    """
    _rendered(tmp_path, "target/qemux86-64")
    _rendered(tmp_path, "snapshots/20260822T000000Z/target/only-in-a-snapshot")

    assert list(derive_targets(tmp_path)) == ["qemux86-64"]
