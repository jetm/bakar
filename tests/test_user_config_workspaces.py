"""[workspaces], [dispatch], and [build] worktree_build_dir config keys."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.user_config import (
    SETTINGS_SCHEMA,
    get_setting,
    load_user_config,
    set_setting,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text("config_version = 3\n" + body)
    return path


def test_defaults_when_absent(tmp_path: Path) -> None:
    cfg = load_user_config(_write(tmp_path, ""))
    assert cfg.workspaces_template is None
    assert cfg.workspaces_releases is None
    assert cfg.dispatch_worktree_root is None
    assert cfg.dispatch_snapshot_untracked_cap_mb == 50
    assert cfg.build_worktree_build_dir is None


def test_all_keys_parse(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
[workspaces]
template = "/ws/{release}"
unknown_key = "ignored"

[workspaces.releases]
wrynose = "/ws/custom-wrynose"

[dispatch]
worktree_root = "/wt"
snapshot_untracked_cap_mb = 120

[build]
worktree_build_dir = "build-{stem}-{worktree}"
""",
    )
    with pytest.warns(UserWarning, match=r"unknown key 'unknown_key' in \[workspaces\]"):
        cfg = load_user_config(path)
    assert cfg.workspaces_template == "/ws/{release}"
    assert cfg.workspaces_releases == {"wrynose": "/ws/custom-wrynose"}
    assert cfg.dispatch_worktree_root == "/wt"
    assert cfg.dispatch_snapshot_untracked_cap_mb == 120
    assert cfg.build_worktree_build_dir == "build-{stem}-{worktree}"


def test_template_without_placeholder_is_accepted(tmp_path: Path) -> None:
    cfg = load_user_config(_write(tmp_path, '[workspaces]\ntemplate = "/ws/fixed"\n'))
    assert cfg.workspaces_template == "/ws/fixed"


def test_workspaces_template_rejects_unknown_placeholder(tmp_path: Path) -> None:
    path = _write(tmp_path, '[workspaces]\ntemplate = "/x/{machine}"\n')
    with pytest.raises(ValueError, match=r"workspaces\.template"):
        load_user_config(path)


def test_workspaces_template_rejects_malformed_braces(tmp_path: Path) -> None:
    path = _write(tmp_path, '[workspaces]\ntemplate = "/x/{release"\n')
    with pytest.raises(ValueError, match=r"workspaces\.template"):
        load_user_config(path)


def test_workspaces_template_must_be_string(tmp_path: Path) -> None:
    path = _write(tmp_path, "[workspaces]\ntemplate = 3\n")
    with pytest.raises(ValueError, match="workspaces_template"):
        load_user_config(path)


def test_build_dir_rejects_unknown_placeholder(tmp_path: Path) -> None:
    path = _write(tmp_path, '[build]\nworktree_build_dir = "build-{machine}"\n')
    with pytest.raises(ValueError, match=r"build\.worktree_build_dir"):
        load_user_config(path)


def test_build_dir_rejects_release_placeholder(tmp_path: Path) -> None:
    path = _write(tmp_path, '[build]\nworktree_build_dir = "build-{release}"\n')
    with pytest.raises(ValueError, match=r"build\.worktree_build_dir"):
        load_user_config(path)


@pytest.mark.parametrize(
    "body",
    [
        "[workspaces.releases]\nwrynose = 3\n",
        '[workspaces]\nreleases = "nope"\n',
    ],
)
def test_releases_must_be_string_to_string(tmp_path: Path, body: str) -> None:
    with pytest.raises(ValueError, match=r"workspaces\.releases"):
        load_user_config(_write(tmp_path, body))


@pytest.mark.parametrize("value", ["0", "-5", '"10"', "true"])
def test_snapshot_cap_must_be_positive_integer(tmp_path: Path, value: str) -> None:
    path = _write(tmp_path, f"[dispatch]\nsnapshot_untracked_cap_mb = {value}\n")
    with pytest.raises(ValueError, match="snapshot_untracked_cap_mb"):
        load_user_config(path)


def test_settings_schema_recognises_new_keys() -> None:
    for key in (
        "workspaces.template",
        "dispatch.worktree_root",
        "dispatch.snapshot_untracked_cap_mb",
        "build.worktree_build_dir",
    ):
        assert key in SETTINGS_SCHEMA
    assert SETTINGS_SCHEMA["dispatch.snapshot_untracked_cap_mb"].is_int


def test_set_setting_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    set_setting("dispatch.worktree_root", "/x", path)
    set_setting("dispatch.snapshot_untracked_cap_mb", "75", path)
    set_setting("workspaces.template", "/ws/{release}", path)
    set_setting("build.worktree_build_dir", "build-{stem}", path)
    assert get_setting("dispatch.worktree_root", path) == "/x"
    assert get_setting("dispatch.snapshot_untracked_cap_mb", path) == 75
    cfg = load_user_config(path)
    assert cfg.dispatch_worktree_root == "/x"
    assert cfg.dispatch_snapshot_untracked_cap_mb == 75
    assert cfg.workspaces_template == "/ws/{release}"
    assert cfg.build_worktree_build_dir == "build-{stem}"


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("dispatch.snapshot_untracked_cap_mb", "0", "snapshot_untracked_cap_mb"),
        ("workspaces.template", "/x/{machine}", r"workspaces\.template"),
        ("build.worktree_build_dir", "build-{machine}", r"build\.worktree_build_dir"),
    ],
)
def test_set_setting_validates(tmp_path: Path, key: str, value: str, match: str) -> None:
    path = tmp_path / "config.toml"
    with pytest.raises(ValueError, match=match):
        set_setting(key, value, path)
    assert not path.exists()


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('[workspaces]\ntemplate = "peridio-{release}"\n', r"workspaces\.template.*absolute"),
        ('[workspaces.releases]\nwrynose = ""\n', r"workspaces\.releases\.wrynose.*absolute"),
        ('[workspaces.releases]\nwrynose = "ws/wrynose"\n', r"workspaces\.releases\.wrynose.*absolute"),
        ('[dispatch]\nworktree_root = ".bakar/worktrees"\n', r"dispatch\.worktree_root.*absolute"),
        ('[dispatch]\nworktree_root = "~/wt"\n', r"dispatch\.worktree_root.*absolute"),
        ('[build]\nworktree_build_dir = "build-{stem:>8}"\n', r"build\.worktree_build_dir.*format spec"),
        ('[build]\nworktree_build_dir = "build-{stem"\n', r"build\.worktree_build_dir"),
        ('[workspaces]\ntemplate = "/w/{release!r}"\n', r"workspaces\.template.*format spec"),
    ],
)
def test_loader_refuses_paths_and_templates_the_runtime_cannot_use(tmp_path: Path, body: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        load_user_config(_write(tmp_path, body))


def test_loader_accepts_a_tilde_workspace_but_not_a_tilde_remote_root(tmp_path: Path) -> None:
    cfg = load_user_config(_write(tmp_path, '[workspaces]\ntemplate = "~/work/peridio-{release}"\n'))
    assert cfg.workspaces_template == "~/work/peridio-{release}"


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("dispatch.worktree_root", "relative/dir", "absolute"),
        ("dispatch.worktree_root", "~/wt", "absolute"),
        ("workspaces.template", "peridio-{release}", "absolute"),
        ("build.worktree_build_dir", "build-{stem:>8}", "format spec"),
    ],
)
def test_set_setting_refuses_unusable_paths_and_templates(tmp_path: Path, key: str, value: str, match: str) -> None:
    path = tmp_path / "config.toml"
    with pytest.raises(ValueError, match=match):
        set_setting(key, value, path)
    assert not path.exists()
