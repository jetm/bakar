"""Tests for the single global build-mode resolution helper."""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import replace
from importlib import import_module
from typing import TYPE_CHECKING

import pytest
import typer.main

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
    assert GLOBAL_MODE_EXCLUSIONS
    assert all(reason.strip() for reason in GLOBAL_MODE_EXCLUSIONS.values())


class _StoppedError(Exception):
    """Raised by the recording wrapper to halt a command right after resolution."""


_MANIFEST = "imx-6.6.52-2.2.0.xml"

# command name -> (module holding the resolve_global_config binding, argv after the global flag).
# Every argv here is invoked with the manifest/workspace options appended unless
# the command is listed in _NO_MANIFEST_OPTION.
_WALKED: dict[str, tuple[str, list[str]]] = {
    "graph": ("graph", ["graph", "busybox"]),
    "inspect": ("inspect", ["inspect", "busybox"]),
    "prefetch": ("prefetch", ["prefetch"]),
    "lock": ("lock", ["lock"]),
    "dump": ("dump", ["dump"]),
    "getvar": ("getvar", ["getvar", "MACHINE"]),
    "bitbake": ("bitbake", ["bitbake", "busybox"]),
    "clean-recipe": ("bitbake", ["clean-recipe", "busybox"]),
    "rebuild": ("bitbake", ["rebuild", "busybox"]),
    "shell": ("shell", ["shell"]),
    "stress-parse": ("stress_parse", ["stress-parse"]),
    "diffsigs": ("diffsigs", ["diffsigs", "busybox", "do_compile"]),
    "layers": ("layers", ["layers"]),
    "show": ("show", ["show"]),
    "sync": ("sync", ["sync"]),
    "clean": ("clean", ["clean"]),
    "diff": ("diff", ["diff", "a.txt", "b.txt"]),
    "doctor": ("doctor", ["doctor"]),
    "monitor": ("monitor", ["monitor"]),
}
_NO_MANIFEST_OPTION = frozenset({"monitor"})

# Commands whose behaviour is exercised in tests/test_cli_build*.py; the walk
# here only requires that their module reaches the helper.
_STRUCTURAL = {"build": "build"}


def _registered_commands() -> dict[str, object]:
    """Top-level commands (and groups) as the CLI registers them, by name."""
    import bakar.cli  # noqa: F401 - registers every subcommand

    return dict(typer.main.get_command(_state.app).commands)


def _calls_plain_resolve(callback: object) -> bool:
    """True when the callback body calls the bare name ``resolve(...)`` (config.resolve)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(callback)))  # type: ignore[arg-type]
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "resolve"
        for node in ast.walk(tree)
    )


def _callback_module(command: object) -> str | None:
    callback = getattr(command, "callback", None)
    return callback.__module__ if callback is not None else None


def test_every_registered_command_is_walked_or_declared_excluded() -> None:
    """The walk is built from the CLI itself, so a new command cannot be forgotten."""
    registered = set(_registered_commands())
    walked = set(_WALKED) | set(_STRUCTURAL)
    excluded = set(_helpers.GLOBAL_MODE_EXCLUSIONS)

    assert registered - walked - excluded == set(), "new command: reach resolve_global_config or declare an exclusion"
    assert walked & excluded == set()
    assert (walked | excluded) - registered == set(), "stale entry names a command that no longer exists"


def test_walked_commands_reach_the_helper_and_excluded_ones_do_not_call_plain_resolve() -> None:
    commands = _registered_commands()
    for name, command in commands.items():
        module_name = _callback_module(command)
        if module_name is None:
            continue
        module = import_module(module_name)
        if name in _WALKED or name in _STRUCTURAL:
            assert hasattr(module, "resolve_global_config"), name
            assert command.callback is not None
            assert not _calls_plain_resolve(command.callback), f"{name} calls plain resolve()"
        else:
            # Declared exclusions may keep the legacy plain resolve(); an
            # undeclared command cannot (the membership test above catches it).
            assert name in _helpers.GLOBAL_MODE_EXCLUSIONS or not _calls_plain_resolve(command.callback), name


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
    extra = [] if command in _NO_MANIFEST_OPTION else ["--manifest", _MANIFEST]
    result = runner.invoke(app, ["--container", *argv, *extra, "--workspace", str(nxp_workspace)])

    assert isinstance(result.exception, _StoppedError), (result.output, result.exception)
    assert seen == [False]
