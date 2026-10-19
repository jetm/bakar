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
