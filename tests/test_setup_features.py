"""Feature catalog invariants for ``bakar init``."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from bakar.setup import features
from bakar.setup.features import CATALOG, FEATURE_IDS, feature_by_id
from bakar.user_config import SETTINGS_SCHEMA

pytestmark = pytest.mark.unit

EXPECTED = [
    ("ccache", 1),
    ("hashserv", 1),
    ("psi", 1),
    ("scope", 1),
    ("nfs-cache", 1),
    ("uninative", 1),
    ("sstate-mirror", 2),
    ("dl-mirror", 2),
    ("cache-push", 2),
    ("sccache-dist", 2),
    ("central-node", 2),
    ("central-client", 2),
    ("remote-dispatch", 3),
    ("release-workspaces", 3),
    ("feed", 3),
]

NOT_WRITTEN = {
    "stall_abort_secs",
    "stop_on_error",
    "mold",
    "scheduler",
    "parallel_make",
    "bb_number_threads",
    "journal",
}


def _written_keys() -> set[str]:
    keys: set[str] = set()
    for f in CATALOG:
        keys.update(s.key for s in f.settings)
        keys.update(f.fixed)
        keys.update(f.off)
    return keys


def test_ids_in_order_and_tier() -> None:
    assert [(f.id, f.tier) for f in CATALOG] == EXPECTED
    assert tuple(i for i, _ in EXPECTED) == FEATURE_IDS


def test_ids_unique_kebab_case() -> None:
    assert len(set(FEATURE_IDS)) == len(FEATURE_IDS)
    assert all(re.fullmatch(r"[a-z]+(-[a-z]+)*", i) for i in FEATURE_IDS)


def test_written_keys_in_schema() -> None:
    assert _written_keys() <= set(SETTINGS_SCHEMA)


def test_markers_in_schema() -> None:
    for f in CATALOG:
        assert set(f.markers) <= set(SETTINGS_SCHEMA), f.id


def test_relation_ids_exist() -> None:
    ids = set(FEATURE_IDS)
    for f in CATALOG:
        assert set(f.requires) <= ids
        assert set(f.excludes) <= ids
        assert {i for i, _ in f.warns_with} <= ids


def test_fixes_non_empty() -> None:
    for f in CATALOG:
        for r in f.requirements:
            assert r.fix.strip(), (f.id, r.target)


def test_fixes_name_no_invented_commands() -> None:
    for f in CATALOG:
        for r in f.requirements:
            assert "bakar daemon" not in r.fix, (f.id, r.target)
            assert "bakar init" not in r.fix, (f.id, r.target)


def test_guides() -> None:
    for f in CATALOG:
        if f.id.startswith("central-"):
            assert f.guide == "docs/features/central-tier.md#what-your-host-needs"
        else:
            assert f.guide == f"docs/features/{f.id}.md#what-your-host-needs"


def test_excluded_settings_not_written() -> None:
    written = {k.removeprefix("build.") for k in _written_keys()}
    assert not (written & NOT_WRITTEN)


def test_scope_off_sets_false() -> None:
    assert dict(feature_by_id("scope").off) == {"build.scope": "false"}
    assert feature_by_id("scope").default_on


def test_defaults_immutable() -> None:
    f = feature_by_id("ccache")
    with pytest.raises(TypeError):
        f.off["x"] = None  # type: ignore[index]


def test_feature_by_id_unknown_lists_ids() -> None:
    with pytest.raises(KeyError) as exc:
        feature_by_id("nope")
    for i in FEATURE_IDS:
        assert i in str(exc.value)


def test_module_has_no_bakar_import() -> None:
    tree = ast.parse(Path(features.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(a.name.split(".")[0] == "bakar" for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert (node.module or "").split(".")[0] != "bakar"


@pytest.mark.unit
def test_scope_check_is_evaluated_against_the_proposed_config() -> None:
    # systemd-scope SKIPs while build.scope is false, the state a user re-enabling scope starts from.
    req = next(r for r in feature_by_id("scope").requirements if r.target == "systemd-scope")
    assert req.needs_values is True


@pytest.mark.unit
def test_daemon_state_skip_is_accepted_for_hashserv() -> None:
    # daemon-state SKIPs when a central endpoint means no per-workspace daemon is in use.
    req = next(r for r in feature_by_id("hashserv").requirements if r.target == "daemon-state")
    assert req.skip_ok is True
