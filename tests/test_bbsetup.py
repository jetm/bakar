"""Unit tests for the bbsetup translation in :mod:`bspctl.kas`.

Covers :func:`translate_bbsetup_config` and :func:`write_bbsetup_yaml` against
the committed fixture ``examples/bbsetup-oe-nodistro-wrynose``. The override,
SHA-fallback, and error cases build a modified workspace under ``tmp_path`` (via
:func:`_copy_fixture`) so the committed fixture is never mutated.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from bspctl.kas import translate_bbsetup_config, write_bbsetup_yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "examples" / "bbsetup-oe-nodistro-wrynose"

_OE_CORE_SHA = "06dd66e6220e5ce4ed4b9af4d8231ae5f0a8ce80"
_BITBAKE_SHA = "22021758e66737bcf68dfd2b74adc6a0cb1d42d9"
_DOCS_SHA = "d7376cca64a0784e59d4fd60b9baefb4da2ce289"


def _copy_fixture(tmp_path: Path) -> Path:
    """Copy the committed fixture into a writable tmp workspace.

    Returns the new setup dir so callers can mutate ``config/*.json`` or drop
    ``sources-fixed-revisions.json`` without touching the committed fixture.
    """
    dest = tmp_path / "ws"
    shutil.copytree(
        FIXTURE,
        dest,
    )
    return dest


# ---------------------------------------------------------------------------
# Happy path (committed fixture, read-only)
# ---------------------------------------------------------------------------


def test_happy_path_machine_distro():
    data = translate_bbsetup_config(FIXTURE)

    assert data["machine"] == "qemux86-64"
    assert data["distro"] == "nodistro"


def test_happy_path_three_repos():
    data = translate_bbsetup_config(FIXTURE)

    assert set(data["repos"]) == {"bitbake", "openembedded-core", "yocto-docs"}


def test_happy_path_layers():
    data = translate_bbsetup_config(FIXTURE)

    assert data["repos"]["openembedded-core"]["layers"] == {"meta": None}
    assert data["repos"]["bitbake"]["layers"] == {}
    assert data["repos"]["yocto-docs"]["layers"] == {}


def test_happy_path_commit_shas():
    data = translate_bbsetup_config(FIXTURE)

    assert data["repos"]["openembedded-core"]["commit"] == _OE_CORE_SHA
    assert data["repos"]["bitbake"]["commit"] == _BITBAKE_SHA
    assert data["repos"]["yocto-docs"]["commit"] == _DOCS_SHA


# ---------------------------------------------------------------------------
# Overrides and fragment-derived distro
# ---------------------------------------------------------------------------


def test_machine_override_replaces_fragment_value(tmp_path):
    ws = _copy_fixture(tmp_path)

    data = translate_bbsetup_config(ws, machine_override="qemuarm64")

    assert data["machine"] == "qemuarm64"


def test_distro_fragment_choice_yields_distro(tmp_path):
    ws = _copy_fixture(tmp_path)
    cfg_path = ws / "config" / "config-upstream.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["bitbake-config"]["oe-fragment-choices"]["distro"] = "distro/poky"
    cfg_path.write_text(json.dumps(cfg))

    data = translate_bbsetup_config(ws)

    assert data["distro"] == "poky"


# ---------------------------------------------------------------------------
# SHA fallback (sources-fixed-revisions.json absent)
# ---------------------------------------------------------------------------


def test_missing_sfr_symbolic_rev_uses_branch(tmp_path):
    ws = _copy_fixture(tmp_path)
    (ws / "config" / "sources-fixed-revisions.json").unlink()

    data = translate_bbsetup_config(ws)

    # openembedded-core has rev "wrynose" (symbolic) in the fixture config.
    oe_core = data["repos"]["openembedded-core"]
    assert oe_core["branch"] == "wrynose"
    assert "commit" not in oe_core


def test_missing_sfr_hex_rev_uses_commit(tmp_path):
    ws = _copy_fixture(tmp_path)
    (ws / "config" / "sources-fixed-revisions.json").unlink()
    cfg_path = ws / "config" / "config-upstream.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["data"]["sources"]["openembedded-core"]["git-remote"]["rev"] = _OE_CORE_SHA
    cfg_path.write_text(json.dumps(cfg))

    data = translate_bbsetup_config(ws)

    assert data["repos"]["openembedded-core"]["commit"] == _OE_CORE_SHA


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_missing_data_key_raises_and_writes_nothing(tmp_path):
    ws = _copy_fixture(tmp_path)
    cfg_path = ws / "config" / "config-upstream.json"
    cfg = json.loads(cfg_path.read_text())
    del cfg["data"]
    cfg_path.write_text(json.dumps(cfg))

    with pytest.raises(ValueError):
        write_bbsetup_yaml(ws)

    assert not (ws / "kas-bbsetup.yml").exists()


def test_local_path_source_raises(tmp_path):
    ws = _copy_fixture(tmp_path)
    cfg_path = ws / "config" / "config-upstream.json"
    cfg = json.loads(cfg_path.read_text())
    # A "local" type source has no "git-remote" key.
    cfg["data"]["sources"]["my-local-layer"] = {"local-path": {"path": "/some/where"}}
    cfg_path.write_text(json.dumps(cfg))

    with pytest.raises(ValueError):
        translate_bbsetup_config(ws)


# ---------------------------------------------------------------------------
# write_bbsetup_yaml: structure and determinism
# ---------------------------------------------------------------------------


def test_write_bbsetup_yaml_structure(tmp_path):
    ws = _copy_fixture(tmp_path)

    output = write_bbsetup_yaml(ws)

    assert output == ws / "kas-bbsetup.yml"
    doc = yaml.safe_load(output.read_text())
    assert doc["header"]["version"] == 3
    assert doc["machine"] == "qemux86-64"
    assert doc["distro"] == "nodistro"
    assert doc["target"] == "core-image-minimal"
    assert set(doc["repos"]) == {"bitbake", "openembedded-core", "yocto-docs"}


def test_write_bbsetup_yaml_is_deterministic(tmp_path):
    ws = _copy_fixture(tmp_path)

    write_bbsetup_yaml(ws)
    first = (ws / "kas-bbsetup.yml").read_bytes()
    write_bbsetup_yaml(ws)
    second = (ws / "kas-bbsetup.yml").read_bytes()

    assert first == second


# ---------------------------------------------------------------------------
# Schema-validation error paths (post-review robustness)
# ---------------------------------------------------------------------------


def _patch_config(ws: Path, mutate) -> None:
    cfg_path = ws / "config" / "config-upstream.json"
    cfg = json.loads(cfg_path.read_text())
    mutate(cfg)
    cfg_path.write_text(json.dumps(cfg))


def test_translate_rejects_empty_sources(tmp_path):
    ws = _copy_fixture(tmp_path)
    _patch_config(ws, lambda c: c["data"].update(sources={}))

    with pytest.raises(ValueError, match="data.sources"):
        translate_bbsetup_config(ws)


def test_translate_rejects_non_object_data(tmp_path):
    ws = _copy_fixture(tmp_path)
    _patch_config(ws, lambda c: c.update(data="not-an-object"))

    with pytest.raises(ValueError, match="'data' object"):
        translate_bbsetup_config(ws)


def test_translate_rejects_malformed_fixed_revisions(tmp_path):
    ws = _copy_fixture(tmp_path)
    (ws / "config" / "sources-fixed-revisions.json").write_text("{ not json")

    with pytest.raises(ValueError, match="not valid JSON"):
        translate_bbsetup_config(ws)


# ---------------------------------------------------------------------------
# CLI workspace-resolution helpers
# ---------------------------------------------------------------------------


def test_bbsetup_workspace_walks_up_from_subdir(tmp_path, monkeypatch):
    from bspctl import cli

    ws = _copy_fixture(tmp_path)
    monkeypatch.chdir(ws / "config")

    assert cli._bbsetup_workspace(None) == ws.resolve()


def test_uninitialized_bbsetup_dir_detects_missing_sentinel(tmp_path):
    from bspctl import cli

    ws = tmp_path / "partial"
    (ws / "config").mkdir(parents=True)
    (ws / "config" / "config-upstream.json").write_text(json.dumps({"data": {"sources": {}}, "bitbake-config": {}}))
    # No build/init-build-env -> recognized signature but not initialized.
    assert cli._uninitialized_bbsetup_dir(ws) == ws.resolve()

    # Once the sentinel exists, it is considered initialized (not pending).
    (ws / "build").mkdir()
    (ws / "build" / "init-build-env").write_text("")
    assert cli._uninitialized_bbsetup_dir(ws) is None
