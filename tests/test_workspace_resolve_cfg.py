"""The shared daemon workspace resolver matches the per-command ``_resolve_cfg`` copies."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bakar.config as config_mod
from bakar.commands import feed, hashserv, prserv
from bakar.commands._workspace import _resolve_daemon_cfg

if TYPE_CHECKING:
    from pathlib import Path

KAS_YAML = "header:\n  version: 14\nmachine: qemux86-64\nrepos:\n  poky:\n    url: https://example.invalid/poky\n"


def _capture(monkeypatch: pytest.MonkeyPatch) -> list:
    """Replace ``config.resolve`` (used by every copy) with a request recorder."""
    seen: list = []

    def fake_resolve(req):
        seen.append(req)
        return ("cfg", req)

    monkeypatch.setattr(config_mod, "resolve", fake_resolve)
    # The three command modules bound ``resolve`` at import time.
    for mod in (hashserv, prserv, feed):
        monkeypatch.setattr(mod, "resolve", fake_resolve)
    return seen


@pytest.mark.parametrize("old", [hashserv, prserv, feed], ids=["hashserv", "prserv", "feed"])
def test_matches_existing_copy_for_kas_yaml(old, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    yml = tmp_path / "my.yml"
    yml.write_text(KAS_YAML)
    seen = _capture(monkeypatch)

    expected = old._resolve_cfg(ws, yml)
    actual = _resolve_daemon_cfg(ws, yml)

    assert actual == expected
    assert len(seen) == 2
    assert seen[0] == seen[1]
    assert seen[1].bsp_family == "generic"
    assert seen[1].kas_yaml == yml


@pytest.mark.parametrize("old", [hashserv, prserv, feed], ids=["hashserv", "prserv", "feed"])
def test_matches_existing_copy_for_default_manifest(old, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    seen = _capture(monkeypatch)

    expected = old._resolve_cfg(ws, None)
    actual = _resolve_daemon_cfg(ws, None)

    assert actual == expected
    assert seen[0] == seen[1]
    assert seen[1].bsp_family == "nxp"
    assert seen[1].kas_yaml is None
