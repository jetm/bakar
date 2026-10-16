"""Inventory of names ``bakar.steps.kas_build`` must and must not re-export.

Patch-target integrity: a name is patched where the module under test looks it
up. A re-export in kas_build that nothing reads is dead weight and hides which
module owns the name, so the removed set is pinned absent and the names a
reader still uses are pinned present.
"""

from __future__ import annotations

import pytest

from bakar.steps import kas_build

REMOVED = [
    "is_path_on_nfs",
    "probe_cluster",
    "_lock_holder_has_activity",
    "_wait_for_cooker_idle",
    "_resolve_capture_target",
    "_PLAIN_STATUS_INTERVAL",
    "GRAPH_CAPTURE_POLL_S",
]

KEPT = [
    "GRAPH_ARTIFACTS",
    "GRAPH_CAPTURE_IDLE_TIMEOUT_S",
    "GRAPH_CAPTURE_TIMEOUT_S",
    "GRAPH_MARKER_NAME",
    "graph_capture_command",
    "_PlainFrameController",
    "_inject_literal_ccache",
    "_inject_literal_mold",
    "_inject_literal_parallelism",
    "_inject_literal_sccache",
    "_inject_local_tmpdir",
    "_resolve_parallelism",
]


@pytest.mark.parametrize("name", REMOVED)
def test_removed_name_absent_from_kas_build(name: str) -> None:
    assert not hasattr(kas_build, name), f"kas_build re-exports {name} with no reader"


@pytest.mark.parametrize("name", KEPT)
def test_kept_name_present_in_kas_build(name: str) -> None:
    assert hasattr(kas_build, name), f"kas_build must still expose {name}"
