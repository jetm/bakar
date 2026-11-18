"""Tests for bakar.steps.hub_mount."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bakar import mounts
from bakar.steps import hub_mount

if TYPE_CHECKING:
    from collections.abc import Sequence


@pytest.fixture(autouse=True)
def _clear_memo() -> None:
    hub_mount._PROBE_MEMO.clear()


def _fake(monkeypatch: pytest.MonkeyPatch, table: dict[str, tuple[str, str]]) -> list[list[Path]]:
    calls: list[list[Path]] = []

    def fake(paths: Sequence[Path], *, deadline_s: float) -> dict[Path, tuple[str, str]]:
        calls.append(list(paths))
        return {p: table[str(p)] for p in paths if str(p) in table}

    monkeypatch.setattr(mounts, "probe_statfs", fake)
    return calls


def test_parse_dirs_order_and_duplicates() -> None:
    assert hub_mount.parse_dirs(" /b  /a\t/b /c ") == [Path("/b"), Path("/a"), Path("/c")]
    assert hub_mount.parse_dirs(None) == []
    assert hub_mount.parse_dirs("") == []


def test_host_mode_false_never_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake(monkeypatch, {"/a": ("ready", "")})
    assert hub_mount.ready_dirs("/a", host_mode=False) == []
    assert hub_mount.dropped_dirs("/a", host_mode=False) == []
    assert calls == []


def test_empty_value_never_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake(monkeypatch, {})
    assert hub_mount.ready_dirs(None, host_mode=True) == []
    assert hub_mount.ready_dirs("  ", host_mode=True) == []
    assert calls == []


def test_batch_call_and_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake(monkeypatch, {"/a": ("ready", ""), "/b": ("ready", ""), "/c": ("ready", "")})
    assert hub_mount.ready_dirs("/a /b", host_mode=True) == [Path("/a"), Path("/b")]
    assert calls == [[Path("/a"), Path("/b")]]
    assert hub_mount.ready_dirs("/a /b", host_mode=True) == [Path("/a"), Path("/b")]
    assert len(calls) == 1
    assert hub_mount.ready_dirs("/b /c", host_mode=True) == [Path("/b"), Path("/c")]
    assert calls[1:] == [[Path("/c")]]


def test_classification_and_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(
        monkeypatch,
        {
            "/ok1": ("ready", ""),
            "/gone": ("missing", "No such file or directory"),
            "/hung": ("unresponsive", ""),
            "/bad": ("error", "Permission denied"),
            "/ok2": ("ready", ""),
        },
    )
    value = "/ok1 /gone /hung /bad /ok2"
    assert hub_mount.ready_dirs(value, host_mode=True) == [Path("/ok1"), Path("/ok2")]
    assert hub_mount.dropped_dirs(value, host_mode=True) == [
        hub_mount.DirStatus(Path("/gone"), "missing", "No such file or directory"),
        hub_mount.DirStatus(Path("/hung"), "unresponsive", ""),
        hub_mount.DirStatus(Path("/bad"), "error", "Permission denied"),
    ]


def test_path_absent_from_probe_result_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, {})
    assert [s.state for s in hub_mount.probe_dirs([Path("/x")])] == ["error"]


def test_all_dropped_yields_empty_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, {"/a": ("missing", "m"), "/b": ("unresponsive", "")})
    assert hub_mount.ready_dirs("/a /b", host_mode=True) == []
    assert len(hub_mount.dropped_dirs("/a /b", host_mode=True)) == 2
