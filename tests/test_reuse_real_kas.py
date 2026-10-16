"""Real-kas tests for the reuse policy: overlays in the classified stack, frozen paths.

Every repository is a local ``file://`` git repo under ``tmp_path``, so kas
needs no network. The shared workspace is ``ws``; the meta-avocado checkout
sits outside it, as in a routed release workspace.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from bakar.config import BuildConfig
from bakar.steps import kas_pins
from bakar.steps.kas_build import _build_kas_arg

pytestmark = pytest.mark.skipif(shutil.which("kas") is None, reason="kas is not on PATH")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    ).stdout.strip()


@dataclass(frozen=True)
class _Upstream:
    url: str
    c1: str
    c2: str


def _upstream(base: Path, name: str) -> _Upstream:
    """Create a bare upstream with two commits; return its url and both SHAs."""
    seed = base / "seed" / name
    seed.mkdir(parents=True)
    _git(seed, "init", "-q", "-b", "main")
    (seed / "f").write_text("one\n")
    _git(seed, "add", "f")
    _git(seed, "commit", "-q", "-m", "one")
    (seed / "f").write_text("two\n")
    _git(seed, "commit", "-q", "-am", "two")
    bare = base / "up" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(base, "clone", "-q", "--bare", str(seed), str(bare))
    return _Upstream(f"file://{bare}", _git(bare, "rev-parse", "main~1"), _git(bare, "rev-parse", "main"))


def _shared_clone(ws: Path, name: str, up: _Upstream, commit: str) -> Path:
    clone = ws / name
    _git(ws, "clone", "-q", up.url.removeprefix("file://"), str(clone))
    _git(clone, "checkout", "-q", "--detach", commit)
    return clone


def _repo_entry(up: _Upstream, commit: str, *, path: str | None = None) -> dict:
    entry: dict = {"url": up.url, "commit": commit, "layers": {".": None}}
    if path is not None:
        entry["path"] = path
    return entry


def _write(path: Path, doc: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"header": {"version": 16}, **doc}, sort_keys=False))
    return path


def _checkout(base: Path, entry_repos: dict, overlay_repos: dict | None = None) -> tuple[Path, Path | None]:
    """Commit a meta-avocado checkout holding the entry and, optionally, a feature overlay."""
    checkout = base / "src" / "meta-avocado"
    checkout.mkdir(parents=True)
    _git(checkout, "init", "-q", "-b", "main")
    repos = {"meta-avocado": {"path": "meta-avocado", "layers": {"meta-x": None}}, **entry_repos}
    entry = _write(checkout / "kas" / "machine" / "m.yml", {"repos": repos})
    feature = None
    if overlay_repos is not None:
        feature = _write(checkout / "kas" / "feature" / "f.yml", {"repos": overlay_repos})
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "kas")
    return entry, feature


def _cfg(ws: Path, entry: Path) -> BuildConfig:
    cfg = BuildConfig(
        workspace=ws,
        bsp_family="generic",  # type: ignore[arg-type]
        machine="generic",
        distro="generic",
        image="generic",
        manifest="",
        repo_url="",
        repo_branch="",
        kas_container_image="jetm/kas-build-env:latest",
        kas_yaml_override=entry,
    )
    cfg.bsp_root.mkdir(parents=True, exist_ok=True)
    return cfg


def _build(tmp_path: Path, ws: Path, entry: Path, extra: list[Path]) -> dict:
    main_overlay = _write(tmp_path / "overlay-src" / "main.yml", {})
    cfg = _cfg(ws, entry)
    dump = Path(_build_kas_arg(cfg, entry, main_overlay, extra))
    return yaml.safe_load(dump.read_text(encoding="utf-8"))


@pytest.fixture
def reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")


def test_overlay_added_repo_off_pin_gets_a_side_checkout(tmp_path: Path, reuse: None) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    foo = _upstream(tmp_path, "meta-foo")
    shared = _shared_clone(ws, "meta-foo", foo, foo.c1)
    entry, feature = _checkout(tmp_path, {}, {"meta-foo": _repo_entry(foo, foo.c2, path="meta-foo")})
    assert feature is not None

    dumped = _build(tmp_path, ws, entry, [feature])

    assert _git(shared, "rev-parse", "HEAD") == foo.c1
    side = ws / ".bakar" / "pins" / "meta-foo" / foo.c2[:12]
    assert _git(side, "rev-parse", "HEAD") == foo.c2
    assert dumped["repos"]["meta-foo"]["path"] == side.relative_to(ws).as_posix()
    assert dumped["repos"]["meta-foo"]["commit"] == foo.c2


def test_overlay_added_repo_at_pin_is_frozen_in_place(tmp_path: Path, reuse: None) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    foo = _upstream(tmp_path, "meta-foo")
    shared = _shared_clone(ws, "meta-foo", foo, foo.c1)
    entry, feature = _checkout(tmp_path, {}, {"meta-foo": _repo_entry(foo, foo.c1, path="meta-foo")})
    assert feature is not None

    dumped = _build(tmp_path, ws, entry, [feature])

    assert _git(shared, "rev-parse", "HEAD") == foo.c1
    assert not (ws / ".bakar" / "pins").exists()
    assert dumped["repos"]["meta-foo"]["path"] == shared.relative_to(ws).as_posix()


def test_overlay_repin_wins_and_leaves_the_shared_clone_alone(tmp_path: Path, reuse: None) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    bar = _upstream(tmp_path, "meta-bar")
    shared = _shared_clone(ws, "meta-bar", bar, bar.c1)
    entry, feature = _checkout(
        tmp_path,
        {"meta-bar": _repo_entry(bar, bar.c1, path="meta-bar")},
        {"meta-bar": {"commit": bar.c2}},
    )
    assert feature is not None

    dumped = _build(tmp_path, ws, entry, [feature])

    # The entry alone would freeze the shared clone at c1 and build that; the
    # overlay's c2 must be what the build gets, from a side checkout.
    assert dumped["repos"]["meta-bar"]["commit"] == bar.c2
    side = ws / ".bakar" / "pins" / "meta-bar" / bar.c2[:12]
    assert dumped["repos"]["meta-bar"]["path"] == side.relative_to(ws).as_posix()
    assert _git(side, "rev-parse", "HEAD") == bar.c2
    assert _git(shared, "rev-parse", "HEAD") == bar.c1


def _resolved_repos(cfg: BuildConfig, dump: Path, work_dir: Path) -> dict[str, str]:
    """Return ``{name: path}`` as ``kas for-all-repos`` resolves the dump's repositories."""
    out = subprocess.run(
        [
            "kas",
            "for-all-repos",
            "--skip",
            "write_bbconfig",
            "--skip",
            "setup_environ",
            dump.name,
            "echo RESOLVED $KAS_REPO_NAME $KAS_REPO_PATH",
        ],
        cwd=cfg.bsp_root,
        env={**os.environ, "KAS_WORK_DIR": str(work_dir)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return dict(line.split()[1:3] for line in out.stdout.splitlines() if line.startswith("RESOLVED"))


def test_frozen_repo_without_path_is_used_in_place(tmp_path: Path, reuse: None) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    baz = _upstream(tmp_path, "meta-baz")
    shared = _shared_clone(ws, "meta-baz", baz, baz.c2)
    entry, _ = _checkout(tmp_path, {"meta-baz": _repo_entry(baz, baz.c2)})
    cfg = _cfg(ws, entry)
    main_overlay = _write(tmp_path / "overlay-src" / "main.yml", {})

    dump = Path(_build_kas_arg(cfg, entry, main_overlay))

    # Relative, so kas-container (KAS_WORK_DIR=/work) finds the same clone.
    assert yaml.safe_load(dump.read_text(encoding="utf-8"))["repos"]["meta-baz"]["path"] == "meta-baz"
    assert _resolved_repos(cfg, dump, ws)["meta-baz"] == str(shared)
    assert _git(shared, "rev-parse", "HEAD") == baz.c2
    assert not (cfg.bsp_root / "meta-baz").exists()


def test_frozen_relative_path_follows_the_work_dir(tmp_path: Path, reuse: None) -> None:
    """A second name for the workspace stands in for the container's ``/work`` mount."""
    ws = tmp_path / "ws"
    ws.mkdir()
    baz = _upstream(tmp_path, "meta-baz")
    _shared_clone(ws, "meta-baz", baz, baz.c2)
    entry, _ = _checkout(tmp_path, {"meta-baz": _repo_entry(baz, baz.c2)})
    cfg = _cfg(ws, entry)
    dump = Path(_build_kas_arg(cfg, entry, _write(tmp_path / "overlay-src" / "main.yml", {})))
    work = tmp_path / "work"
    work.symlink_to(ws)

    # A host-absolute path would still print the ws path here.
    assert _resolved_repos(cfg, dump, work)["meta-baz"] == str(work / "meta-baz")


# ---------------------------------------------------------------------------
# String includes in overlays resolve as in the build's own dump
# ---------------------------------------------------------------------------


def _probe_leftovers(cfg: BuildConfig) -> list[Path]:
    return sorted(cfg.bsp_root.glob(".reuse-probe-*"))


def _git_workspace(tmp_path: Path) -> Path:
    """A workspace that is itself a git repository, holding meta-avocado as a nested clone."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q", "-b", "main")
    return ws


def test_repo_qualified_string_include_in_an_overlay_classifies(tmp_path: Path, reuse: None) -> None:
    ws = _git_workspace(tmp_path)
    foo = _upstream(tmp_path, "meta-foo")
    shared = _shared_clone(ws, "meta-foo", foo, foo.c1)
    entry, feature = _checkout(ws, {}, {"meta-foo": _repo_entry(foo, foo.c1, path="meta-foo")})
    assert feature is not None
    # _checkout puts meta-avocado at ws/src/meta-avocado; name the feature from the git root.
    rel = feature.relative_to(ws).as_posix()
    user = _write(tmp_path / "user" / "inc.yml", {"header": {"version": 16, "includes": [rel]}})

    dumped = _build(tmp_path, ws, entry, [user])

    # Without the include the classify stack would not know meta-foo at all.
    assert dumped["repos"]["meta-foo"]["path"] == "meta-foo"
    assert dumped["repos"]["meta-foo"]["commit"] == foo.c1
    assert _git(shared, "rev-parse", "HEAD") == foo.c1
    assert _probe_leftovers(_cfg(ws, entry)) == []


def test_sibling_file_include_in_an_overlay_classifies(tmp_path: Path, reuse: None) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    foo = _upstream(tmp_path, "meta-foo")
    _shared_clone(ws, "meta-foo", foo, foo.c1)
    entry, _ = _checkout(tmp_path, {})
    sib = _write(tmp_path / "user" / "sib.yml", {"repos": {"meta-foo": _repo_entry(foo, foo.c1, path="meta-foo")}})
    user = _write(tmp_path / "user" / "top.yml", {"header": {"version": 16, "includes": ["sib.yml"]}})

    # Both are materialised into .bakar/overlays, where top.yml finds sib.yml beside it.
    dumped = _build(tmp_path, ws, entry, [sib, user])

    assert dumped["repos"]["meta-foo"]["path"] == "meta-foo"
    assert dumped["repos"]["meta-foo"]["commit"] == foo.c1
    assert _probe_leftovers(_cfg(ws, entry)) == []


def test_bsp_root_relative_include_outside_git_classifies(tmp_path: Path, reuse: None) -> None:
    """Outside git kas roots string includes at the first file's directory: the probe must share it."""
    ws = tmp_path / "ws"
    ws.mkdir()
    foo = _upstream(tmp_path, "meta-foo")
    _shared_clone(ws, "meta-foo", foo, foo.c1)
    entry, _ = _checkout(tmp_path, {})
    cfg = _cfg(ws, entry)
    _write(cfg.bsp_root / "local-repos.yml", {"repos": {"meta-foo": _repo_entry(foo, foo.c1, path="meta-foo")}})
    user = _write(tmp_path / "user" / "top.yml", {"header": {"version": 16, "includes": ["local-repos.yml"]}})

    dumped = _build(tmp_path, ws, entry, [user])

    assert dumped["repos"]["meta-foo"]["path"] == "meta-foo"
    assert _probe_leftovers(cfg) == []


def test_classification_matches_the_build_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe's merge equals the build's own dump of the same stack under the kas policy."""
    ws = _git_workspace(tmp_path)
    foo = _upstream(tmp_path, "meta-foo")
    _shared_clone(ws, "meta-foo", foo, foo.c1)
    entry, feature = _checkout(ws, {}, {"meta-foo": _repo_entry(foo, foo.c1, path="meta-foo")})
    assert feature is not None
    inc = _write(
        tmp_path / "user" / "inc.yml",
        {"header": {"version": 16, "includes": [feature.relative_to(ws).as_posix()]}, "machine": "probe-m"},
    )
    monkeypatch.setenv("BAKAR_REPO_POLICY", "kas")
    cfg = _cfg(ws, entry)
    main_overlay = _write(tmp_path / "overlay-src" / "main.yml", {})
    real = yaml.safe_load(Path(_build_kas_arg(cfg, entry, main_overlay, [inc])).read_text(encoding="utf-8"))
    overlays = [cfg.bsp_root / ".bakar" / "overlays" / name for name in ("main.yml", "inc.yml")]

    merged = kas_pins.read_merged_config(
        entry, checkout=entry.parents[2], extra_files=overlays, wrap_parent=cfg.bsp_root
    )

    assert merged["machine"] == real["machine"] == "probe-m"
    assert merged["repos"]["meta-foo"] == real["repos"]["meta-foo"]
    assert set(merged["repos"]) == set(real["repos"])
    assert _probe_leftovers(cfg) == []


def test_failed_classification_leaves_no_probe_behind(tmp_path: Path, reuse: None) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    entry, _ = _checkout(tmp_path, {})
    user = _write(tmp_path / "user" / "top.yml", {"header": {"version": 16, "includes": ["absent.yml"]}})

    with pytest.raises(RuntimeError, match=r"absent\.yml"):
        _build(tmp_path, ws, entry, [user])

    assert _probe_leftovers(_cfg(ws, entry)) == []
