"""The hashserv, prserv and feed commands share one daemon workspace resolver."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bakar.commands._workspace as workspace_mod
from bakar.commands import feed, hashserv, prserv
from bakar.commands._workspace import _resolve_daemon_cfg

if TYPE_CHECKING:
    from pathlib import Path

KAS_YAML = "header:\n  version: 14\nmachine: qemux86-64\nrepos:\n  poky:\n    url: https://example.invalid/poky\n"


def _capture(monkeypatch: pytest.MonkeyPatch) -> list:
    """Replace ``resolve`` as the shared helper sees it with a request recorder."""
    seen: list = []

    def fake_resolve(req):
        seen.append(req)
        return ("cfg", req)

    monkeypatch.setattr("bakar.config.resolve", fake_resolve)
    return seen


@pytest.mark.parametrize("mod", [hashserv, prserv, feed], ids=["hashserv", "prserv", "feed"])
def test_command_modules_use_the_shared_resolver(mod) -> None:
    assert mod._resolve_daemon_cfg is workspace_mod._resolve_daemon_cfg
    assert not hasattr(mod, "_resolve_cfg")


def test_resolves_generic_family_for_kas_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    yml = tmp_path / "my.yml"
    yml.write_text(KAS_YAML)
    seen = _capture(monkeypatch)

    _resolve_daemon_cfg(ws, yml)

    assert len(seen) == 1
    assert seen[0].bsp_family == "generic"
    assert seen[0].kas_yaml == yml


def test_resolves_nxp_family_for_default_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    seen = _capture(monkeypatch)

    _resolve_daemon_cfg(ws, None)

    assert len(seen) == 1
    assert seen[0].bsp_family == "nxp"
    assert seen[0].kas_yaml is None
