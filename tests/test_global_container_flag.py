"""Tests for the single global build-mode resolution helper."""

from __future__ import annotations

from dataclasses import replace
from importlib import import_module
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

    from typer.testing import CliRunner

pytestmark = pytest.mark.unit


def _request(ws: Path) -> ResolveRequest:
    return ResolveRequest(workspace=ws, bsp_family="generic", kas_yaml=ws / "k.yml", spec=BSPSpec())


def test_global_container_flag_yields_container_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_state, "_GLOBAL_OPTIONS", replace(_state._GLOBAL_OPTIONS, container=True))
    cfg = resolve_global_config(_request(tmp_path))
    assert cfg.host_mode is False


def test_no_global_flag_equals_plain_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_state, "_GLOBAL_OPTIONS", replace(_state._GLOBAL_OPTIONS, container=False, host=False))
    req = _request(tmp_path)
    assert resolve_global_config(req) == resolve(req)


def test_global_host_flag_forces_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_state, "_GLOBAL_OPTIONS", replace(_state._GLOBAL_OPTIONS, host=True))
    assert resolve_global_config(_request(tmp_path)).host_mode is True


def test_request_without_spec_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_state, "_GLOBAL_OPTIONS", replace(_state._GLOBAL_OPTIONS, container=True))
    req = replace(_request(tmp_path), spec=None)
    assert resolve_global_config(req).host_mode is False


def test_exclusion_registry_records_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_helpers, "GLOBAL_MODE_EXCLUSIONS", {})
    declare_global_mode_exclusion("demo", "never launches kas")
    assert _helpers.GLOBAL_MODE_EXCLUSIONS == {"demo": "never launches kas"}


def test_registry_is_exported() -> None:
    assert isinstance(GLOBAL_MODE_EXCLUSIONS, dict)


class _StoppedError(Exception):
    """Raised by the recording wrapper to halt a command right after resolution."""


_MANIFEST = "imx-6.6.52-2.2.0.xml"

# command name -> (module holding the resolve_global_config binding, argv after the global flag)
_WALKED: dict[str, tuple[str, list[str]]] = {
    "graph": ("graph", ["graph", "busybox"]),
    "inspect": ("inspect", ["inspect", "busybox"]),
    "prefetch": ("prefetch", ["prefetch"]),
    "lock": ("lock", ["lock"]),
    "dump": ("dump", ["dump"]),
    "getvar": ("getvar", ["getvar", "MACHINE"]),
    "bitbake": ("bitbake", ["bitbake", "busybox"]),
    "shell": ("shell", ["shell"]),
    "stress-parse": ("stress_parse", ["stress-parse"]),
    "diffsigs": ("diffsigs", ["diffsigs", "busybox", "do_compile"]),
    "layers": ("layers", ["layers"]),
}


@pytest.mark.parametrize("command", sorted(_WALKED))
def test_kas_launching_command_honours_global_container(
    command: str, monkeypatch: pytest.MonkeyPatch, runner: CliRunner, nxp_workspace: Path
) -> None:
    from bakar.cli import app

    module_name, argv = _WALKED[command]
    module = import_module(f"bakar.commands.{module_name}")
    assert command not in _helpers.GLOBAL_MODE_EXCLUSIONS
    real = module.resolve_global_config
    seen: list[bool] = []

    def recording(request: ResolveRequest) -> object:
        cfg = real(request)
        seen.append(cfg.host_mode)
        raise _StoppedError

    monkeypatch.setattr(module, "resolve_global_config", recording)
    monkeypatch.setattr(_state, "_GLOBAL_OPTIONS", replace(_state._GLOBAL_OPTIONS, container=False, host=False))
    result = runner.invoke(app, ["--container", *argv, "--manifest", _MANIFEST, "--workspace", str(nxp_workspace)])

    assert isinstance(result.exception, _StoppedError), (result.output, result.exception)
    assert seen == [False]


def test_run_is_a_declared_exclusion(monkeypatch: pytest.MonkeyPatch) -> None:
    """``bakar run`` still resolves without the helper; the exclusion is recorded, not implied."""
    monkeypatch.setattr(_helpers, "GLOBAL_MODE_EXCLUSIONS", {})
    declare_global_mode_exclusion("run", "resolves without resolve_global_config; migrated separately")
    assert "run" in _helpers.GLOBAL_MODE_EXCLUSIONS
    assert not set(_WALKED) & set(_helpers.GLOBAL_MODE_EXCLUSIONS)
