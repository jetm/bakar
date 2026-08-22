"""A1 premise: staging accumulates across machines instead of replacing.

``sdk/all`` is release-global. The renderer runs ``createrepo_c`` over whatever
staged tree it is handed, so if two machines stage into different roots, the
second machine's render of that repository REPLACES the first machine's rather
than unioning with it - and the shared toolchain repository quietly shrinks to
whatever was staged last. Nothing errors, dnf still resolves, the feed still
serves. That silence is what makes this the change's central bet and why it gets
a test rather than a comment.

Scope of what is asserted here. Bakar's contribution to the union is threefold:
both machines resolve the SAME stage root, both point the render at the same
staged subtree, and nothing between the two syncs clears that root. Those are
hermetic and are what this file covers. That the staging script itself merges
rather than truncates is meta-avocado's tar-pipe behaviour, which cannot be
asserted here without depending on that checkout existing - it is verified by
running it against real multi-machine RPMs, recorded on the change.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from bakar.feed import resolve_stage_root, sync
from tests.conftest import make_build_config

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

# Each machine declares the release-global toolchain repo plus its own target.
# all_avocadosdk is the arch key that stages into sdk/all, which is the
# repository the union property is about.
_MAP = "all_avocadosdk=$releasever/sdk/all\nrepo=$releasever/sdk/all\nrepo=$releasever/target/{machine}\n"


def _deploy(tmp_path: Path, machine: str) -> Path:
    """A deploy dir for ``machine`` declaring sdk/all and its own target."""
    deploy = tmp_path / f"build-{machine}" / "tmp" / "deploy" / "rpm"
    (deploy / "all_avocadosdk").mkdir(parents=True)
    (deploy / "avocado-repo.map").write_text(_MAP.format(machine=machine))
    (deploy / "all_avocadosdk" / f"toolchain-{machine}-1.0.noarch.rpm").write_bytes(b"x")
    return deploy


def _scripts(tmp_path: Path) -> Path:
    scripts = tmp_path / "meta-avocado" / "scripts"
    scripts.mkdir(parents=True)
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

    def stage_targets(self) -> list[str]:
        """The stage-root argument of every staging call, in order."""
        return [argv[2] for argv in self.calls if argv[0].endswith("repo-stage-rpms.sh")]

    def staged_for(self, subpath: str) -> list[str]:
        """The ``--staged`` value of every render of ``subpath``."""
        return [
            argv[argv.index("--staged") + 1]
            for argv in self.calls
            if "--subpath" in argv and argv[argv.index("--subpath") + 1] == subpath
        ]


def _sync_machine(tmp_path: Path, scripts: Path, machine: str) -> None:
    """Sync ``machine`` with a config whose ``machine`` field is that machine.

    Varying the field, not just the deploy directory, is the whole point: a
    per-machine stage root is indistinguishable from a shared one when every
    config in the test carries the same machine, which is how a vacuous version
    of this test passes against the very implementation it exists to reject.
    """
    cfg = make_build_config(
        workspace=tmp_path,
        feed_dir=str(tmp_path / "feed"),
        machine=machine,
    )
    # Stand in for the staging script, which is mocked here: sync skips a repo
    # root with no staged tree, because the renderer exits non-zero on a missing
    # one. Both machines stage into the SAME root, which is the property tested.
    for root in ("sdk/all", f"target/{machine}"):
        (tmp_path / "feed-stage" / "2026" / "edge" / root).mkdir(parents=True, exist_ok=True)
    sync(
        cfg,
        deploy_dir=_deploy(tmp_path, machine),
        scripts=scripts,
        release="2026",
        channel="edge",
        snapshot=f"SNAP-{machine}",
    )


def test_two_machines_stage_into_one_root(tmp_path, monkeypatch) -> None:
    """Both machines hand the staging script the SAME stage root.

    A per-machine stage root is the silent truncation. This asserts the two
    calls agree rather than asserting either value, because the specific path
    does not matter and the agreement is the whole property.
    """
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    scripts = _scripts(tmp_path)

    for machine in ("qemux86-64", "imx93-frdm"):
        _sync_machine(tmp_path, scripts, machine)

    targets = rec.stage_targets()
    assert len(targets) == 2
    assert targets[0] == targets[1]


def test_both_machines_render_sdk_all_from_the_same_staged_tree(tmp_path, monkeypatch) -> None:
    """``sdk/all`` is rendered from one accumulated tree, not two private ones.

    This is the assertion that would fail under a per-machine stage root: each
    machine would render the release-global repository from its own subtree, so
    the second render would publish only its own packages.
    """
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    scripts = _scripts(tmp_path)

    for machine in ("qemux86-64", "imx93-frdm"):
        _sync_machine(tmp_path, scripts, machine)

    staged = rec.staged_for("sdk/all")
    assert len(staged) == 2
    assert staged[0] == staged[1]


def test_sync_does_not_clear_the_stage_root(tmp_path, monkeypatch) -> None:
    """A sync leaves content an earlier machine staged in place.

    Clearing the stage root per sync would reproduce the truncation even with a
    shared root, so this asserts on real files rather than on argv: content put
    there before the sync must survive it.
    """
    monkeypatch.setattr(subprocess, "run", _Recorder())
    scripts = _scripts(tmp_path)
    cfg = make_build_config(workspace=tmp_path, feed_dir=str(tmp_path / "feed"))

    stage = resolve_stage_root(cfg)
    earlier = stage / "2026" / "edge" / "sdk" / "all" / "toolchain-from-machine-a-1.0.noarch.rpm"
    earlier.parent.mkdir(parents=True)
    earlier.write_bytes(b"x")

    sync(
        cfg,
        deploy_dir=_deploy(tmp_path, "imx93-frdm"),
        scripts=scripts,
        release="2026",
        channel="edge",
        snapshot="SNAP",
    )

    assert earlier.is_file()


def test_a_second_machine_does_not_retarget_the_first_machines_repo(tmp_path, monkeypatch) -> None:
    """Each machine still renders its own target repo, at its own subpath.

    The union applies to the release-global repository. A machine's own target
    repository is private to it, and collapsing those onto one subpath would be
    the opposite failure - one machine's packages served as another's.
    """
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    scripts = _scripts(tmp_path)

    for machine in ("qemux86-64", "imx93-frdm"):
        _sync_machine(tmp_path, scripts, machine)

    rendered = [
        argv[argv.index("--subpath") + 1]
        for argv in rec.calls
        if "--subpath" in argv and not argv[argv.index("--subpath") + 1].startswith("snapshots/")
    ]
    assert rendered == ["sdk/all", "target/qemux86-64", "sdk/all", "target/imx93-frdm"]
