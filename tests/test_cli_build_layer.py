"""``bakar build <layer entry> -w <release workspace>`` through the real CLI.

A layer repository's kas entry pins meta-avocado by commit. Built with ``-w``
naming a configured release workspace, the build must resolve to that
workspace, take the reuse policy, keep its run records in the layer build
directory, and leave the workspace's shared clones exactly as they were. Only
the final ``kas build`` launch is stubbed: the dump and the classification run
for real against the real ``kas`` binary on ``file://`` git fixtures. When
``BAKAR_REQUIRE_KAS`` is set a missing kas fails the tests instead of skipping
them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
import yaml

from bakar.cli import main
from bakar.config import meta_avocado_build_dir_name
from bakar.steps import kas_build
from bakar.user_config import UserConfig

if TYPE_CHECKING:
    from pathlib import Path

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}
_POLICY = "BAKAR_REPO_POLICY"


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


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _init(repo: Path, branch: str = "main") -> Path:
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", branch)
    return repo


def _base(value: str, oe: tuple[Path, str], bitbake: tuple[Path, str]) -> str:
    return yaml.safe_dump(
        {
            "header": {"version": 14},
            "distro": "poky",
            "local_conf_header": {"dep": f'DEP = "{value}"'},
            "repos": {
                "openembedded-core": {"url": f"file://{oe[0]}", "commit": oe[1], "layers": {"meta": None}},
                "bitbake": {"url": f"file://{bitbake[0]}", "commit": bitbake[1], "layers": {"bitbake": "excluded"}},
            },
        }
    )


@dataclass(frozen=True)
class _Fixture:
    ws: Path
    layer: Path
    entry: Path
    pin: str
    head: str

    @property
    def clones(self) -> list[Path]:
        return [self.ws / "meta-avocado", self.ws / "openembedded-core", self.ws / "bitbake"]


def _fixture(tmp_path: Path) -> _Fixture:
    """A release workspace whose meta-avocado clone sits one commit past the entry's pin."""
    ws = tmp_path / "ws"
    oe = _init(ws / "openembedded-core")
    oe_pin = _commit(oe, {"meta/conf/layer.conf": ""}, "oe")
    bitbake = _init(ws / "bitbake")
    bb_pin = _commit(bitbake, {"bin/bitbake": ""}, "bitbake")
    ma = _init(ws / "meta-avocado", "wrynose")
    pin = _commit(ma, {"kas/base.yml": _base("pinned", (oe, oe_pin), (bitbake, bb_pin))}, "pinned")
    head = _commit(ma, {"kas/base.yml": _base("head", (oe, oe_pin), (bitbake, bb_pin))}, "head")
    assert pin != head

    layer = _init(tmp_path / "layers" / "meta-board")
    entry_doc = {
        "header": {"version": 16, "includes": [{"repo": "meta-avocado", "file": "kas/base.yml"}]},
        "machine": "qemux86-64",
        "target": "core-image-minimal",
        "repos": {
            "meta-board": {"path": "."},
            "meta-avocado": {"url": f"file://{ma}", "branch": "wrynose", "commit": pin},
        },
    }
    _commit(layer, {"kas/entry.yml": yaml.safe_dump(entry_doc)}, "entry")
    return _Fixture(ws=ws, layer=layer, entry=layer / "kas" / "entry.yml", pin=pin, head=head)


def _snapshot(repo: Path) -> tuple[str, str, str]:
    """HEAD, working-tree status and the content of every tracked file."""
    return (
        _git(repo, "rev-parse", "HEAD"),
        _git(repo, "status", "--porcelain", "--untracked-files=all", "--ignored"),
        _git(repo, "ls-files", "-s"),
    )


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[kas_build._PtyCtx]:
    """Run the CLI on a host build with the final ``kas build`` launch recorded instead of run."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COLUMNS", "500")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("BAKAR_CONTAINER", raising=False)
    monkeypatch.delenv("BAKAR_WORKTREE_BUILD_DIR", raising=False)
    monkeypatch.setenv("BAKAR_HOST_MODE", "1")
    # The CLI sets the policy through os.environ; registering it here restores it afterwards.
    monkeypatch.setenv(_POLICY, "placeholder")
    monkeypatch.delenv(_POLICY)
    for name, value in _GIT_ENV.items():
        monkeypatch.setenv(name, value)

    seen: list[kas_build._PtyCtx] = []

    def fake_pty(ctx: kas_build._PtyCtx) -> kas_build._PtyOutcome:
        seen.append(ctx)
        return kas_build._PtyOutcome(rc=0)

    monkeypatch.setattr(kas_build, "_run_pty_with_ui", fake_pty)
    monkeypatch.setattr(kas_build, "_provision_buildtools", lambda cfg, passthrough: None)
    monkeypatch.setattr(kas_build, "_capture_dependency_graph", lambda ctx, log: None)
    monkeypatch.setattr("bakar.commands._helpers.run_all", lambda *a, **k: [])
    return seen


def _map_release(monkeypatch: pytest.MonkeyPatch, release: str, workspace: Path) -> None:
    cfg = UserConfig(workspaces_releases={release: str(workspace)})
    monkeypatch.setattr("bakar.commands._workspace.load_user_config", lambda: cfg)


def _run_cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["bakar", "build", *args])
    return main()


def test_layer_entry_builds_in_the_given_release_workspace_without_touching_its_clones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launched: list[kas_build._PtyCtx]
) -> None:
    fx = _fixture(tmp_path)
    _map_release(monkeypatch, "wrynose", fx.ws)
    before = {clone: _snapshot(clone) for clone in fx.clones}
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    rc = _run_cli(monkeypatch, str(fx.entry), "-w", str(fx.ws))

    assert rc == 0
    assert len(launched) == 1
    cfg = launched[0].cfg
    # The workspace is the one given, not the directory holding the kas file.
    assert cfg.workspace == fx.ws.resolve()
    assert os.environ[_POLICY] == "reuse"
    build_dir = fx.ws / meta_avocado_build_dir_name(fx.entry, None)
    assert "meta-board" in build_dir.name
    assert cfg.bsp_root == build_dir
    # The layer wrapper, the reuse overlay and the pin were written, and the
    # dump merged the dependency from the pinned commit, not the clone's HEAD.
    assert (build_dir / "avocado-wrapper.yml").is_file()
    assert (build_dir / "avocado-reuse.yml").is_file()
    assert (build_dir / "avocado-pin.yml").is_file()
    dump = yaml.safe_load((build_dir / "avocado-bakar.yml").read_text(encoding="utf-8"))
    assert dump["local_conf_header"]["dep"] == 'DEP = "pinned"'
    assert launched[0].cmd[launched[0].cmd.index("build") + 1] == str(build_dir / "avocado-bakar.yml")
    # Run records sit in the layer build directory and nowhere else.
    events = sorted(fx.ws.rglob("events.jsonl"))
    assert len(events) == 1, events
    assert build_dir in events[0].parents
    assert not list(fx.entry.parent.rglob("events.jsonl"))
    # No shared clone moved or changed.
    assert {clone: _snapshot(clone) for clone in fx.clones} == before
    assert _git(fx.ws / "meta-avocado", "rev-parse", "HEAD") == fx.head


def test_workspace_option_is_accepted_for_a_generic_file_outside_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launched: list[kas_build._PtyCtx]
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    generic = other / "generic.yml"
    generic.write_text("header:\n  version: 14\nmachine: qemux86-64\ndistro: poky\n", encoding="utf-8")
    monkeypatch.chdir(other)

    rc = _run_cli(monkeypatch, str(generic), "-w", str(ws))

    assert rc == 0
    assert len(launched) == 1
    assert launched[0].cfg.workspace == ws.resolve()
    assert launched[0].cfg.kas_yaml == generic.resolve()


@pytest.mark.parametrize("policy", ["kas", None])
def test_layer_entry_under_the_kas_policy_builds_in_the_kas_files_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launched: list[kas_build._PtyCtx], policy: str | None
) -> None:
    fx = _fixture(tmp_path)
    if policy is not None:
        monkeypatch.setenv(_POLICY, policy)
    # No release mapping, so routing does not set the reuse policy.
    monkeypatch.setattr("bakar.commands._workspace.load_user_config", UserConfig)
    monkeypatch.chdir(fx.layer)

    rc = _run_cli(monkeypatch, str(fx.entry))

    # Before the fix this raised "kas YAML ... is outside bsp_root" ahead of kas.
    assert rc == 0
    assert len(launched) == 1
    cfg = launched[0].cfg
    assert cfg.bsp_root == fx.entry.parent.resolve()
    assert cfg.bsp_root != fx.ws / meta_avocado_build_dir_name(fx.entry, None)
