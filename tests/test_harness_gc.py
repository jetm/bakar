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
    next(hook)
    assert calls == ["disable"]
    with pytest.raises(StopIteration):
        hook.send(None)
    assert calls == ["disable", "enable", "collect", "freeze"]


def test_pause_restores_collector_when_collection_raises(monkeypatch):
    calls = _record_gc(monkeypatch)
    hook = HarnessGcPause().pytest_collection(session=None)
    next(hook)
    with pytest.raises(RuntimeError):
        hook.throw(RuntimeError("boom"))
    assert calls == ["disable", "enable", "collect", "freeze"]


def test_plugin_is_registered_by_name_and_blockable(pytestconfig):
    # Run with and without ``-p no:bakar_harness_gc``: the plugin is present
    # exactly when the run did not block it.
    blocked = "no:bakar_harness_gc" in pytestconfig.invocation_params.args
    assert pytestconfig.pluginmanager.has_plugin("bakar_harness_gc") is not blocked
