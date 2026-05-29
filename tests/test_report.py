"""Unit tests for :func:`bakar.report.assemble_report`.

Builds a real run directory under ``tmp_path`` following the
:class:`bakar.observability.RunLogger` event schema
(``{"ts": ..., "event": ..., ...}`` with the ``%Y-%m-%dT%H:%M:%SZ`` ts
format) and a ``du.tsv`` of ``<epoch>\\t<bytes>`` rows, then asserts the
assembled summary. The ``BuildConfig`` is resolved from a tmp nxp workspace;
``collect_layer_hashes(cfg)`` returns ``[]`` because there is no bblayers.conf,
which is fine - this test exercises status/duration/deploy_dir/peak_tmp.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bakar.config import resolve
from bakar.report import assemble_report

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _nxp_cfg(tmp_path: Path):
    """Resolve an nxp BuildConfig rooted at a tmp_path workspace."""
    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    return resolve(workspace=tmp_path, bsp_family="nxp")


def _write_events(run_dir: Path, records: list[dict]) -> None:
    """Write a list of event records as JSONL into ``run_dir/events.jsonl``."""
    run_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(rec) for rec in records]
    (run_dir / "events.jsonl").write_text("\n".join(lines) + "\n")


def test_success_run_summary(tmp_path: Path) -> None:
    """A success run yields status success, positive duration, deploy_dir, peak."""
    cfg = _nxp_cfg(tmp_path)
    run_dir = tmp_path / "runs" / "20260527-100000"
    deploy_dir = "/work/build/tmp/deploy/images/imx8mp-var-dart"
    _write_events(
        run_dir,
        [
            {"ts": "2026-05-27T10:00:00Z", "event": "run_start", "run_id": "20260527-100000"},
            {"ts": "2026-05-27T10:30:45Z", "event": "step_ok", "step": "kas_build", "deploy_dir": deploy_dir},
            {"ts": "2026-05-27T10:30:45Z", "event": "run_end"},
        ],
    )
    (run_dir / "du.tsv").write_text("1716800000\t1000\n1716800100\t5000\n1716800200\t3000\n")

    summary = assemble_report(run_dir, cfg)

    assert summary.run_id == "20260527-100000"
    assert summary.status == "success"
    assert summary.duration_s is not None
    assert summary.duration_s > 0
    assert summary.duration_s == pytest.approx(1845.0)
    assert summary.deploy_dir == deploy_dir
    assert summary.peak_tmp_bytes == 5000


def test_failure_run_status(tmp_path: Path) -> None:
    """A run with step_fail and no step_ok yields status failure."""
    cfg = _nxp_cfg(tmp_path)
    run_dir = tmp_path / "runs" / "20260527-110000"
    _write_events(
        run_dir,
        [
            {"ts": "2026-05-27T11:00:00Z", "event": "run_start", "run_id": "20260527-110000"},
            {"ts": "2026-05-27T11:05:00Z", "event": "step_fail", "step": "kas_build", "reason": "boom"},
            {"ts": "2026-05-27T11:05:00Z", "event": "run_end"},
        ],
    )

    summary = assemble_report(run_dir, cfg)

    assert summary.status == "failure"


def test_missing_du_tsv_yields_none_peak(tmp_path: Path) -> None:
    """A run with no du.tsv yields peak_tmp_bytes is None, no exception."""
    cfg = _nxp_cfg(tmp_path)
    run_dir = tmp_path / "runs" / "20260527-120000"
    _write_events(
        run_dir,
        [
            {"ts": "2026-05-27T12:00:00Z", "event": "run_start", "run_id": "20260527-120000"},
            {"ts": "2026-05-27T12:10:00Z", "event": "step_ok", "step": "kas_build", "deploy_dir": "/work/x"},
            {"ts": "2026-05-27T12:10:00Z", "event": "run_end"},
        ],
    )
    assert not (run_dir / "du.tsv").exists()

    summary = assemble_report(run_dir, cfg)

    assert summary.peak_tmp_bytes is None
