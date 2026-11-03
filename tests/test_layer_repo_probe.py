"""Executable evidence for the kas behaviours the layer-repo build relies on.

Design assumptions A2, A3 and A9 are checked against the real ``kas`` binary
on local ``file://`` git fixtures under ``tmp_path``. No kas output is
fabricated. When ``BAKAR_REQUIRE_KAS`` is set a missing kas fails the tests
instead of skipping them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}
_SKIPS = ("repos_checkout", "finish_setup_repos", "repos_apply_patches")


@pytest.fixture(autouse=True)
def _require_kas() -> None:
    if shutil.which("kas") is not None:
        return
    if os.environ.get("BAKAR_REQUIRE_KAS"):
        pytest.fail("kas is not on PATH and BAKAR_REQUIRE_KAS is set")
    pytest.skip("kas is not on PATH")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    ).stdout.strip()


@dataclass(frozen=True)
class _Dep:
    url: str
    c1: str
    c2: str


def _make_dep(base: Path) -> _Dep:
    """Fake meta-avocado: a kas tree whose base.yml differs at two commits."""
    repo = base / "dep-src"
    (repo / "kas").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    commits = []
    for value in ("one", "two"):
        (repo / "kas" / "base.yml").write_text(
            f'header:\n  version: 14\nlocal_conf_header:\n  dep: "DEP = \\"{value}\\""\n'
        )
        _git(repo, "add", "kas/base.yml")
        _git(repo, "commit", "-q", "-m", value)
        commits.append(_git(repo, "rev-parse", "HEAD"))
    return _Dep(f"file://{repo}", commits[0], commits[1])


def _export(dep_repo_url: str, commit: str, dest: Path) -> Path:
    """Read-only style export of the dependency's tree at ``commit``."""
    dest.mkdir(parents=True)
    repo = Path(dep_repo_url.removeprefix("file://"))
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", commit],
        check=True,
        capture_output=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    return dest


def _make_layer(base: Path, dep: _Dep, *, with_layers: bool) -> Path:
    """Layer repository whose entries include the dependency by repo reference.

    ``kas/entry.yml`` also declares the dependency by URL, branch and commit.
    ``kas/entry-nourl.yml`` is the same entry without that declaration, so a
    wrapper can supply the dependency at an exported path.
    """
    repo = base / ("with" if with_layers else "none") / "layer"
    (repo / "kas").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    # The entry names its own repository either way; with no ``layers`` kas
    # defaults to the repository root.
    doc: dict = {
        "header": {"version": 14, "includes": [{"repo": "meta-avocado", "file": "kas/base.yml"}]},
        "repos": {"layer": {"layers": {"meta-x": None}} if with_layers else None},
    }
    (repo / "kas" / "entry-nourl.yml").write_text(yaml.safe_dump(doc))
    doc["repos"]["meta-avocado"] = {"url": dep.url, "branch": "main", "commit": dep.c1}
    (repo / "kas" / "entry.yml").write_text(yaml.safe_dump(doc))
    _git(repo, "add", "kas")
    _git(repo, "commit", "-q", "-m", "entry")
    return repo


def _wrapper(path: Path, repos: dict, entry: Path) -> Path:
    path.write_text(yaml.safe_dump({"header": {"version": 14, "includes": [str(entry)]}, "repos": repos}))
    return path


def _dump(files: str, work: Path, *, skip: bool, cwd: Path) -> dict:
    cmd = ["kas", "dump"]
    if skip:
        for step in _SKIPS:
            cmd += ["--skip", step]
    cmd += [files]
    out = work.parent / f"{work.name}.dump.out"
    err = work.parent / f"{work.name}.dump.err"
    with out.open("wb") as o, err.open("wb") as e:
        rc = subprocess.run(
            cmd,
            stdout=o,
            stderr=e,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env={**os.environ, **_GIT_ENV, "KAS_WORK_DIR": str(work)},
            start_new_session=True,
            timeout=120,
            check=False,
        ).returncode
    assert rc == 0, f"kas dump failed rc={rc}: {err.read_text()}"
    return yaml.safe_load(out.read_text())


def _layers(doc: dict, repo: str) -> dict:
    """Layers of ``repo`` in a merged dump (kas: omitted -> repo root)."""
    entry = doc["repos"].get(repo) or {}
    return entry.get("layers", {"": None})


def test_a2_skip_dump_with_exported_dependency_clones_nothing(tmp_path: Path) -> None:
    dep = _make_dep(tmp_path)
    layer = _make_layer(tmp_path, dep, with_layers=True)
    export = _export(dep.url, dep.c1, tmp_path / "export")
    work = tmp_path / "work"
    work.mkdir()
    wrapper = _wrapper(
        tmp_path / "wrap.yml",
        {"meta-avocado": {"path": str(export)}, "layer": {"path": str(layer)}},
        layer / "kas" / "entry-nourl.yml",
    )

    doc = _dump(str(wrapper), work, skip=True, cwd=tmp_path)

    # The include from the exported dependency was read and merged.
    assert doc["local_conf_header"]["dep"] == 'DEP = "one"'
    assert doc["repos"]["meta-avocado"]["path"] == str(export)
    # Nothing was cloned: the work directory is still empty and the export
    # was not turned into a git checkout.
    assert list(work.iterdir()) == []
    assert not (export / ".git").exists()


@pytest.mark.parametrize("with_layers", [True, False])
def test_a3_path_only_wrapper_keeps_layers_of_plain_dump(tmp_path: Path, with_layers: bool) -> None:
    dep = _make_dep(tmp_path)
    layer = _make_layer(tmp_path, dep, with_layers=with_layers)
    export = _export(dep.url, dep.c1, tmp_path / "export")
    plain_work = tmp_path / "plain-work"
    plain_work.mkdir()
    work = tmp_path / "work"
    work.mkdir()

    # Plain dump of the entry: kas derives the layer repository itself.
    plain = _dump(str(layer / "kas" / "entry.yml"), plain_work, skip=False, cwd=layer)
    wrapper = _wrapper(
        tmp_path / "wrap.yml",
        {"meta-avocado": {"path": str(export)}, "layer": {"path": str(layer)}},
        layer / "kas" / "entry-nourl.yml",
    )
    wrapped = _dump(str(wrapper), work, skip=True, cwd=tmp_path)

    # Both merges must list the layer repository, otherwise the comparison
    # would pass on kas's default for an absent entry.
    assert "layer" in plain["repos"]
    assert "layer" in wrapped["repos"]
    plain_layers = _layers(plain, "layer")
    assert _layers(wrapped, "layer") == plain_layers
    assert plain_layers == ({"meta-x": None} if with_layers else {"": None})


def test_a9_non_skipping_dump_clones_absent_dependency_at_pinned_commit(tmp_path: Path) -> None:
    dep = _make_dep(tmp_path)
    layer = _make_layer(tmp_path, dep, with_layers=True)
    work = tmp_path / "work"
    work.mkdir()
    side = tmp_path / "side" / "meta-avocado"  # absent path
    assert not side.exists()
    wrapper = _wrapper(
        tmp_path / "wrap.yml",
        {
            "meta-avocado": {"path": str(side), "url": dep.url, "commit": dep.c1, "branch": "main"},
            "layer": {"path": str(layer)},
        },
        layer / "kas" / "entry.yml",
    )

    doc = _dump(str(wrapper), work, skip=False, cwd=tmp_path)

    # The dependency was cloned at the pinned commit (the upstream branch
    # head is one commit ahead) and the include was read from that checkout.
    assert _git(side, "rev-parse", "HEAD") == dep.c1
    assert dep.c1 != dep.c2
    assert doc["local_conf_header"]["dep"] == 'DEP = "one"'
