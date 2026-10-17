"""Post-build steps leave step records in the run's ``events.jsonl``.

The CVE report, the SBOM filter and the feed sync run after a build that already
succeeded, so none of them may change the exit status. Their outcome is
otherwise only a console line, which is gone once the terminal scrolls; the
event log is what ``bakar triage`` and the monitor read afterwards.

A real ``RunLogger`` is used because the records are the thing under test, and a
stub would pass whatever the helpers chose to call.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bakar import cve_report, feed_ops, sbom_publish
from bakar.commands import _post_build as post_build
from bakar.commands import build as build_mod
from bakar.observability import RunLogger
from bakar.steps.kas_build import KasBuildContext

pytestmark = pytest.mark.unit

MACHINE = "avocado-qemux86-64"


@pytest.fixture
def cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        host_mode=True,
        resolved_tmpdir=tmp_path / "tmp",
        machine=MACHINE,
        workspace=tmp_path / "ws",
        kas_yaml=tmp_path / "ws" / "machine.yml",
        effective_feed_dir=tmp_path / "_feed",
    )


@pytest.fixture
def log(tmp_path: Path):
    with RunLogger(tmp_path / "runs") as logger:
        yield logger


def _steps(log: RunLogger) -> list[tuple[str, str, str | None]]:
    """(event, step, reason) for every step record in the run's events.jsonl."""
    records = [json.loads(line) for line in log.events_path.read_text(encoding="utf-8").splitlines()]
    return [(r["event"], r["step"], r.get("reason")) for r in records if r.get("step")]


def test_failing_feed_sync_records_step_fail_and_exits_zero(monkeypatch, cfg, log) -> None:
    def boom(*_a, **_kw):
        raise TypeError("sync() got an unexpected keyword argument 'deploy_dir'")

    monkeypatch.setattr(feed_ops, "sync_then_index", boom)
    request = post_build._FeedRequest(kas_yaml=cfg.kas_yaml, release="dev", channel="local")

    # Returns normally: a raised typer.Exit here would be the non-zero exit.
    build_mod._finish_build(cfg, log, 0, MACHINE, feed=request)

    steps = _steps(log)
    assert steps[0] == ("step_start", "feed_sync", None)
    assert steps[1][:2] == ("step_fail", "feed_sync")
    assert "deploy_dir" in (steps[1][2] or "")
    assert not any(event == "step_ok" for event, _, _ in steps)


def test_successful_feed_sync_records_step_ok(monkeypatch, cfg, log) -> None:
    monkeypatch.setattr(
        feed_ops,
        "sync_then_index",
        lambda *_a, **_kw: {"snapshot": "s1", "machines": [MACHINE], "unstaged": []},
    )
    request = post_build._FeedRequest(kas_yaml=cfg.kas_yaml, release="dev", channel="local")

    build_mod._finish_build(cfg, log, 0, MACHINE, feed=request)

    assert [(e, s) for e, s, _ in _steps(log)] == [("step_start", "feed_sync"), ("step_ok", "feed_sync")]


def test_produced_cve_report_records_step_ok(monkeypatch, cfg, log) -> None:
    cve_dir = cve_report.cve_data_dir(cfg, MACHINE)
    cve_dir.mkdir(parents=True)
    (cve_dir / "glibc_cve.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(post_build.step_kas, "run_build", lambda *_a, **_kw: 0)
    ctx = KasBuildContext(cfg, log, cfg.kas_yaml, Path("/overlay"), target=None)

    build_mod._finish_build(cfg, log, 0, MACHINE, cve=post_build._CveRequest(kas_ctx=ctx, extra_overlays=[]))

    assert [(e, s) for e, s, _ in _steps(log)] == [("step_start", "cve_report"), ("step_ok", "cve_report")]


def test_failed_cve_report_records_step_fail_and_exits_zero(monkeypatch, cfg, log) -> None:
    cve_dir = cve_report.cve_data_dir(cfg, MACHINE)
    cve_dir.mkdir(parents=True)
    (cve_dir / "glibc_cve.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(post_build.step_kas, "run_build", lambda *_a, **_kw: 3)
    ctx = KasBuildContext(cfg, log, cfg.kas_yaml, Path("/overlay"), target=None)

    build_mod._finish_build(cfg, log, 0, MACHINE, cve=post_build._CveRequest(kas_ctx=ctx, extra_overlays=[]))

    assert _steps(log)[-1] == ("step_fail", "cve_report", "avocado-cve-report exited 3")


def _install_filter(cfg) -> None:
    lib = sbom_publish.sbom_lib_dir(cfg.workspace)
    (lib / "avocado_sbom").mkdir(parents=True)
    (lib / "avocado_sbom" / "publish.py").write_text("", encoding="utf-8")


def _write_image_sbom(cfg) -> None:
    images = sbom_publish.images_dir(cfg) / MACHINE
    images.mkdir(parents=True, exist_ok=True)
    (images / "avocado-image-rootfs-qemux86-64.spdx.json").write_text(
        json.dumps({"@graph": [{"type": "software_Package", "name": "zlib"}]}), encoding="utf-8"
    )


def _fake_filter(node: dict):
    def run(cmd, **_kwargs):
        out = Path(cmd[cmd.index("-o") + 1]) / MACHINE
        out.mkdir(parents=True, exist_ok=True)
        (out / "avocado-image-rootfs-qemux86-64.spdx.json").write_text(json.dumps({"@graph": [node]}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run


def test_clean_filtered_sbom_records_step_ok(monkeypatch, cfg, log) -> None:
    _install_filter(cfg)
    _write_image_sbom(cfg)
    monkeypatch.setattr(post_build.subprocess, "run", _fake_filter({"type": "software_Package", "name": "zlib"}))

    build_mod._finish_build(cfg, log, 0, MACHINE, sbom=post_build._SbomRequest(workspace=cfg.workspace))

    assert [(e, s) for e, s, _ in _steps(log)] == [("step_start", "sbom_filter"), ("step_ok", "sbom_filter")]


def test_leaking_filtered_sbom_records_step_fail_and_exits_zero(monkeypatch, cfg, log) -> None:
    _install_filter(cfg)
    _write_image_sbom(cfg)
    monkeypatch.setattr(post_build.subprocess, "run", _fake_filter({"type": "security_Vulnerability"}))

    build_mod._finish_build(cfg, log, 0, MACHINE, sbom=post_build._SbomRequest(workspace=cfg.workspace))

    event, step, reason = _steps(log)[-1]
    assert (event, step) == ("step_fail", "sbom_filter")
    assert "vulnerability data" in (reason or "")


class _FullDisk:
    """Stands in for ``console_path``: every open fails like a full filesystem."""

    def open(self, *_a, **_kw):
        raise OSError(errno.ENOSPC, "No space left on device")


def test_full_disk_on_console_log_does_not_change_the_post_build_exit(monkeypatch, cfg, log) -> None:
    def boom(*_a, **_kw):
        raise RuntimeError("rsync failed")

    monkeypatch.setattr(feed_ops, "sync_then_index", boom)
    monkeypatch.setattr(type(log), "console_path", property(lambda _self: _FullDisk()))
    request = post_build._FeedRequest(kas_yaml=cfg.kas_yaml, release="dev", channel="local")

    # Returning normally is the contract: an OSError escaping here would exit 1
    # after "build succeeded".
    build_mod._finish_build(cfg, log, 0, MACHINE, feed=request)

    assert _steps(log)[-1][:2] == ("step_fail", "feed_sync")


def test_failing_cve_report_build_does_not_read_as_a_failed_build(monkeypatch, tmp_path: Path) -> None:
    """The nested report run shares the build's RunLogger: its failure must stay a post-build failure."""
    from bakar.commands.triage import _run_has_failure
    from bakar.config import BuildConfig
    from bakar.steps import kas_build
    from bakar.triage import is_build_failure_event

    cfg = BuildConfig(
        workspace=tmp_path,
        host_mode=False,
        bsp_family="nxp",
        machine="imx95-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.12.49-2.2.0.xml",
        repo_url="https://example.invalid/none.git",
        repo_branch="walnascar",
        kas_container_image="jetm/kas-build-env:latest",
    )
    cfg.bsp_root.mkdir(parents=True, exist_ok=True)
    kas_yaml = cfg.bsp_root / "build.yml"
    kas_yaml.write_text("header: {}\n", encoding="utf-8")
    overlay = tmp_path / "bakar-tuning-nxp.yml"
    overlay.write_text("header: {}\n", encoding="utf-8")
    cve_dir = cve_report.cve_data_dir(cfg, cfg.machine)
    cve_dir.mkdir(parents=True)
    (cve_dir / "glibc_cve.json").write_text("{}", encoding="utf-8")

    rcs = iter([0, 1])
    persisted: list[str] = []
    monkeypatch.setattr(kas_build, "_run_pty_with_ui", lambda *_a, **_k: kas_build._PtyOutcome(rc=next(rcs)))
    monkeypatch.setattr(kas_build, "_capture_dependency_graph", lambda *_a, **_k: None)
    monkeypatch.setattr(kas_build, "persist_run_artifacts", lambda _cfg, log, **_k: persisted.append(log.run_id))

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        ctx = KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)
        assert kas_build.run_build(ctx) == 0
        post_build._generate_cve_report(cfg, log, post_build._CveRequest(kas_ctx=ctx, extra_overlays=[]), cfg.machine)
        run_dir = log.run_dir

    records = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    fails = [r for r in records if r.get("event") == "step_fail"]
    assert fails, "the report run's failure is still recorded"
    assert all(r["step"] == "cve_report" for r in fails)
    assert not any(is_build_failure_event(r) for r in records)
    assert not _run_has_failure(run_dir)
    assert not (run_dir / "error-report.json").exists()
    # Only the main build normalized its artifacts into the run dir.
    assert len(persisted) == 1
