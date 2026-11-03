"""Per-worktree build directories for meta-avocado kas files (``BuildConfig.bsp_root``)."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from bakar.config import ResolveRequest, meta_avocado_build_dir_name, resolve, worktree_build_template
from bakar.user_config import UserConfig
from bakar.workspace_config import WorkspaceConfig

if TYPE_CHECKING:
    from pathlib import Path

    from bakar.config import BuildConfig
    from tests.conftest import MetaAvocadoWorktreeLayout

_ENV = "BAKAR_WORKTREE_BUILD_DIR"


def _resolve(ws: Path, kas_yaml: Path, user_config: UserConfig | None = None) -> BuildConfig:
    return resolve(
        ResolveRequest(
            workspace=ws,
            bsp_family="generic",
            kas_yaml=kas_yaml,
            user_config=user_config if user_config is not None else UserConfig(),
            workspace_config=WorkspaceConfig(),
        )
    )


def _add_worktree(main: Path, path: Path, branch: str) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", str(path), "-b", branch],
        check=True,
        capture_output=True,
        env=env,
    )


@pytest.mark.unit
def test_worktree_default_gets_suffixed_build_dir(
    meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    layout = meta_avocado_worktree_layout
    cfg = _resolve(layout.ws, layout.kas_yaml)
    assert cfg.bsp_root == layout.ws / "build-m-wt1"


@pytest.mark.unit
def test_env_template_shares_plain_build_dir(
    meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "build-{stem}")
    layout = meta_avocado_worktree_layout
    cfg = _resolve(layout.ws, layout.kas_yaml)
    assert cfg.bsp_root == layout.ws / "build-m"


@pytest.mark.unit
def test_user_config_template_applies(
    meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    layout = meta_avocado_worktree_layout
    cfg = _resolve(layout.ws, layout.kas_yaml, UserConfig(build_worktree_build_dir="b-{worktree}-{stem}"))
    assert cfg.bsp_root == layout.ws / "b-wt1-m"


@pytest.mark.unit
def test_env_template_beats_user_config(
    meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "env-{worktree}")
    layout = meta_avocado_worktree_layout
    user = UserConfig(build_worktree_build_dir="user-{worktree}")
    assert worktree_build_template(user) == "env-{worktree}"
    cfg = _resolve(layout.ws, layout.kas_yaml, user)
    assert cfg.bsp_root == layout.ws / "env-wt1"


@pytest.mark.unit
def test_template_helper_is_none_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    assert worktree_build_template(None) is None
    assert worktree_build_template(UserConfig()) is None


@pytest.mark.unit
def test_main_checkout_keeps_plain_build_dir(
    meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "build-{stem}-{worktree}-x")
    layout = meta_avocado_worktree_layout
    cfg = _resolve(layout.ws, layout.main / "kas" / "machine" / "m.yml")
    assert cfg.bsp_root == layout.ws / "build-m"


@pytest.mark.unit
def test_two_worktrees_get_distinct_roots_and_tmpdirs(
    meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    layout = meta_avocado_worktree_layout
    second = layout.worktree.parent / "wt2"
    _add_worktree(layout.main, second, "wt2")
    user = UserConfig(local_tmpdir_base=str(tmp_path / "local-tmp"))

    cfg1 = _resolve(layout.ws, layout.kas_yaml, user)
    cfg2 = _resolve(layout.ws, second / "kas" / "machine" / "m.yml", user)

    assert cfg1.host_mode and cfg2.host_mode
    assert cfg1.bsp_root == layout.ws / "build-m-wt1"
    assert cfg2.bsp_root == layout.ws / "build-m-wt2"
    tmp1, tmp2 = cfg1.resolved_tmpdir, cfg2.resolved_tmpdir
    assert tmp1.parent == tmp2.parent == tmp_path / "local-tmp"
    assert tmp1 != tmp2
    assert tmp1.name.rsplit("-", 1)[1] != tmp2.name.rsplit("-", 1)[1]


def _make_layer_repo(root: Path, name: str = "meta-acme") -> Path:
    """A git repo holding one layer entry: the layer repo (no url) plus a pinned meta-avocado."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    ident = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    (root / "kas").mkdir(parents=True)
    (root / "kas" / "m.yml").write_text(
        "header:\n  version: 16\nrepos:\n"
        f"  {name}:\n    path: .\n"
        "  meta-avocado:\n    url: https://example.invalid/meta-avocado.git\n    branch: main\n"
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", *ident, "-C", str(root), "commit", "-q", "-m", "i"], check=True, capture_output=True, env=env
    )
    return root / "kas" / "m.yml"


@pytest.mark.unit
def test_layer_main_checkout_names_dir_after_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    entry = _make_layer_repo(tmp_path / "checkout")
    assert meta_avocado_build_dir_name(entry, None) == "build-m-meta-acme"


@pytest.mark.unit
def test_layer_worktrees_get_distinct_dirs_and_bsp_root_agrees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    main_entry = _make_layer_repo(tmp_path / "main")
    main = main_entry.parent.parent
    wt1, wt2 = tmp_path / "wt1", tmp_path / "wt2"
    _add_worktree(main, wt1, "b1")
    _add_worktree(main, wt2, "b2")
    ws = tmp_path / "ws"
    ws.mkdir()
    names = {meta_avocado_build_dir_name(p / "kas" / "m.yml", None) for p in (main, wt1, wt2)}
    assert names == {"build-m-meta-acme", "build-m-meta-acme-wt1", "build-m-meta-acme-wt2"}
    # Without the reuse policy a layer entry builds beside its own kas file.
    assert _resolve(ws, wt1 / "kas" / "m.yml").bsp_root == (wt1 / "kas").resolve()
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg = _resolve(ws, wt1 / "kas" / "m.yml")
    assert cfg.bsp_root == ws / "build-m-meta-acme-wt1"


@pytest.mark.unit
def test_layer_stem_never_collides_with_meta_avocado_machine_file(
    meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    layer_entry = _make_layer_repo(tmp_path / "layer")
    meta_main = meta_avocado_worktree_layout.main / "kas" / "machine" / "m.yml"
    assert meta_avocado_build_dir_name(layer_entry, None) != meta_avocado_build_dir_name(meta_main, None)
    assert meta_avocado_build_dir_name(meta_main, None) == "build-m"
