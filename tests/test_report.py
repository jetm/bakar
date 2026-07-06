"""Unit tests for :func:`bakar.report.assemble_report`.

Builds a real run directory under ``tmp_path`` following the
:class:`bakar.observability.RunLogger` event schema
(``{"ts": ..., "event": ..., ...}`` with the ``%Y-%m-%dT%H:%M:%SZ`` ts
format), then asserts the assembled summary. The ``BuildConfig`` is resolved
from a tmp nxp workspace; ``collect_layer_hashes(cfg)`` returns ``[]`` because
there is no bblayers.conf, which is fine - this test exercises
status/duration/deploy_dir.
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
    """A success run yields status success, positive duration, and deploy_dir."""
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

    summary = assemble_report(run_dir, cfg)

    assert summary.run_id == "20260527-100000"
    assert summary.status == "success"
    assert summary.duration_s is not None
    assert summary.duration_s > 0
    assert summary.duration_s == pytest.approx(1845.0)
    assert summary.deploy_dir == deploy_dir


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


def _write_json(path: Path, payload: object) -> None:
    """Write ``payload`` as JSON into ``path`` (parent must already exist)."""
    path.write_text(json.dumps(payload))


def _success_events(run_dir: Path) -> None:
    """Write a minimal success event log so status resolves to success."""
    _write_events(
        run_dir,
        [
            {"ts": "2026-05-27T10:00:00Z", "event": "run_start", "run_id": run_dir.name},
            {"ts": "2026-05-27T10:30:45Z", "event": "step_ok", "step": "kas_build"},
            {"ts": "2026-05-27T10:30:45Z", "event": "run_end"},
        ],
    )


def test_measurement_fields_populated(tmp_path: Path) -> None:
    """Per-language cache, per-node, and task-family rollup fields populate."""
    cfg = _nxp_cfg(tmp_path)
    run_dir = tmp_path / "runs" / "20260527-120000"
    _success_events(run_dir)
    _write_json(
        run_dir / "sccache-stats.json",
        {
            "cache_hits": 511,
            "cache_misses": 52186,
            "distributed": 100,
            "dist_errors": 0,
            "per_node": {"10.42.0.1": 60, "10.42.0.2": 40},
            "hits_by_lang": {"Rust": 400, "C/C++": 100},
            "misses_by_lang": {"Rust": 100, "C/C++": 52086},
        },
    )
    _write_json(
        run_dir / "bitbake-events.json",
        {
            "tasks": [
                {"recipe": "go-helloworld", "task": "do_compile", "started": 0, "completed": 20},
                {"recipe": "zlib", "task": "do_compile", "started": 0, "completed": 30},
                {"recipe": "zlib", "task": "do_configure", "started": 0, "completed": 5},
            ]
        },
    )

    summary = assemble_report(run_dir, cfg)

    assert set(summary.cache_by_language) == {"Rust", "C/C++"}
    rust = summary.cache_by_language["Rust"]
    assert rust.hits == 400
    assert rust.misses == 100
    assert rust.hit_rate == pytest.approx(80.0)

    assert summary.dist_by_node == {"10.42.0.1": 60, "10.42.0.2": 40}

    assert summary.task_family_rollup["do_compile"].seconds == pytest.approx(50.0)
    assert summary.task_family_rollup["do_compile"].count == 2
    assert summary.task_family_rollup["do_configure"].seconds == pytest.approx(5.0)
    assert summary.go_compile_seconds == pytest.approx(20.0)


def test_missing_sccache_stats_yields_empty_fields(tmp_path: Path) -> None:
    """A run with no sccache-stats.json yields empty cache/node fields, no raise."""
    cfg = _nxp_cfg(tmp_path)
    run_dir = tmp_path / "runs" / "20260527-130000"
    _success_events(run_dir)

    summary = assemble_report(run_dir, cfg)

    assert summary.cache_by_language == {}
    assert summary.dist_by_node == {}


def test_empty_event_log_yields_zeroed_rollup(tmp_path: Path) -> None:
    """A run whose event log has no usable task durations yields a zeroed rollup."""
    cfg = _nxp_cfg(tmp_path)
    run_dir = tmp_path / "runs" / "20260527-140000"
    _success_events(run_dir)
    _write_json(run_dir / "bitbake-events.json", {"tasks": []})

    summary = assemble_report(run_dir, cfg)

    assert summary.go_compile_seconds == 0.0
    assert all(stat.seconds == 0.0 and stat.count == 0 for stat in summary.task_family_rollup.values())


# ---------------------------------------------------------------------------
# bakar report - backend-agnostic ccache section (commands/report.py)
# ---------------------------------------------------------------------------


def _cli_run_dir(nxp_ws: Path) -> Path:
    run_dir = nxp_ws / "nxp" / "build" / "runs" / "20260527-100000"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def test_report_json_gains_ccache_key_when_artifact_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--json`` gains an additive ``ccache_cache`` key when ccache-stats.json exists."""
    from typer.testing import CliRunner

    import bakar.commands.report as report_module
    from bakar.cli import app
    from bakar.report import LangCacheStat, ReportSummary

    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    run_dir = _cli_run_dir(tmp_path)
    (run_dir / "ccache-stats.json").write_text(
        json.dumps({"cache_hits": 5, "cache_misses": 2, "hit_rate": 71.4, "window": "build"})
    )
    summary = ReportSummary(
        run_id="20260527-100000",
        status="success",
        duration_s=1.0,
        deploy_dir="/x",
        image_size=1,
        layers=[],
        build_revision=None,
        cache_by_language={"C/C++": LangCacheStat(hits=10, misses=1, hit_rate=90.9)},
        dist_by_node={"10.42.0.2": 5},
    )
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: summary)

    result = CliRunner().invoke(app, ["report", "--json", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    # The additive ccache key is present with the three tool counters plus window.
    assert payload["ccache_cache"] == {"cache_hits": 5, "cache_misses": 2, "hit_rate": 71.4, "window": "build"}
    # The pre-existing sccache keys are retained, unchanged in shape.
    assert payload["cache_by_language"]["C/C++"]["hits"] == 10
    assert payload["dist_by_node"] == {"10.42.0.2": 5}


def test_report_json_omits_ccache_key_when_artifact_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing ccache-stats.json omits the section without error."""
    from typer.testing import CliRunner

    import bakar.commands.report as report_module
    from bakar.cli import app
    from bakar.report import ReportSummary

    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    run_dir = _cli_run_dir(tmp_path)  # no ccache-stats.json written
    summary = ReportSummary(
        run_id="20260527-100000",
        status="success",
        duration_s=1.0,
        deploy_dir="/x",
        image_size=1,
        layers=[],
        build_revision=None,
    )
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: summary)

    result = CliRunner().invoke(app, ["report", "--json", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert "ccache_cache" not in payload
    # cache_by_language / dist_by_node still present (empty), never removed.
    assert "cache_by_language" in payload
    assert "dist_by_node" in payload


def test_report_human_shows_ccache_section_when_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The human path renders an additive ccache section when the artifact exists."""
    from typer.testing import CliRunner

    import bakar.commands.report as report_module
    from bakar.cli import app
    from bakar.report import ReportSummary

    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    run_dir = _cli_run_dir(tmp_path)
    (run_dir / "ccache-stats.json").write_text(
        json.dumps({"cache_hits": 5, "cache_misses": 2, "hit_rate": 71.4, "window": "build"})
    )
    summary = ReportSummary(
        run_id="20260527-100000",
        status="success",
        duration_s=1.0,
        deploy_dir="/x",
        image_size=1,
        layers=[],
        build_revision=None,
    )
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: summary)

    result = CliRunner().invoke(app, ["report", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "ccache" in result.output
    assert "71.4" in result.output


def test_report_json_carries_lifetime_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--json`` carries ``window: "lifetime"`` through to the payload unchanged."""
    from typer.testing import CliRunner

    import bakar.commands.report as report_module
    from bakar.cli import app
    from bakar.report import ReportSummary

    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    run_dir = _cli_run_dir(tmp_path)
    (run_dir / "ccache-stats.json").write_text(
        json.dumps({"cache_hits": 5, "cache_misses": 2, "hit_rate": 71.4, "window": "lifetime"})
    )
    summary = ReportSummary(
        run_id="20260527-100000",
        status="success",
        duration_s=1.0,
        deploy_dir="/x",
        image_size=1,
        layers=[],
        build_revision=None,
    )
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: summary)

    result = CliRunner().invoke(app, ["report", "--json", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["ccache_cache"]["window"] == "lifetime"


def test_report_json_window_absent_for_legacy_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-existing artifact written before ``window`` existed yields ``window: None``."""
    from typer.testing import CliRunner

    import bakar.commands.report as report_module
    from bakar.cli import app
    from bakar.report import ReportSummary

    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    run_dir = _cli_run_dir(tmp_path)
    (run_dir / "ccache-stats.json").write_text(json.dumps({"cache_hits": 5, "cache_misses": 2, "hit_rate": 71.4}))
    summary = ReportSummary(
        run_id="20260527-100000",
        status="success",
        duration_s=1.0,
        deploy_dir="/x",
        image_size=1,
        layers=[],
        build_revision=None,
    )
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: summary)

    result = CliRunner().invoke(app, ["report", "--json", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["ccache_cache"]["window"] is None


def test_report_human_shows_lifetime_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The human path prints ``ccache (lifetime):`` when ``window`` is ``"lifetime"``."""
    from typer.testing import CliRunner

    import bakar.commands.report as report_module
    from bakar.cli import app
    from bakar.report import ReportSummary

    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    run_dir = _cli_run_dir(tmp_path)
    (run_dir / "ccache-stats.json").write_text(
        json.dumps({"cache_hits": 5, "cache_misses": 2, "hit_rate": 71.4, "window": "lifetime"})
    )
    summary = ReportSummary(
        run_id="20260527-100000",
        status="success",
        duration_s=1.0,
        deploy_dir="/x",
        image_size=1,
        layers=[],
        build_revision=None,
    )
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: summary)

    result = CliRunner().invoke(app, ["report", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "ccache (lifetime):" in result.output
    assert "ccache (this build):" not in result.output


def test_report_human_falls_back_to_this_build_label_for_legacy_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy artifact with no ``window`` field still prints the historical label."""
    from typer.testing import CliRunner

    import bakar.commands.report as report_module
    from bakar.cli import app
    from bakar.report import ReportSummary

    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    run_dir = _cli_run_dir(tmp_path)
    (run_dir / "ccache-stats.json").write_text(json.dumps({"cache_hits": 5, "cache_misses": 2, "hit_rate": 71.4}))
    summary = ReportSummary(
        run_id="20260527-100000",
        status="success",
        duration_s=1.0,
        deploy_dir="/x",
        image_size=1,
        layers=[],
        build_revision=None,
    )
    monkeypatch.setattr(report_module, "_find_run", lambda runs_dirs, run_id: (run_dir, "nxp"))
    monkeypatch.setattr(report_module, "assemble_report", lambda run_dir, cfg: summary)

    result = CliRunner().invoke(app, ["report", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "ccache (this build):" in result.output
