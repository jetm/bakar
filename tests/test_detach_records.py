"""Tests for bakar.detach_records."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from bakar import detach_records as dr

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def rt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tmp_path.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setitem(dr._state, "unit", None)
    return tmp_path


def _unit(tag: str = "a1b2c3", kind: str = "detach", day: str = "20260101") -> str:
    return f"bakar-{kind}-{day}-120000-{tag}"


def _finish(rt: Path, unit: str, age_days: float, rc: int = 0) -> None:
    p = rt / f"{unit}.log.rc"
    p.write_text(f"{rc}\n")
    (rt / f"{unit}.log").write_text("out")
    (rt / f"{unit}.runs").write_text("{}\n")
    t = 1_000_000_000 - age_days * 86400
    os.utime(p, (t, t))


def test_unit_names() -> None:
    assert dr.is_unit_name(dr.new_unit_name())
    assert dr.new_unit_name().startswith("bakar-detach-")
    assert dr.is_unit_name(_unit(kind="dispatch"))
    assert not dr.is_unit_name("x; rm -rf /")
    assert not dr.is_unit_name(_unit() + "\n")


def test_log_and_sentinel_live_under_runtime_dir(rt: Path) -> None:
    paths = dr.unit_paths(_unit())
    assert paths.log == rt / f"{_unit()}.log"
    assert paths.sentinel == rt / f"{_unit()}.log.rc"
    assert paths.registry == rt / f"{_unit()}.runs"
    assert dr.read_sentinel(_unit()) is None
    paths.sentinel.write_text("3\n")
    assert dr.read_sentinel(_unit()) == 3


@pytest.mark.parametrize("value", [None, "", "/nonexistent/dir/xyz"])
def test_runtime_dir_unavailable(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    else:
        monkeypatch.setenv("XDG_RUNTIME_DIR", value)
    with pytest.raises(dr.DetachUnavailable):
        dr.runtime_dir()


def test_runtime_dir_refuses_group_or_world_writable(rt: Path) -> None:
    rt.chmod(0o777)
    with pytest.raises(dr.DetachUnavailable, match="writable"):
        dr.runtime_dir()
    rt.chmod(0o770)
    with pytest.raises(dr.DetachUnavailable, match="writable"):
        dr.runtime_dir()


def test_runtime_dir_refuses_foreign_owner(rt: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dr.os, "getuid", lambda: os.stat(rt).st_uid + 1)
    with pytest.raises(dr.DetachUnavailable, match="another user"):
        dr.runtime_dir()


def test_runtime_dir_accepts_owned_private_dir(rt: Path) -> None:
    assert dr.runtime_dir() == rt


def test_registration_env_is_consumed_at_startup(rt: Path) -> None:
    env = {dr.REGISTRATION_ENV: "x; rm -rf /"}
    dr.consume_registration_env(env)
    assert dr.REGISTRATION_ENV not in env
    assert dr.current_unit() is None

    env = {dr.REGISTRATION_ENV: _unit()}
    dr.consume_registration_env(env)
    assert dr.REGISTRATION_ENV not in env
    assert dr.current_unit() == _unit()


def test_register_current_run_needs_unit(rt: Path) -> None:
    assert dr.register_current_run("r1", "/x", 1) is False
    assert list(rt.glob("*.runs")) == []
    dr.consume_registration_env({dr.REGISTRATION_ENV: _unit()})
    assert dr.register_current_run("r1", "/x", 1) is True
    assert dr.find_unit_for_run("r1") == _unit()
    assert dr.find_unit_for_run("nope") is None


def test_malformed_line_round_trip(rt: Path) -> None:
    unit = _unit()
    assert dr.append_registration(unit, "r1", rt / "run1", 11)
    with (rt / f"{unit}.runs").open("a") as fh:
        fh.write("not json\n[1]\n")
    assert dr.append_registration(unit, "r2", rt / "run2", 12)
    recs = dr.read_registrations(unit)
    assert [r["run_id"] for r in recs] == ["r1", "r2"]
    assert recs[1]["pid"] == 12


def test_append_registration_false_on_oserror(rt: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (rt / f"{_unit()}.runs").mkdir()
    assert dr.append_registration(_unit(), "r", "/x", 1) is False


def test_prune_skips_live_and_dispatch_entries(rt: Path) -> None:
    old = _unit("aaaaaa")
    live = _unit("bbbbbb")
    disp = _unit("cccccc", kind="dispatch")
    fresh = _unit("dddddd")
    _finish(rt, old, 8)
    _finish(rt, disp, 8)
    _finish(rt, fresh, 1)
    (rt / f"{live}.log").write_text("running")
    (rt / f"{live}.runs").write_text("{}\n")

    pruned = dr.prune(now=1_000_000_000)

    assert pruned == [old]
    assert not (rt / f"{old}.log").exists()
    assert not (rt / f"{old}.log.rc").exists()
    assert not (rt / f"{old}.runs").exists()
    for u in (live, disp, fresh):
        assert (rt / f"{u}.log").exists()
        assert (rt / f"{u}.runs").exists()
    assert (rt / f"{disp}.log.rc").exists()
    assert (rt / f"{fresh}.log.rc").exists()


def test_readers_tolerate_an_unusable_runtime_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert dr.read_sentinel(_unit()) is None
    assert dr.read_registrations(_unit()) == []


def test_runtime_dir_refuses_a_relative_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "run/user")
    with pytest.raises(dr.DetachUnavailable, match="absolute"):
        dr.runtime_dir()


def test_non_utf8_registry_does_not_break_lookups(rt: Path) -> None:
    (rt / f"{_unit('aaaaaa')}.runs").write_bytes(b"\xff\xfe garbage\n")
    assert dr.append_registration(_unit("bbbbbb"), "r9", rt / "run9", 9)
    assert dr.find_unit_for_run("r9") == _unit("bbbbbb")
    assert dr.read_registrations(_unit("aaaaaa")) == []


def test_registrations_with_wrong_types_are_skipped(rt: Path) -> None:
    unit = _unit()
    lines = [
        '{"run_id": "ok", "run_dir": "/x", "pid": 1}',
        '"a string"',
        "42",
        "null",
        '{"run_id": "bad1", "run_dir": 5, "pid": 1}',
        '{"run_id": "bad2", "run_dir": "/x", "pid": "1"}',
        '{"run_id": "bad3", "run_dir": "/x", "pid": true}',
        '{"run_id": 7, "run_dir": "/x", "pid": 1}',
    ]
    (rt / f"{unit}.runs").write_text("\n".join(lines) + "\n")
    assert [r["run_id"] for r in dr.read_registrations(unit)] == ["ok"]


def test_prune_removes_old_inactive_units_without_a_sentinel(rt: Path) -> None:
    now = 1_000_000_000
    stale = _unit("aaaaaa")
    active = _unit("bbbbbb")
    young = _unit("cccccc")
    disp = _unit("dddddd", kind="dispatch")
    for unit, age in ((stale, 8), (active, 8), (young, 1), (disp, 8)):
        for suffix in (".log", ".runs"):
            p = rt / f"{unit}{suffix}"
            p.write_text("x")
            t = now - age * 86400
            os.utime(p, (t, t))

    pruned = dr.prune(now=now, is_active=lambda u: u == active)

    assert pruned == [stale]
    assert not (rt / f"{stale}.log").exists()
    assert not (rt / f"{stale}.runs").exists()
    for u in (active, young, disp):
        assert (rt / f"{u}.log").exists()
        assert (rt / f"{u}.runs").exists()


def _touch(rt: Path, name: str, mtime: float) -> None:
    p = rt / name
    p.write_text("x")
    os.utime(p, (mtime, mtime))


def test_unit_listing_newest_unit_picks_newest_mtime(rt: Path) -> None:
    old = _unit("aaaaaa")
    new = _unit("bbbbbb", kind="dispatch")
    _touch(rt, f"{old}.log", 2_000)
    _touch(rt, f"{old}.runs", 3_000)
    _touch(rt, f"{new}.log", 4_000)
    _touch(rt, "bakar-detach-bogus.log", 9_000)
    _touch(rt, "other.runs", 9_000)

    assert dr.newest_unit() == new

    _touch(rt, f"{old}.runs", 5_000)
    assert dr.newest_unit() == old


def test_unit_listing_newest_unit_breaks_ties_by_name(rt: Path) -> None:
    a = _unit("aaaaaa")
    b = _unit("bbbbbb")
    _touch(rt, f"{a}.log", 1_000)
    _touch(rt, f"{b}.log", 1_000)
    assert dr.newest_unit() == b


def test_unit_listing_newest_unit_counts_a_sentinel_only_unit(rt: Path) -> None:
    unit = _unit("cccccc")
    _touch(rt, f"{unit}.log.rc", 1_000)
    assert dr.newest_unit() == unit


def test_unit_listing_newest_unit_ignores_non_unit_files(rt: Path) -> None:
    _touch(rt, "bakar-detach-nope.log", 1_000)
    _touch(rt, "random.log.rc", 1_000)
    assert dr.newest_unit() is None


def test_unit_listing_newest_unit_none_for_empty_dir(rt: Path) -> None:
    assert dr.newest_unit() is None


def test_unit_listing_none_without_runtime_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    calls: list[str] = []

    def probe(unit: str) -> bool:
        calls.append(unit)
        return True

    assert dr.newest_unit() is None
    assert dr.active_units(is_active=probe) == []
    assert calls == []


def test_unit_listing_active_units_filters_sentinel_and_probe(rt: Path) -> None:
    done = _unit("aaaaaa")
    live_b = _unit("bbbbbb", kind="dispatch")
    live_a = _unit("cccccc")
    idle = _unit("dddddd")
    (rt / f"{done}.log").write_text("x")
    (rt / f"{done}.log.rc").write_text("0\n")
    (rt / f"{live_b}.runs").write_text("{}\n")
    (rt / f"{live_a}.log").write_text("x")
    (rt / f"{idle}.log").write_text("x")
    (rt / "bakar-detach-bogus.log").write_text("x")

    calls: list[str] = []

    def probe(unit: str) -> bool:
        calls.append(unit)
        return unit != idle

    assert dr.active_units(is_active=probe) == sorted([live_a, live_b])
    assert done not in calls
    assert sorted(calls) == sorted([live_a, live_b, idle])


def test_unit_listing_active_units_defaults_to_systemctl(rt: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unit = _unit("eeeeee")
    (rt / f"{unit}.log").write_text("x")
    monkeypatch.setattr(dr, "systemctl_unit_active", lambda u: u == unit)
    assert dr.active_units() == [unit]
