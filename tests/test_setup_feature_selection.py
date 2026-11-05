"""Tests for the pure feature-selection logic."""

from __future__ import annotations

import pytest

from bakar.setup.feature_selection import derive_enabled, plan_changes, validate_selection

CENTRAL = {"build.bb_hashserve": "h:1", "build.prserv_host": "p:2", "build.cluster": "true"}


@pytest.mark.unit
def test_empty_config_enables_only_scope() -> None:
    assert derive_enabled({}, central_node_units_enabled=False) == {"scope"}


@pytest.mark.unit
def test_explicit_false_marker_turns_default_on_feature_off() -> None:
    assert derive_enabled({"build.scope": False}, central_node_units_enabled=False) == frozenset()
    assert derive_enabled({"build.scope": "false"}, central_node_units_enabled=False) == frozenset()


@pytest.mark.unit
def test_truthy_markers() -> None:
    raw = {"build.ccache": "yes", "build.psi_autocalibrate": 1, "build.hashserv": 0, "build.scope": True}
    assert derive_enabled(raw, central_node_units_enabled=False) == {"ccache", "psi", "scope"}


@pytest.mark.unit
def test_any_mode_needs_one_marker_all_mode_needs_every_one() -> None:
    assert "nfs-cache" in derive_enabled({"build.dl_dir": "/dl"}, central_node_units_enabled=False)
    partial = {"build.sccache_dist": True}
    assert "sccache-dist" not in derive_enabled(partial, central_node_units_enabled=False)
    full = {"build.sccache_dist": True, "build.sccache_scheduler_url": "http://s"}
    assert "sccache-dist" in derive_enabled(full, central_node_units_enabled=False)


@pytest.mark.unit
def test_remote_dispatch_is_never_enabled() -> None:
    raw = {"dispatch.worktree_root": "/x", "dispatch.layer_repo_dir": "/y"}
    assert "remote-dispatch" not in derive_enabled(raw, central_node_units_enabled=True)
    assert "remote-dispatch" not in derive_enabled(raw, central_node_units_enabled=False)


@pytest.mark.unit
def test_central_roles_split_on_units_flag() -> None:
    node = derive_enabled(CENTRAL, central_node_units_enabled=True)
    client = derive_enabled(CENTRAL, central_node_units_enabled=False)
    assert "central-node" in node and "central-client" not in node
    assert "central-client" in client and "central-node" not in client


@pytest.mark.unit
def test_removing_scope_sets_it_false_rather_than_unsetting() -> None:
    changes = plan_changes(set(), {"scope"}, {})
    assert changes.sets == {"build.scope": "false"}
    assert "build.scope" not in changes.unsets
    assert changes.removed == ("scope",)


@pytest.mark.unit
def test_removed_feature_unsets_its_keys() -> None:
    changes = plan_changes({"scope"}, {"scope", "ccache"}, {})
    assert changes.unsets == ("build.ccache", "build.ccache_dir", "build.ccache_shared")


@pytest.mark.unit
def test_off_never_unsets_key_a_selected_feature_writes() -> None:
    values = {"build.bb_hashserve": "h:1", "build.prserv_host": "p:2"}
    changes = plan_changes({"central-node"}, {"central-client"}, values)
    assert changes.unsets == ()
    assert changes.sets["build.bb_hashserve"] == "h:1"
    assert changes.sets["build.cluster"] == "true"


@pytest.mark.unit
def test_node_plus_client_is_an_error() -> None:
    errors, _ = validate_selection({"central-node", "central-client"}, set())
    assert len(errors) == 1
    assert "choose one" in errors[0]


@pytest.mark.unit
def test_unknown_id_error_lists_valid_ids() -> None:
    errors, _ = validate_selection({"bogus"}, set())
    assert len(errors) == 1
    assert "bogus" in errors[0] and "ccache" in errors[0]


@pytest.mark.unit
def test_requires_satisfied_by_the_selection() -> None:
    errors, _ = validate_selection({"remote-dispatch"}, set())
    assert any("release-workspaces" in e for e in errors)
    assert validate_selection({"remote-dispatch", "release-workspaces"}, set())[0] == []
    # An enabled feature kept in the selection satisfies it; selection is enabled minus removals.
    assert validate_selection({"remote-dispatch", "release-workspaces"}, {"release-workspaces"})[0] == []


@pytest.mark.unit
def test_removing_a_required_feature_in_the_same_run_is_refused() -> None:
    errors, _ = validate_selection({"remote-dispatch"}, {"remote-dispatch", "release-workspaces"})
    assert any("remote-dispatch requires release-workspaces" in e for e in errors)


@pytest.mark.unit
def test_feed_plus_remote_dispatch_warns() -> None:
    errors, warnings = validate_selection({"feed", "remote-dispatch", "release-workspaces"}, set())
    assert errors == []
    assert len(warnings) == 1
    assert "feed" in warnings[0]


@pytest.mark.unit
def test_hashserv_with_central_warns() -> None:
    _, warnings = validate_selection({"hashserv", "central-client"}, set())
    assert len(warnings) == 1


@pytest.mark.unit
def test_selected_remote_dispatch_is_configured_not_added() -> None:
    changes = plan_changes({"remote-dispatch", "ccache"}, set(), {})
    assert "remote-dispatch" in changes.configure
    assert "remote-dispatch" not in changes.added
    assert changes.added == ("ccache",)
    assert changes.configure == ("ccache", "remote-dispatch")


@pytest.mark.unit
def test_empty_string_value_is_dropped_from_sets() -> None:
    values = {"dispatch.snapshot_untracked_cap_mb": "", "dispatch.worktree_root": "/w"}
    changes = plan_changes({"remote-dispatch"}, set(), values)
    assert "dispatch.snapshot_untracked_cap_mb" not in changes.sets
    assert changes.sets == {"dispatch.worktree_root": "/w"}


@pytest.mark.unit
def test_string_zero_marker_is_off_like_the_settings_coercion() -> None:
    raw = {"build.ccache": "0", "build.hashserv": "false", "build.scope": "1"}
    assert derive_enabled(raw, central_node_units_enabled=False) == {"scope"}
