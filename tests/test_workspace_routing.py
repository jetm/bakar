"""Tests for ``[workspaces]`` release routing in ``_resolve_workspace``.

The resolver sets the process-wide ``BAKAR_REPO_POLICY`` when it routes, so
every test that can reach the routing branch isolates that variable through
``monkeypatch`` (``delenv`` or ``setenv``) to keep it from leaking.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import typer

import bakar.commands._workspace as workspace_mod
from bakar.bsp_detect import detect_kas_workspace
from bakar.commands._helpers import _INVOCATION, _bsp_from_cwd, _resolve_workspace
from bakar.commands.triage import _resolve_triage_dirs
from bakar.release_workspace import ReleaseRoutingError
from bakar.steps import kas_pins
from bakar.user_config import UserConfig

if TYPE_CHECKING:
    from tests.conftest import MetaAvocadoWorktreeLayout

pytestmark = pytest.mark.unit

_POLICY = "BAKAR_REPO_POLICY"


def _use_config(monkeypatch: pytest.MonkeyPatch, cfg: UserConfig) -> None:
    monkeypatch.setattr(workspace_mod, "load_user_config", lambda: cfg)


def _probe_returns(monkeypatch: pytest.MonkeyPatch, release: str) -> list[tuple[Path, Path]]:
    calls: list[tuple[Path, Path]] = []

    def fake_probe(kas_yaml: Path, checkout: Path) -> str:
        calls.append((kas_yaml, checkout))
        return release

    monkeypatch.setattr(kas_pins, "probe_release", fake_probe)
    return calls


def _probe_must_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_a: object, **_k: object) -> str:
        raise AssertionError("probe_release must not run without a [workspaces] mapping")

    monkeypatch.setattr(kas_pins, "probe_release", fail)


def test_mapped_release_returns_routed_path_and_sets_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout
) -> None:
    monkeypatch.delenv(_POLICY, raising=False)
    routed = tmp_path / "peridio-wrynose"
    routed.mkdir()
    _use_config(monkeypatch, UserConfig(workspaces_template=str(tmp_path / "peridio-{release}")))
    calls = _probe_returns(monkeypatch, "wrynose")
    layout = meta_avocado_worktree_layout

    result = _resolve_workspace(None, kas_yaml=layout.kas_yaml, family="generic")

    assert result == routed
    assert calls == [(layout.kas_yaml, layout.worktree.resolve())]
    assert workspace_mod.os.environ[_POLICY] == "reuse"


def test_per_release_entry_wins_over_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout
) -> None:
    monkeypatch.delenv(_POLICY, raising=False)
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    _use_config(
        monkeypatch,
        UserConfig(
            workspaces_template=str(tmp_path / "peridio-{release}"),
            workspaces_releases={"wrynose": str(explicit)},
        ),
    )
    _probe_returns(monkeypatch, "wrynose")

    result = _resolve_workspace(None, kas_yaml=meta_avocado_worktree_layout.kas_yaml, family="generic")

    assert result == explicit


def test_unmapped_main_checkout_equals_detect_kas_workspace(
    monkeypatch: pytest.MonkeyPatch, meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout
) -> None:
    monkeypatch.delenv(_POLICY, raising=False)
    _use_config(monkeypatch, UserConfig())
    _probe_must_not_run(monkeypatch)
    kas_yaml = meta_avocado_worktree_layout.main / "kas" / "machine" / "m.yml"

    result = _resolve_workspace(None, kas_yaml=kas_yaml, family="generic")

    assert result == detect_kas_workspace(kas_yaml)
    assert _POLICY not in workspace_mod.os.environ


def test_unmapped_worktree_equals_detect_kas_workspace(
    monkeypatch: pytest.MonkeyPatch, meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout
) -> None:
    monkeypatch.delenv(_POLICY, raising=False)
    _use_config(monkeypatch, UserConfig())
    _probe_must_not_run(monkeypatch)
    kas_yaml = meta_avocado_worktree_layout.kas_yaml

    result = _resolve_workspace(None, kas_yaml=kas_yaml, family="generic")

    assert result == detect_kas_workspace(kas_yaml)
    assert _POLICY not in workspace_mod.os.environ


def test_release_without_entry_falls_back_to_detect_kas_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout
) -> None:
    """A per-release table that does not name this release and no template maps nothing."""
    monkeypatch.delenv(_POLICY, raising=False)
    _use_config(monkeypatch, UserConfig(workspaces_releases={"scarthgap": str(tmp_path)}))
    _probe_returns(monkeypatch, "wrynose")
    kas_yaml = meta_avocado_worktree_layout.kas_yaml

    result = _resolve_workspace(None, kas_yaml=kas_yaml, family="generic")

    assert result == detect_kas_workspace(kas_yaml)
    assert _POLICY not in workspace_mod.os.environ


def test_missing_routed_path_exits_2_naming_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout,
) -> None:
    monkeypatch.delenv(_POLICY, raising=False)
    _use_config(monkeypatch, UserConfig(workspaces_template=str(tmp_path / "absent-{release}")))
    _probe_returns(monkeypatch, "wrynose")

    with pytest.raises(typer.Exit) as excinfo:
        _resolve_workspace(None, kas_yaml=meta_avocado_worktree_layout.kas_yaml, family="generic", strict_routing=True)

    assert excinfo.value.exit_code == 2
    err = capsys.readouterr().err.replace("\n", "")
    assert "absent-wrynose" in err
    assert "[workspaces]" in err
    assert _POLICY not in workspace_mod.os.environ


def test_missing_routed_path_falls_back_for_read_only_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout,
) -> None:
    monkeypatch.delenv(_POLICY, raising=False)
    _use_config(monkeypatch, UserConfig(workspaces_template=str(tmp_path / "absent-{release}")))
    _probe_returns(monkeypatch, "wrynose")

    ws = _resolve_workspace(
        None, kas_yaml=meta_avocado_worktree_layout.kas_yaml, family="generic", strict_routing=False
    )

    assert ws == workspace_mod.detect_kas_workspace(meta_avocado_worktree_layout.kas_yaml)
    assert "absent-wrynose" in capsys.readouterr().err.replace("\n", "")
    assert _POLICY not in workspace_mod.os.environ


def test_missing_routed_path_exits_2_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout
) -> None:
    monkeypatch.delenv(_POLICY, raising=False)
    _use_config(monkeypatch, UserConfig(workspaces_template=str(tmp_path / "absent-{release}")))
    _probe_returns(monkeypatch, "wrynose")

    with pytest.raises(typer.Exit) as excinfo:
        _resolve_workspace(None, kas_yaml=meta_avocado_worktree_layout.kas_yaml, family="generic")

    assert excinfo.value.exit_code == 2
    assert _POLICY not in workspace_mod.os.environ


def test_routing_error_falls_back_for_read_only_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout,
) -> None:
    monkeypatch.delenv(_POLICY, raising=False)
    (tmp_path / "peridio-wrynose").mkdir()
    _use_config(monkeypatch, UserConfig(workspaces_template=str(tmp_path / "peridio-{release}")))

    def boom(*_a: object, **_k: object) -> str:
        raise ReleaseRoutingError("kas not found on PATH")

    monkeypatch.setattr(kas_pins, "probe_release", boom)

    ws = _resolve_workspace(
        None, kas_yaml=meta_avocado_worktree_layout.kas_yaml, family="generic", strict_routing=False
    )

    assert ws == workspace_mod.detect_kas_workspace(meta_avocado_worktree_layout.kas_yaml)
    err = capsys.readouterr().err.replace("\n", "")
    assert "kas not found on PATH" in err
    assert "unrouted workspace" in err
    assert _POLICY not in workspace_mod.os.environ


def test_routing_error_exits_2_with_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout,
) -> None:
    monkeypatch.delenv(_POLICY, raising=False)
    _use_config(monkeypatch, UserConfig(workspaces_template=str(tmp_path / "peridio-{release}")))

    def boom(*_a: object, **_k: object) -> str:
        raise ReleaseRoutingError("the openembedded-core pin has no branch:")

    monkeypatch.setattr(kas_pins, "probe_release", boom)

    with pytest.raises(typer.Exit) as excinfo:
        _resolve_workspace(None, kas_yaml=meta_avocado_worktree_layout.kas_yaml, family="generic")

    assert excinfo.value.exit_code == 2
    assert "has no branch:" in capsys.readouterr().err.replace("\n", "")
    assert _POLICY not in workspace_mod.os.environ


def test_explicit_workspace_wins_over_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout
) -> None:
    monkeypatch.delenv(_POLICY, raising=False)
    (tmp_path / "peridio-wrynose").mkdir()
    _use_config(monkeypatch, UserConfig(workspaces_template=str(tmp_path / "peridio-{release}")))
    _probe_must_not_run(monkeypatch)
    explicit = tmp_path / "mine"

    result = _resolve_workspace(explicit, kas_yaml=meta_avocado_worktree_layout.kas_yaml, family="generic")

    assert result == explicit
    assert _POLICY not in workspace_mod.os.environ


@pytest.mark.parametrize("mapping", ["template", "releases"])
def test_explicit_release_workspace_defaults_to_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout,
    mapping: str,
) -> None:
    monkeypatch.delenv(_POLICY, raising=False)
    explicit = tmp_path / "peridio-wrynose"
    explicit.mkdir()
    if mapping == "template":
        cfg = UserConfig(workspaces_template=str(tmp_path / "peridio-{release}"))
    else:
        cfg = UserConfig(workspaces_releases={"wrynose": str(explicit)})
    _use_config(monkeypatch, cfg)
    _probe_must_not_run(monkeypatch)

    result = _resolve_workspace(explicit, kas_yaml=meta_avocado_worktree_layout.kas_yaml, family="generic")

    assert result == explicit
    assert workspace_mod.os.environ[_POLICY] == "reuse"


def test_explicit_release_workspace_keeps_explicit_kas_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout
) -> None:
    monkeypatch.setenv(_POLICY, "kas")
    explicit = tmp_path / "peridio-wrynose"
    explicit.mkdir()
    _use_config(monkeypatch, UserConfig(workspaces_template=str(tmp_path / "peridio-{release}")))
    _probe_must_not_run(monkeypatch)

    assert _resolve_workspace(explicit, kas_yaml=meta_avocado_worktree_layout.kas_yaml, family="generic") == explicit
    assert workspace_mod.os.environ[_POLICY] == "kas"


def test_explicit_workspace_with_non_meta_avocado_kas_file_is_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_POLICY, raising=False)
    explicit = tmp_path / "peridio-wrynose"
    explicit.mkdir()
    kas_yaml = tmp_path / "plain" / "kas.yml"
    kas_yaml.parent.mkdir()
    kas_yaml.write_text("header:\n  version: 16\nmachine: qemux86-64\n")
    _use_config(monkeypatch, UserConfig(workspaces_template=str(tmp_path / "peridio-{release}")))
    _probe_must_not_run(monkeypatch)

    assert _resolve_workspace(explicit, kas_yaml=kas_yaml, family="generic") == explicit
    assert _POLICY not in workspace_mod.os.environ


def test_preset_policy_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout
) -> None:
    monkeypatch.setenv(_POLICY, "kas")
    routed = tmp_path / "peridio-wrynose"
    routed.mkdir()
    _use_config(monkeypatch, UserConfig(workspaces_template=str(tmp_path / "peridio-{release}")))
    _probe_returns(monkeypatch, "wrynose")

    result = _resolve_workspace(None, kas_yaml=meta_avocado_worktree_layout.kas_yaml, family="generic")

    assert result == routed
    assert workspace_mod.os.environ[_POLICY] == "kas"


def test_bsp_from_cwd_still_reads_invoking_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout
) -> None:
    monkeypatch.delenv(_POLICY, raising=False)
    ws = tmp_path / "bsp-ws"
    (ws / "nxp").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    _INVOCATION["cwd"] = ws / "nxp"
    routed = tmp_path / "peridio-wrynose"
    routed.mkdir()
    _use_config(monkeypatch, UserConfig(workspaces_template=str(tmp_path / "peridio-{release}")))
    _probe_returns(monkeypatch, "wrynose")

    assert _resolve_workspace(None, kas_yaml=meta_avocado_worktree_layout.kas_yaml, family="generic") == routed

    assert _INVOCATION["cwd"] == ws / "nxp"
    assert Path.cwd() == elsewhere.resolve()
    assert _bsp_from_cwd(ws) == "nxp"


def test_triage_build_root_uses_routed_workspace_and_worktree_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout
) -> None:
    monkeypatch.delenv(_POLICY, raising=False)
    monkeypatch.delenv("BAKAR_WORKTREE_BUILD_DIR", raising=False)
    routed = tmp_path / "peridio-wrynose"
    routed.mkdir()
    cfg = UserConfig(workspaces_template=str(tmp_path / "peridio-{release}"))
    _use_config(monkeypatch, cfg)
    monkeypatch.setattr("bakar.commands.triage.load_user_config", lambda: cfg)
    _probe_returns(monkeypatch, "wrynose")

    runs_dirs, report_root, _label, translation = _resolve_triage_dirs(meta_avocado_worktree_layout.kas_yaml, None)

    assert report_root == routed / "build-m-wt1"
    assert runs_dirs == [(routed / "build-m-wt1" / "build" / "runs", "generic")]
    assert translation == routed


@pytest.mark.parametrize(
    ("templates", "want_dir"),
    [(("b-{worktree}", None), "b-wt1"), ((None, "env-{stem}"), "env-m"), (("b-{worktree}", "env-{stem}"), "env-m")],
)
def test_triage_uses_the_configured_worktree_build_dir_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout,
    templates: tuple[str | None, str | None],
    want_dir: str,
) -> None:
    config_template, env_template = templates
    monkeypatch.delenv(_POLICY, raising=False)
    if env_template is None:
        monkeypatch.delenv("BAKAR_WORKTREE_BUILD_DIR", raising=False)
    else:
        monkeypatch.setenv("BAKAR_WORKTREE_BUILD_DIR", env_template)
    routed = tmp_path / "peridio-wrynose"
    routed.mkdir()
    cfg = UserConfig(workspaces_template=str(tmp_path / "peridio-{release}"), build_worktree_build_dir=config_template)
    _use_config(monkeypatch, cfg)
    monkeypatch.setattr("bakar.commands.triage.load_user_config", lambda: cfg)
    _probe_returns(monkeypatch, "wrynose")

    _runs, report_root, _label, _translation = _resolve_triage_dirs(meta_avocado_worktree_layout.kas_yaml, None)

    assert report_root == routed / want_dir


def test_triage_refuses_a_build_dir_template_that_is_not_one_directory_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout,
) -> None:
    monkeypatch.delenv(_POLICY, raising=False)
    monkeypatch.setenv("BAKAR_WORKTREE_BUILD_DIR", "../{worktree}")
    _use_config(monkeypatch, UserConfig())
    monkeypatch.setattr("bakar.commands.triage.load_user_config", UserConfig)

    with pytest.raises(typer.Exit) as excinfo:
        _resolve_triage_dirs(meta_avocado_worktree_layout.kas_yaml, None)

    assert excinfo.value.exit_code == 2
    assert "single directory name" in capsys.readouterr().err.replace("\n", "")


def test_triage_build_root_unrouted_main_checkout(
    monkeypatch: pytest.MonkeyPatch, meta_avocado_worktree_layout: MetaAvocadoWorktreeLayout
) -> None:
    monkeypatch.delenv(_POLICY, raising=False)
    _use_config(monkeypatch, UserConfig())
    monkeypatch.setattr("bakar.commands.triage.load_user_config", UserConfig)
    _probe_must_not_run(monkeypatch)
    kas_yaml = (meta_avocado_worktree_layout.main / "kas" / "machine" / "m.yml").resolve()

    _runs, report_root, _label, _translation = _resolve_triage_dirs(kas_yaml, None)

    assert report_root == detect_kas_workspace(kas_yaml) / "build-m"
    assert _POLICY not in workspace_mod.os.environ
