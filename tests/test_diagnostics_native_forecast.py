"""Tests for the ``native-rebuild-forecast`` doctor check."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from bakar import diagnostics, native_provenance
from bakar.commands._helpers import _print_diagnosis
from bakar.config import BuildConfig

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={**os.environ, **_GIT_ENV},
    )


def _make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "f.txt").write_text("0\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "c0")
    return repo


def _advance(repo: Path, n: int = 1) -> None:
    for i in range(n):
        (repo / "f.txt").write_text(f"{i + 1}\n")
        _git(repo, "commit", "-q", "-a", "-m", f"c{i + 1}")


def _cfg(workspace: Path, sstate: Path | None) -> BuildConfig:
    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        kas_container_image="img:latest",
        sstate_dir=str(sstate) if sstate else None,
    )


def _record(workspace: Path, sstate: Path, *, run_id: str = "run-1", outcome: str = "success") -> None:
    cfg = _cfg(workspace, sstate)
    snap = native_provenance.snapshot_repos(workspace, cfg.bsp_root, probe_timeout=10.0)
    path = native_provenance.write_record(
        sstate,
        None,
        snap,
        node="node-a",
        run_id=run_id,
        outcome=outcome,
        now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )
    assert path is not None


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    ws = tmp_path / "ws"
    _make_repo(ws, "bitbake")
    _make_repo(ws, "meta-avocado")
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    return ws, sstate


def test_skip_without_sstate_dir(env: tuple[Path, Path]) -> None:
    ws, _ = env
    result = diagnostics.check_native_forecast(_cfg(ws, None))
    assert result.status is diagnostics.Status.SKIP
    assert result.severity is diagnostics.Severity.INFO
    assert "sstate" in result.message


def test_skip_without_records(env: tuple[Path, Path]) -> None:
    ws, sstate = env
    result = diagnostics.check_native_forecast(_cfg(ws, sstate))
    assert result.status is diagnostics.Status.SKIP
    assert result.severity is diagnostics.Severity.INFO
    assert "no build has been recorded for" in result.message


def test_exact_match_passes_at_info(env: tuple[Path, Path]) -> None:
    ws, sstate = env
    _record(ws, sstate)
    result = diagnostics.check_native_forecast(_cfg(ws, sstate))
    assert result.status is diagnostics.Status.PASS
    assert result.severity is diagnostics.Severity.INFO
    assert "2026-09-01" in result.message
    assert "node-a" in result.message
    assert "run-1" in result.message
    assert "configuration changes are not forecast" in result.message


def test_bitbake_moved_warns_with_wide_rebuild(env: tuple[Path, Path]) -> None:
    ws, sstate = env
    _record(ws, sstate)
    _advance(ws / "bitbake", 3)
    result = diagnostics.check_native_forecast(_cfg(ws, sstate))
    assert result.status is diagnostics.Status.FAIL
    assert result.severity is diagnostics.Severity.WARN
    assert "bitbake (3 ahead / 0 behind)" in result.message
    assert "expect a wide native rebuild" in result.message
    assert "run-1" in result.message and "node-a" in result.message
    assert result.fix_hint is not None
    assert "bakar insights --natives" in result.fix_hint


def test_ordinary_layer_moved_is_info(env: tuple[Path, Path]) -> None:
    ws, sstate = env
    _record(ws, sstate)
    _advance(ws / "meta-avocado")
    result = diagnostics.check_native_forecast(_cfg(ws, sstate))
    assert result.status is diagnostics.Status.FAIL
    assert result.severity is diagnostics.Severity.INFO
    assert "meta-avocado (1 ahead / 0 behind)" in result.message
    assert "wide native rebuild" not in result.message


def test_ordinary_layer_moved_and_dirty_warns(env: tuple[Path, Path]) -> None:
    ws, sstate = env
    _record(ws, sstate)
    _advance(ws / "meta-avocado")
    (ws / "meta-avocado" / "f.txt").write_text("dirty\n")
    result = diagnostics.check_native_forecast(_cfg(ws, sstate))
    assert result.severity is diagnostics.Severity.WARN
    assert "meta-avocado" in result.message
    assert "dirty, not forecastable: meta-avocado" in result.message
    assert "wide native rebuild" not in result.message


def test_unknown_distance_is_stated(env: tuple[Path, Path]) -> None:
    ws, sstate = env
    _record(ws, sstate)
    _advance(ws / "meta-avocado")
    # Replace the recorded sha with one the checkout has never seen.
    rec = next((sstate / ".bakar" / "native-provenance" / "_unknown").glob("*.json"))
    rec.write_text(rec.read_text().replace(_head(ws / "meta-avocado", "HEAD~1"), "f" * 40))
    result = diagnostics.check_native_forecast(_cfg(ws, sstate))
    assert "meta-avocado (distance unknown)" in result.message


def _head(repo: Path, rev: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", rev], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_malformed_record_is_named_and_not_a_match(env: tuple[Path, Path]) -> None:
    ws, sstate = env
    _record(ws, sstate)
    bad = sstate / ".bakar" / "native-provenance" / "_unknown" / "broken.json"
    bad.write_text("{not json")
    exact = diagnostics.check_native_forecast(_cfg(ws, sstate))
    assert exact.status is diagnostics.Status.PASS
    assert "unreadable record: broken.json" in exact.message

    _advance(ws / "bitbake")
    moved = diagnostics.check_native_forecast(_cfg(ws, sstate))
    assert moved.status is diagnostics.Status.FAIL
    assert "unreadable record: broken.json" in moved.message


def test_only_malformed_records_names_them_and_skips(env: tuple[Path, Path]) -> None:
    ws, sstate = env
    root = sstate / ".bakar" / "native-provenance" / "_unknown"
    root.mkdir(parents=True)
    (root / "broken.json").write_text("{not json")
    result = diagnostics.check_native_forecast(_cfg(ws, sstate))
    assert result.status is diagnostics.Status.SKIP
    assert "broken.json" in result.message


def test_never_blocks_across_fixtures(env: tuple[Path, Path]) -> None:
    ws, sstate = env
    cfg = _cfg(ws, sstate)
    results = [diagnostics.check_native_forecast(_cfg(ws, None)), diagnostics.check_native_forecast(cfg)]
    _record(ws, sstate)
    results.append(diagnostics.check_native_forecast(cfg))
    _advance(ws / "meta-avocado")
    results.append(diagnostics.check_native_forecast(cfg))
    _advance(ws / "bitbake", 2)
    (ws / "bitbake" / "f.txt").write_text("dirty\n")
    results.append(diagnostics.check_native_forecast(cfg))
    (ws / "bitbake" / ".git").rename(ws / "bitbake" / ".git.gone")
    results.append(diagnostics.check_native_forecast(cfg))
    assert all(r.severity is not diagnostics.Severity.BLOCK for r in results)
    assert results[-2].severity is diagnostics.Severity.WARN


def test_registry_membership() -> None:
    name = "native-rebuild-forecast"
    assert diagnostics.check_native_forecast in diagnostics.SHARED_CHECKS
    assert diagnostics.check_native_forecast in diagnostics._CACHE_TOUCHING_CHECKS
    groups = dict(diagnostics.CHECK_GROUPS)
    assert name in groups["Caches & storage"]
    meta = {fn: (n, sev) for fn, n, sev in diagnostics._CHECK_METADATA}
    assert meta[diagnostics.check_native_forecast] == (name, diagnostics.Severity.WARN)


_HOSTILE = "foo[/]bar[on red blink]x\x1b]0;PWNED\x07"


def test_artifact_text_is_neutralized(env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "1000")
    ws, sstate = env
    _record(ws, sstate)
    bucket = sstate / ".bakar" / "native-provenance" / "_unknown"
    rec = next(bucket.glob("*.json"))
    data = json.loads(rec.read_text())
    data["last_node"] = _HOSTILE
    data["last_run_id"] = _HOSTILE
    data["last_seen"] = _HOSTILE
    data["last_outcome"] = _HOSTILE
    rec.write_text(json.dumps(data))
    (bucket / "bad[on red blink]\x1b]0;x\x07.json").write_text("{not json")
    for mutate in (False, True):
        if mutate:
            _advance(ws / "meta-avocado")
        result = diagnostics.check_native_forecast(_cfg(ws, sstate))
        assert "\x1b" not in result.message
        other = diagnostics.CheckResult(
            name="x", severity=diagnostics.Severity.WARN, status=diagnostics.Status.FAIL, message="other"
        )
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            _print_diagnosis([result, other])  # must not raise MarkupError
        assert "foo[/]bar" in buf.getvalue()


@pytest.mark.parametrize("outcome", ["failed", "unknown", "None"])
def test_exact_match_non_success_outcome_is_not_called_built(env: tuple[Path, Path], outcome: str) -> None:
    ws, sstate = env
    _record(ws, sstate, outcome=outcome)
    result = diagnostics.check_native_forecast(_cfg(ws, sstate))
    assert result.status is diagnostics.Status.PASS
    assert result.severity is diagnostics.Severity.INFO
    assert "last built" not in result.message
    assert "last seen" in result.message
    assert f"outcome {outcome}" in result.message
    assert "no completed build is recorded for this revision set" in result.message


def test_exact_match_success_outcome_keeps_last_built(env: tuple[Path, Path]) -> None:
    ws, sstate = env
    _record(ws, sstate, outcome="success")
    result = diagnostics.check_native_forecast(_cfg(ws, sstate))
    assert "last built" in result.message


def test_exact_match_with_dirty_checkout_warns(env: tuple[Path, Path]) -> None:
    ws, sstate = env
    (ws / "bitbake" / "f.txt").write_text("edit-A\n")
    _record(ws, sstate)
    (ws / "bitbake" / "f.txt").write_text("edit-B, different\n")
    result = diagnostics.check_native_forecast(_cfg(ws, sstate))
    assert result.status is diagnostics.Status.FAIL
    assert result.severity is diagnostics.Severity.WARN
    assert "dirty, not forecastable: bitbake" in result.message
    assert "cannot be trusted" in result.message
    assert "uncommitted edits are not part of the revision set" in result.message


def test_bucket_name_matches_loader(env: tuple[Path, Path]) -> None:
    ws, sstate = env
    result = diagnostics.check_native_forecast(_cfg(ws, sstate))
    _record(ws, sstate)
    bucket = next((sstate / ".bakar" / "native-provenance").iterdir()).name
    assert f"recorded for {bucket} yet" in result.message
