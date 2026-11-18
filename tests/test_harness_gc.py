"""Tests for the collection garbage-collection pause plugin in ``tests/conftest.py``."""

from __future__ import annotations

import gc

import pytest

from tests.conftest import HarnessGcPause


def _record_gc(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(gc, "disable", lambda: calls.append("disable"))
    monkeypatch.setattr(gc, "enable", lambda: calls.append("enable"))
    monkeypatch.setattr(gc, "collect", lambda *a: calls.append("collect") or 0)
    monkeypatch.setattr(gc, "freeze", lambda: calls.append("freeze"))
    return calls


def test_pause_disables_during_collection_then_enables_and_freezes(monkeypatch):
    calls = _record_gc(monkeypatch)
    hook = HarnessGcPause().pytest_collection(session=None)
    try:
        next(hook)
        assert calls == ["disable"]
        with pytest.raises(StopIteration):
            hook.send(None)
        assert calls == ["disable", "enable", "collect", "freeze"]
    finally:
        # A failed assertion must not leave the generator suspended: its own
        # ``finally`` would then run against the real gc in a later test.
        hook.close()


def test_pause_restores_collector_when_collection_raises(monkeypatch):
    calls = _record_gc(monkeypatch)
    hook = HarnessGcPause().pytest_collection(session=None)
    try:
        next(hook)
        with pytest.raises(RuntimeError):
            hook.throw(RuntimeError("boom"))
        assert calls == ["disable", "enable", "collect", "freeze"]
    finally:
        hook.close()


def test_plugin_is_registered_by_name_and_blockable(pytestconfig):
    # Run with and without ``-p no:bakar_harness_gc``: the plugin is present
    # exactly when the run did not block it.
    blocked = pytestconfig.pluginmanager.is_blocked("bakar_harness_gc")
    assert pytestconfig.pluginmanager.has_plugin("bakar_harness_gc") is not blocked
