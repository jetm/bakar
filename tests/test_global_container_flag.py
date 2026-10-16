"""Tests for the single global build-mode resolution helper."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

import bakar.commands._app as _state
from bakar.commands import _helpers
from bakar.commands._helpers import (
    GLOBAL_MODE_EXCLUSIONS,
    declare_global_mode_exclusion,
    resolve_global_config,
)
from bakar.config import BSPSpec, ResolveRequest, resolve

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _request(ws: Path) -> ResolveRequest:
    return ResolveRequest(workspace=ws, bsp_family="generic", kas_yaml=ws / "k.yml", spec=BSPSpec())


def test_global_container_flag_yields_container_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_state, "_CONTAINER_MODE", True)
    cfg = resolve_global_config(_request(tmp_path))
    assert cfg.host_mode is False


def test_no_global_flag_equals_plain_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_state, "_CONTAINER_MODE", False)
    monkeypatch.setattr(_state, "_HOST_MODE", False)
    req = _request(tmp_path)
    assert resolve_global_config(req) == resolve(req)


def test_global_host_flag_forces_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_state, "_HOST_MODE", True)
    assert resolve_global_config(_request(tmp_path)).host_mode is True


def test_request_without_spec_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_state, "_CONTAINER_MODE", True)
    req = replace(_request(tmp_path), spec=None)
    assert resolve_global_config(req).host_mode is False


def test_exclusion_registry_records_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_helpers, "GLOBAL_MODE_EXCLUSIONS", {})
    declare_global_mode_exclusion("demo", "never launches kas")
    assert _helpers.GLOBAL_MODE_EXCLUSIONS == {"demo": "never launches kas"}


def test_registry_is_exported() -> None:
    assert isinstance(GLOBAL_MODE_EXCLUSIONS, dict)
