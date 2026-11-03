"""End to end: a layer repository entry builds locally and dispatches without moving a shared clone.

Assumption A1 of the layer-repo-dispatch change: a layer repository's kas entry
pins meta-avocado by commit, the release workspace's meta-avocado clone may sit
on another commit, and the entry still builds (and ships) against the pin
without changing HEAD, index or working tree of any shared clone. The local half
drives ``bakar build`` through the real CLI on ``file://`` git fixtures with the
real ``kas`` binary; only the final ``kas build`` launch is stubbed. Every kas
process is recorded at the subprocess boundary and classified by the guard of
``test_layer_repo_kas_guard``. The delivery half calls ``dispatch_layer_build``
through the fake host transport of ``test_avocado_dispatch``. When
``BAKAR_REQUIRE_KAS`` is set a missing kas fails the tests instead of skipping
them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from bakar import bsp_detect
from bakar.config import meta_avocado_build_dir_name
from bakar.steps import avocado_dispatch as ad
from bakar.steps import layer_delivery, remote_dispatch
from tests import test_cli_build_layer as base
from tests.test_avocado_dispatch import HOST, LayerRec, Recorder
from tests.test_layer_repo_kas_guard import (
    REUSE_OVERLAY,
    Invocation,
    _is_kas,
    _parse_kas_argv,
    _unprotected,
    classify_invocation,
)
from tests.test_layer_repo_kas_guard import Recorder as KasRecorder

if TYPE_CHECKING:
    from collections.abc import Iterator

# The CLI fixtures of the 4.1 test, shared so the environment a build runs in is defined once.
_require_kas = base._require_kas
launched = base.launched

_REAL_RUN = subprocess.run
_LAYER_NAME = "meta-board"
_RELEASE = "wrynose"


def _rgit(repo: Path, *args: str) -> str:
    """Real git, immune to the dispatch recorder that replaces ``subprocess.run`` for a test."""
    return _REAL_RUN(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **base._GIT_ENV},
    ).stdout.strip()


@dataclass(frozen=True)
class _World:
    ws: Path
    layer: Path
    entry: Path
    upstream: Path
    pin: str
    head: str

    @property
    def clone(self) -> Path:
        return self.ws / "meta-avocado"

    @property
    def side(self) -> Path:
        return self.ws / ".bakar" / "pins" / "meta-avocado" / self.pin[:12]

    @property
    def shared(self) -> list[Path]:
        return [self.clone, self.ws / "openembedded-core", self.ws / "bitbake"]


def _world(tmp_path: Path) -> _World:
    """A release workspace whose meta-avocado clone sits one commit past the entry's pin.

    meta-avocado has its own upstream so the entry's url can still be cloned
    after the workspace's copy is moved or removed.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    oe = base._init(tmp_path / "upstream" / "openembedded-core")
    oe_pin = base._commit(oe, {"meta/conf/layer.conf": ""}, "oe")
    bitbake = base._init(tmp_path / "upstream" / "bitbake")
    bb_pin = base._commit(bitbake, {"bin/bitbake": ""}, "bitbake")
    upstream = base._init(tmp_path / "upstream" / "meta-avocado", _RELEASE)
    pin = base._commit(upstream, {"kas/base.yml": base._base("pinned", (oe, oe_pin), (bitbake, bb_pin))}, "pinned")
    head = base._commit(upstream, {"kas/base.yml": base._base("head", (oe, oe_pin), (bitbake, bb_pin))}, "head")
    assert pin != head
    # oe-core and bitbake sit at their pins, clean; meta-avocado one commit past its.
    for src, name in ((oe, "openembedded-core"), (bitbake, "bitbake"), (upstream, "meta-avocado")):
        base._git(ws, "clone", "-q", f"file://{src}", str(ws / name))

    layer = base._init(tmp_path / "layers" / _LAYER_NAME)
    entry_doc = {
        "header": {"version": 16, "includes": [{"repo": "meta-avocado", "file": "kas/base.yml"}]},
        "machine": "qemux86-64",
        "target": "core-image-minimal",
        "repos": {
            _LAYER_NAME: {"path": "."},
            "meta-avocado": {"url": f"file://{upstream}", "branch": _RELEASE, "commit": pin},
        },
    }
    base._commit(layer, {"kas/entry.yml": yaml.safe_dump(entry_doc)}, "entry")
    return _World(ws=ws, layer=layer, entry=layer / "kas" / "entry.yml", upstream=upstream, pin=pin, head=head)


def _state(repo: Path) -> tuple[str, ...]:
    """HEAD, status, index content, refs, worktrees and local config of ``repo``."""
    return (
        _rgit(repo, "rev-parse", "HEAD"),
        _rgit(repo, "status", "--porcelain", "--untracked-files=all", "--ignored"),
        _rgit(repo, "ls-files", "-s"),
        _rgit(repo, "for-each-ref"),
        _rgit(repo, "worktree", "list", "--porcelain"),
        _rgit(repo, "config", "--local", "--list"),
    )


def _checkout_pin(world: _World, repo: Path) -> None:
    base._git(repo, "checkout", "-q", world.pin)


def _make_side(world: _World) -> None:
    base._git(world.ws, "clone", "-q", f"file://{world.upstream}", str(world.side))
    _checkout_pin(world, world.side)


# ---------------------------------------------------------------------------
# kas recorded at the subprocess boundary, the real binary running behind it
# ---------------------------------------------------------------------------


@pytest.fixture
def kas_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[KasRecorder]:
    rec = KasRecorder()
    real_run, real_popen = subprocess.run, subprocess.Popen

    def spy_run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_kas(argv):
            rec.record(argv, kwargs)
        return real_run(argv, *args, **kwargs)

    def spy_popen(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_kas(argv):
            rec.record(argv, kwargs)
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    yield rec


@dataclass(frozen=True)
class _Built:
    rc: int
    build_dir: Path
    wrapper: dict
    overlay: dict
    dump: dict


def _build(world: _World, monkeypatch: pytest.MonkeyPatch, launched: list[Any], kas_calls: KasRecorder) -> _Built:
    base._map_release(monkeypatch, _RELEASE, world.ws)
    cwd = world.ws.parent / "elsewhere"
    cwd.mkdir(exist_ok=True)
    monkeypatch.chdir(cwd)
    rc = base._run_cli(monkeypatch, str(world.entry), "-w", str(world.ws))
    build_dir = world.ws / meta_avocado_build_dir_name(world.entry, None)
    if launched:
        # The stubbed launch is a kas call too; the guard classifies it like the others.
        cmd = list(launched[0].cmd)
        start = next(i for i, tok in enumerate(cmd) if Path(tok).name == "kas")
        sub, skips, files = _parse_kas_argv(cmd[start:])
        kas_calls.invocations.append(Invocation("final build launch", tuple(cmd), build_dir, None, skips, files, sub))

    def load(name: str) -> dict:
        path = build_dir / name
        return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}

    return _Built(rc, build_dir, load("avocado-wrapper.yml"), load(REUSE_OVERLAY), load("avocado-bakar.yml"))


def _assert_protected(world: _World, kas_calls: KasRecorder, built: _Built) -> None:
    assert _unprotected(kas_calls, world.ws) == []
    kinds = {
        (inv.subcommand, classify_invocation(inv, world.ws, kas_calls.invocations[:i]))
        for i, inv in enumerate(kas_calls.invocations)
    }
    # A guard over an empty recording passes for the wrong reason.
    assert ("dump", "throwaway") in kinds  # the layer reader of the pinned commit
    assert ("dump", "overlay") in kinds  # the build's own dump
    assert ("build", "generated-dump") in kinds  # the launch, on the self-contained dump
    dumps = [inv for inv in kas_calls.invocations if inv.subcommand == "dump" and inv.cwd == built.build_dir]
    assert dumps
    names = [Path(part).name for part in (dumps[-1].files or "").split(":")]
    assert names[0] == "avocado-wrapper.yml"
    assert REUSE_OVERLAY in names
    assert names[-1] == "avocado-pin.yml"


def _dependency_path(built: _Built) -> str:
    return built.wrapper["repos"]["meta-avocado"]["path"]


# ---------------------------------------------------------------------------
# The three reuse classifications, through the real CLI
# ---------------------------------------------------------------------------


def test_clone_on_another_commit_builds_from_the_pin_in_a_side_checkout_and_the_clones_stay_put(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launched: list[Any], kas_calls: KasRecorder
) -> None:
    world = _world(tmp_path)
    before = {clone: _state(clone) for clone in world.shared}
    assert _rgit(world.clone, "rev-parse", "HEAD") == world.head

    built = _build(world, monkeypatch, launched, kas_calls)

    assert built.rc == 0
    assert len(launched) == 1
    # The dependency's includes come from the pinned commit, not the clone's HEAD.
    assert built.dump["local_conf_header"]["dep"] == 'DEP = "pinned"'
    # Classified side: the overlay points meta-avocado at the pin's own checkout and keeps its url,
    # because that checkout does not exist yet and kas has to create it.
    side_rel = f".bakar/pins/meta-avocado/{world.pin[:12]}"
    assert built.overlay["repos"]["meta-avocado"] == {"path": side_rel}
    # Both other clones are at their pins and clean: frozen, declared by path with the url cleared.
    for name in ("openembedded-core", "bitbake"):
        assert built.overlay["repos"][name] == {"path": name, "url": None}
    # The wrapper declares the dependency at the side checkout and the layer at its own checkout.
    assert _dependency_path(built) == str(world.side.resolve())
    assert built.wrapper["repos"][_LAYER_NAME]["path"] == str(world.layer.resolve())
    assert (world.side / "kas" / "base.yml").is_file()
    assert _rgit(world.side, "rev-parse", "HEAD") == world.pin
    # No shared clone moved: HEAD, status, index, refs, worktrees and config are as they were.
    assert {clone: _state(clone) for clone in world.shared} == before
    assert _rgit(world.clone, "rev-parse", "HEAD") == world.head
    _assert_protected(world, kas_calls, built)


def test_clone_at_its_pin_is_frozen_and_declared_by_path_without_a_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launched: list[Any], kas_calls: KasRecorder
) -> None:
    world = _world(tmp_path)
    _checkout_pin(world, world.clone)
    before = {clone: _state(clone) for clone in world.shared}

    built = _build(world, monkeypatch, launched, kas_calls)

    assert built.rc == 0
    assert built.overlay["repos"]["meta-avocado"] == {"path": "meta-avocado", "url": None}
    assert _dependency_path(built) == str(world.clone.resolve())
    assert built.dump["local_conf_header"]["dep"] == 'DEP = "pinned"'
    assert not (world.ws / ".bakar" / "pins").exists()
    assert {clone: _state(clone) for clone in world.shared} == before
    _assert_protected(world, kas_calls, built)


def test_an_existing_side_checkout_is_reused_and_declared_without_a_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launched: list[Any], kas_calls: KasRecorder
) -> None:
    world = _world(tmp_path)
    _make_side(world)
    before = {clone: _state(clone) for clone in (*world.shared, world.side)}

    built = _build(world, monkeypatch, launched, kas_calls)

    assert built.rc == 0
    side_rel = f".bakar/pins/meta-avocado/{world.pin[:12]}"
    assert built.overlay["repos"]["meta-avocado"] == {"path": side_rel, "url": None}
    assert _dependency_path(built) == str(world.side.resolve())
    assert built.dump["local_conf_header"]["dep"] == 'DEP = "pinned"'
    assert {clone: _state(clone) for clone in (*world.shared, world.side)} == before
    _assert_protected(world, kas_calls, built)


def test_a_missing_shared_repository_is_left_out_of_the_overlay_for_kas_to_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launched: list[Any], kas_calls: KasRecorder
) -> None:
    world = _world(tmp_path)
    bitbake = world.ws / "bitbake"
    bb_pin = _rgit(bitbake, "rev-parse", "HEAD")
    shutil.rmtree(bitbake)
    others = [world.clone, world.ws / "openembedded-core"]
    before = {clone: _state(clone) for clone in others}

    built = _build(world, monkeypatch, launched, kas_calls)

    assert built.rc == 0
    # Classified missing: no overlay entry, so kas clones it at its pin from the entry's url.
    assert "bitbake" not in built.overlay["repos"]
    assert built.overlay["repos"]["openembedded-core"] == {"path": "openembedded-core", "url": None}
    assert built.overlay["repos"]["meta-avocado"] == {"path": f".bakar/pins/meta-avocado/{world.pin[:12]}"}
    assert _rgit(bitbake, "rev-parse", "HEAD") == bb_pin
    assert built.dump["local_conf_header"]["dep"] == 'DEP = "pinned"'
    assert {clone: _state(clone) for clone in others} == before
    _assert_protected(world, kas_calls, built)


def test_a_missing_dependency_clone_is_refused_before_any_build_and_says_there_is_no_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launched: list[Any], kas_calls: KasRecorder
) -> None:
    world = _world(tmp_path)
    shutil.rmtree(world.clone)
    others = [world.ws / "openembedded-core", world.ws / "bitbake"]
    before = {clone: _state(clone) for clone in others}

    # The pinned commit is read from the workspace's clone and bakar never fetches into a shared one,
    # so an absent clone stops the build, saying so, instead of cloning in its place or advising a fetch
    # that cannot run.
    with pytest.raises(RuntimeError) as refusal:
        _build(world, monkeypatch, launched, kas_calls)

    assert world.pin in str(refusal.value)
    assert f"no meta-avocado clone at {world.clone}" in str(refusal.value)
    assert "fetch" not in str(refusal.value)
    assert launched == []
    assert not world.clone.exists()
    assert {clone: _state(clone) for clone in others} == before
    assert _unprotected(kas_calls, world.ws) == []


# ---------------------------------------------------------------------------
# Delivery through the fake host transport
# ---------------------------------------------------------------------------


def test_dispatch_delivers_to_a_bare_repository_and_never_touches_the_workspaces_meta_avocado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path)
    layer = bsp_detect.layer_repo_of(world.entry)
    assert layer is not None
    assert layer.name == _LAYER_NAME
    layer_head = _rgit(world.layer, "rev-parse", "HEAD")
    before = _state(world.clone)
    rec = Recorder(monkeypatch)
    lrec = LayerRec(rec, monkeypatch)
    # The delivered commit is the layer checkout's own HEAD, not the recorder's stand-in.
    monkeypatch.setattr(layer_delivery, "snapshot_commit", lambda checkout, *, cap_mb: (layer_head, False))
    request = ad.AvocadoDispatchRequest(
        workspace=world.ws,
        checkout=world.layer,
        worktree=None,
        kas_yaml=world.entry,
        kas_arg="kas/entry.yml",
        local_args=["build", "kas/entry.yml", "--on", HOST, "-w", "/not/the/hosts/workspace"],
        release=_RELEASE,
        worktree_root=None,
        build_dir_template=None,
        cap_mb=64,
        invoking_cwd=world.layer,
        workspace_source="[workspaces] template",
    )

    rc = ad.dispatch_layer_build(
        HOST, request, layer, None, remote_dispatch.DispatchFlags(sccache_dist=False, assume_yes=False, dry_run=False)
    )

    assert rc == 0
    bare = f"{world.ws}/.bakar/layers/{_LAYER_NAME}.git"
    root = f"{world.ws}/.bakar/worktrees/{_LAYER_NAME}"
    # The recorder files every other local git call under "push" too; the real push is the one that says so.
    push = next(argv for name, argv in rec.calls if name == "push" and "push" in argv)
    # A non-forced push of the layer's commit to the bare repository, under refs/bakar/<name>/<commit12>.
    assert f"ssh://{HOST}{bare}" in push
    assert f"{layer_head}:refs/bakar/main/{layer_head[:12]}" in push
    assert not {"--force", "-f", "--delete", "--prune", "--mirror", "--force-with-lease"} & set(push)
    assert not any("meta-avocado" in arg for arg in push)
    # The bare repository is created if absent, the worktree sits under its own directory, and the
    # layout never names the workspace's meta-avocado clone.
    assert "git init -q --bare" in lrec.repo_script
    assert bare in lrec.repo_script
    ensure = rec.ensure_script
    assert f"repo_git={bare}\n" in ensure
    assert f"root={root}\n" in ensure
    assert 'git -C "$repo_git" worktree add' in ensure
    assert "meta-avocado" not in ensure
    assert f"cd {root}/main" in rec.remote_script
    assert f"--workspace {world.ws}" in rec.remote_script
    assert "/not/the/hosts/workspace" not in rec.remote_script
    assert str(world.clone) not in rec.remote_script
    # The workspace's own clone: no ref pushed, no worktree added, nothing moved.
    assert _state(world.clone) == before
    assert "refs/bakar" not in _rgit(world.clone, "for-each-ref")
    assert str(world.clone) not in " ".join(
        " ".join(map(str, a)) if isinstance(a, list) else str(a) for _, a in rec.calls
    )
