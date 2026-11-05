"""``run_all`` name selection (``only``) and caller deadline (``deadline``)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import bakar.diagnostics as diagnostics
from bakar.config import BuildConfig
from bakar.diagnostics import CheckResult, Severity, Status, run_all

pytestmark = pytest.mark.unit


def _cfg() -> BuildConfig:
    return BuildConfig(
        workspace=Path("/tmp"),
        bsp_family="nxp",
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        kas_container_image="img:latest",
        sstate_dir="/cache/sstate",
        dl_dir="/cache/downloads",
    )


def _make_stub(name: str, sleep: float = 0.0):
    def _stub(cfg: BuildConfig) -> CheckResult:
        if sleep:
            time.sleep(sleep)
        return CheckResult(name=name, severity=Severity.WARN, status=Status.PASS, message="ok")

    _stub.__name__ = f"check_{name}"
    return _stub


def _install(monkeypatch: pytest.MonkeyPatch, stubs: list) -> None:
    names = {s: s.__name__.removeprefix("check_") for s in stubs}
    monkeypatch.setattr(diagnostics, "SHARED_CHECKS", tuple(stubs))
    monkeypatch.setattr(diagnostics, "_CHECK_NAME", names)
    monkeypatch.setattr(diagnostics, "_CHECK_SEVERITY", dict.fromkeys(names.values(), Severity.WARN))
    monkeypatch.setattr(diagnostics, "_DOCKER_CHECKS", ())
    monkeypatch.setattr(diagnostics, "_CLUSTER_CHECKS", ())
    monkeypatch.setattr(diagnostics, "_POST_BUILD_CHECKS", ())


def test_only_returns_exactly_the_named_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [_make_stub("a"), _make_stub("b"), _make_stub("c")])
    results = run_all(_cfg(), only={"a", "b"})
    assert [r.name for r in results] == ["a", "b"]


def test_default_call_returns_every_stub_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [_make_stub("a"), _make_stub("b"), _make_stub("c")])
    assert [r.name for r in run_all(_cfg())] == ["a", "b", "c"]


def test_deadline_overrides_default_and_message_reports_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [_make_stub("slow", sleep=2.0)])
    start = time.monotonic()
    results = run_all(_cfg(), deadline=0.5)
    elapsed = time.monotonic() - start
    assert elapsed < 1.5
    assert len(results) == 1
    assert results[0].status is Status.FAIL
    assert "0.5" in results[0].message
    assert "180" not in results[0].message
