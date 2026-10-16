"""The root callback publishes its flags as one frozen object, in sync with the per-flag globals."""

from __future__ import annotations

import pytest

from bakar.commands import _app
from bakar.output_mode import OutputMode

pytestmark = pytest.mark.unit

_GLOBAL_NAMES = (
    "_GLOBAL_OPTIONS",
    "_HIDE_DOCTOR_REPORT",
    "_HOST_MODE",
    "_CONTAINER_MODE",
    "_NO_SCOPE",
    "_SCCACHE_DIST",
    "_SCCACHE_SCHEDULER",
    "_MOLD",
    "_MOLD_BASELINE",
    "_MOLD_GLOBAL",
    "_OUTPUT_MODE_OVERRIDE",
    "_USER_CONFIG",
)


@pytest.fixture(autouse=True)
def _isolated_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _GLOBAL_NAMES:
        monkeypatch.setattr(_app, name, getattr(_app, name))
    monkeypatch.setattr(_app, "_load_user_config_safe", lambda: None)
    monkeypatch.setattr(_app, "_get_vendors", list)
    monkeypatch.setattr(_app, "_load_presets_safe", lambda: None)


def _call(**flags: object) -> None:
    kwargs: dict[str, object] = {
        "version": False,
        "hide_doctor_report": False,
        "host": False,
        "container": False,
        "no_scope": False,
        "sccache_dist": False,
        "sccache_scheduler": None,
        "mold": False,
        "mold_baseline": False,
        "mold_global": False,
        "plain": False,
        "rich_output": False,
    }
    kwargs.update(flags)
    _app._main(**kwargs)  # type: ignore[arg-type]


def _assert_in_sync() -> None:
    opts = _app._GLOBAL_OPTIONS
    assert opts.hide_doctor_report == _app._HIDE_DOCTOR_REPORT
    assert opts.host == _app._HOST_MODE
    assert opts.container == _app._CONTAINER_MODE
    assert opts.no_scope == _app._NO_SCOPE
    assert opts.sccache_dist == _app._SCCACHE_DIST
    assert opts.sccache_scheduler == _app._SCCACHE_SCHEDULER
    assert opts.mold == _app._MOLD
    assert opts.mold_baseline == _app._MOLD_BASELINE
    assert opts.mold_global == _app._MOLD_GLOBAL
    expected = OutputMode.PLAIN if opts.plain else (OutputMode.RICH if opts.rich_output else None)
    assert expected == _app._OUTPUT_MODE_OVERRIDE


def test_object_carries_each_flag() -> None:
    _call(
        hide_doctor_report=True,
        host=True,
        container=True,
        no_scope=True,
        sccache_dist=True,
        sccache_scheduler="http://s:1",
        mold=True,
        plain=True,
    )
    opts = _app._GLOBAL_OPTIONS
    assert opts.hide_doctor_report
    assert opts.host
    assert opts.container
    assert opts.no_scope
    assert opts.sccache_dist
    assert opts.sccache_scheduler == "http://s:1"
    assert opts.mold
    assert not opts.mold_baseline
    assert not opts.mold_global
    assert opts.plain
    assert not opts.rich_output
    _assert_in_sync()
    assert _app._OUTPUT_MODE_OVERRIDE is OutputMode.PLAIN


def test_second_invocation_replaces_first() -> None:
    _call(host=True, mold_global=True, mold_baseline=True, plain=True)
    _assert_in_sync()
    _call(container=True, rich_output=True, sccache_scheduler="x")
    opts = _app._GLOBAL_OPTIONS
    assert not opts.host
    assert not opts.mold_global
    assert not opts.plain
    assert opts.container
    assert opts.rich_output
    assert opts.sccache_scheduler == "x"
    _assert_in_sync()
    assert _app._OUTPUT_MODE_OVERRIDE is OutputMode.RICH
    _call()
    assert _app._OUTPUT_MODE_OVERRIDE is None
    _assert_in_sync()
