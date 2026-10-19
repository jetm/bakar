"""Tests for bakar.host_snapshot.measure."""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar import host_snapshot


@pytest.fixture
def meminfo(tmp_path, monkeypatch):
    p = tmp_path / "meminfo"
    p.write_text("MemTotal:       16000000 kB\nMemAvailable:    2097152 kB\n")
    monkeypatch.setattr(host_snapshot, "MEMINFO_PATH", p)
    return p


@pytest.fixture
def psi_values(monkeypatch):
    vals = {"cpu": 1.5, "io": 2.5, "memory": 0.0}
    monkeypatch.setattr(host_snapshot.psi, "read_psi_avg10", lambda r: vals[r])
    return vals


def _probe_returning(result, calls):
    def fake(paths, *, deadline_s):
        calls.append((list(paths), deadline_s))
        return dict.fromkeys(paths, result)

    return fake


def test_exact_keys(meminfo, psi_values, monkeypatch):
    monkeypatch.setattr(host_snapshot.mounts, "probe_free_bytes", _probe_returning(1610612736, []))
    out = host_snapshot.measure(Path("/b"), Path("/s"))
    assert set(out) == {"disk_free_gb", "mem_available_mb", "pressure"}
    assert set(out["disk_free_gb"]) == {"build", "sstate"}
    assert set(out["pressure"]) == {"cpu", "io", "memory"}
    assert out["mem_available_mb"] == 2048
    assert out["pressure"] == {"cpu": 1.5, "io": 2.5, "memory": 0.0}


def test_gib_rounding(meminfo, psi_values, monkeypatch):
    monkeypatch.setattr(host_snapshot.mounts, "probe_free_bytes", _probe_returning(1610612736, []))
    out = host_snapshot.measure(Path("/b"), Path("/s"))
    assert out["disk_free_gb"] == {"build": 1.5, "sstate": 1.5}


def test_none_path_never_probed(meminfo, psi_values, monkeypatch):
    calls: list = []
    monkeypatch.setattr(host_snapshot.mounts, "probe_free_bytes", _probe_returning(1073741824, calls))
    out = host_snapshot.measure(Path("/b"), None, deadline_s=1.25)
    assert calls == [([Path("/b")], 1.25)]
    assert out["disk_free_gb"] == {"build": 1.0, "sstate": None}
    calls.clear()
    out = host_snapshot.measure(None, None)
    assert calls == []
    assert out["disk_free_gb"] == {"build": None, "sstate": None}


def test_none_result_reports_none(meminfo, psi_values, monkeypatch):
    monkeypatch.setattr(host_snapshot.mounts, "probe_free_bytes", _probe_returning(None, []))
    out = host_snapshot.measure(Path("/b"), Path("/s"))
    assert out["disk_free_gb"] == {"build": None, "sstate": None}


def test_unreadable_meminfo_gives_none(tmp_path, psi_values, monkeypatch):
    monkeypatch.setattr(host_snapshot, "MEMINFO_PATH", tmp_path / "absent")
    monkeypatch.setattr(host_snapshot.mounts, "probe_free_bytes", _probe_returning(1, []))
    assert host_snapshot.measure(Path("/b"), None)["mem_available_mb"] is None


def test_meminfo_without_field_gives_none(tmp_path, psi_values, monkeypatch):
    p = tmp_path / "meminfo"
    p.write_text("MemTotal: 1 kB\n")
    monkeypatch.setattr(host_snapshot, "MEMINFO_PATH", p)
    assert host_snapshot.measure(None, None)["mem_available_mb"] is None


def test_psi_absent_gives_three_nulls(meminfo, monkeypatch):
    monkeypatch.setattr(host_snapshot.psi, "read_psi_avg10", lambda r: None)
    out = host_snapshot.measure(None, None)
    assert out["pressure"] == {"cpu": None, "io": None, "memory": None}


def test_raising_probe_gives_null_disk(meminfo, psi_values, monkeypatch):
    def boom(paths, *, deadline_s):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(host_snapshot.mounts, "probe_free_bytes", boom)
    out = host_snapshot.measure(Path("/b"), Path("/s"))
    assert out["disk_free_gb"] == {"build": None, "sstate": None}
