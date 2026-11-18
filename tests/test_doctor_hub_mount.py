"""Doctor row dl-mirror-paths-ready (spec cache-sharing-doctor-checks)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from bakar import diagnostics
from bakar.diagnostics import Severity, Status, check_dl_mirror_paths_ready
from bakar.steps import hub_mount
from tests.conftest import make_build_config

if TYPE_CHECKING:
    import pytest


def _cfg(**kw: object):
    return make_build_config(workspace=Path("/nonexistent-ws"), **kw)


def _probe(monkeypatch: pytest.MonkeyPatch, states: dict[str, str]) -> list[list[Path]]:
    calls: list[list[Path]] = []

    def fake(dirs):
        calls.append(list(dirs))
        return [hub_mount.DirStatus(path=d, state=states.get(str(d), "ready"), detail="") for d in dirs]

    monkeypatch.setattr(hub_mount, "probe_dirs", fake)
    return calls


def test_all_ready_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _probe(monkeypatch, {})
    r = check_dl_mirror_paths_ready(_cfg(dl_mirror_paths="/a /b", host_mode=True))
    assert r.status == Status.PASS
    assert "/a" in r.message
    assert "/b" in r.message


def test_missing_directory_warns_and_names_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _probe(monkeypatch, {"/b": "missing"})
    r = check_dl_mirror_paths_ready(_cfg(dl_mirror_paths="/a /b", host_mode=True))
    assert r.status == Status.FAIL
    assert r.severity == Severity.WARN
    assert "/b" in r.message
    assert "missing" in r.message
    assert "/a" not in r.message


def test_unresponsive_directory_is_never_block(monkeypatch: pytest.MonkeyPatch) -> None:
    _probe(monkeypatch, {"/a": "unresponsive"})
    r = check_dl_mirror_paths_ready(_cfg(dl_mirror_paths="/a", host_mode=True))
    assert r.status == Status.FAIL
    assert r.severity != Severity.BLOCK
    assert "unresponsive" in r.message


def test_key_unset_skips_without_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _probe(monkeypatch, {})
    r = check_dl_mirror_paths_ready(_cfg(dl_mirror_paths=None, host_mode=True))
    assert r.status == Status.SKIP
    assert calls == []


def test_container_mode_skips_with_info(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _probe(monkeypatch, {})
    r = check_dl_mirror_paths_ready(_cfg(dl_mirror_paths="/a", host_mode=False))
    assert r.status == Status.SKIP
    assert r.severity == Severity.INFO
    assert calls == []


def test_row_registered() -> None:
    name = "dl-mirror-paths-ready"
    assert name in {n for _g, ns in diagnostics.CHECK_GROUPS for n in ns}
    assert diagnostics._CHECK_SEVERITY[name] == Severity.WARN
    assert check_dl_mirror_paths_ready in diagnostics.SHARED_CHECKS
