"""candidate_runs_dirs: one definition of every run-search root."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bakar.commands._helpers import _find_run, candidate_runs_dirs

if TYPE_CHECKING:
    from pathlib import Path

# (relative runs dir, expected family, run id). Ids are ascending so the last
# entry is the newest by timestamped name.
_LAYOUT = [
    ("nxp/build/runs", "nxp", "20260101-000001-1"),
    ("ti/build/runs", "ti", "20260101-000002-1"),
    ("qcom/build-qcom-wayland/runs", "qcom", "20260101-000003-1"),
    ("build/runs", "generic", "20260101-000004-1"),
    ("build-qemux86-64/build/runs", "generic", "20260101-000005-1"),
    ("build/rel-a/build/runs", "generic", "20260101-000006-1"),
    ("build/rel-b/nxp/build/runs", "nxp", "20260101-000007-1"),
    ("build/rel-c/ti/build/runs", "ti", "20260101-000008-1"),
    ("build/rel-d/qcom/build-qcom-wayland/runs", "qcom", "20260101-000009-1"),
]


def _make_workspace(tmp_path: Path) -> Path:
    for rel, _family, run_id in _LAYOUT:
        (tmp_path / rel / run_id).mkdir(parents=True)
    return tmp_path


def test_every_location_is_found_with_its_family(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    roots = candidate_runs_dirs(ws)
    for rel, family, run_id in _LAYOUT:
        found = _find_run(roots, run_id)
        assert found == (ws / rel / run_id, family), rel


def test_newest_run_wins_across_roots(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    found = _find_run(candidate_runs_dirs(ws), None)
    assert found is not None
    assert found[0].name == "20260101-000009-1"
    assert found[1] == "qcom"


def test_newest_wins_regardless_of_root_order(tmp_path: Path) -> None:
    (tmp_path / "nxp/build/runs/20270101-000000-1").mkdir(parents=True)
    (tmp_path / "build/rel-a/build/runs/20260101-000000-1").mkdir(parents=True)
    found = _find_run(candidate_runs_dirs(tmp_path), None)
    assert found is not None
    assert found[0].name == "20270101-000000-1"


def test_order_is_stable(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    assert candidate_runs_dirs(ws) == candidate_runs_dirs(ws)


def test_no_duplicate_roots(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path)
    roots = [d.resolve() for d, _ in candidate_runs_dirs(ws, kas_yaml=ws / "build" / "x.yml")]
    assert len(roots) == len(set(roots))


def test_bbsetup_workspace_tags_root_and_fanout(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config-upstream.json").write_text(json.dumps({"data": {}, "bitbake-config": {}}))
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "init-build-env").write_text("")
    (tmp_path / "build/runs/20260101-000001-1").mkdir(parents=True)
    (tmp_path / "build/rel/build/runs/20260101-000002-1").mkdir(parents=True)
    roots = dict(candidate_runs_dirs(tmp_path))
    assert roots[tmp_path / "build" / "runs"] == "bbsetup"
    assert roots[tmp_path / "build" / "rel" / "build" / "runs"] == "bbsetup"


def test_kas_yaml_adds_byo_root(tmp_path: Path) -> None:
    byo = tmp_path / "byo"
    (byo / "build/runs/20260101-000001-1").mkdir(parents=True)
    roots = candidate_runs_dirs(tmp_path / "ws", kas_yaml=byo / "x.yml")
    assert (byo / "build" / "runs", "generic") in roots
    assert _find_run(roots, None) == (byo / "build/runs/20260101-000001-1", "generic")


def test_empty_workspace_finds_nothing(tmp_path: Path) -> None:
    assert _find_run(candidate_runs_dirs(tmp_path), None) is None
