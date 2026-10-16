"""The root callback publishes its flags as one frozen object."""

from __future__ import annotations

import pytest

from bakar.commands import _app

pytestmark = pytest.mark.unit

_GLOBAL_NAMES = ("_GLOBAL_OPTIONS", "_USER_CONFIG")


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


def test_second_invocation_replaces_first() -> None:
    _call(host=True, mold_global=True, mold_baseline=True, plain=True)
    _call(container=True, rich_output=True, sccache_scheduler="x")
    opts = _app._GLOBAL_OPTIONS
    assert not opts.host
    assert not opts.mold_global
    assert not opts.plain
    assert opts.container
    assert opts.rich_output
    assert opts.sccache_scheduler == "x"
    _call()
    assert not _app._GLOBAL_OPTIONS.plain
    assert not _app._GLOBAL_OPTIONS.rich_output
