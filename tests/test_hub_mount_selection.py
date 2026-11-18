"""Hub-mount overlay selection: gating, probe avoidance and one warning per dropped directory."""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bakar.commands import _overlays
from bakar.commands._overlays import _tuning_extra_overlays
from bakar.config import BuildConfig
from bakar.steps import hub_mount

if TYPE_CHECKING:
    from collections.abc import Sequence

pytestmark = pytest.mark.unit

_OVERLAY_DIR = Path(str(importlib.resources.files("bakar") / "overlays"))
_MOUNT = _OVERLAY_DIR / "bakar-tuning-hub-mount.yml"
_CACHE = _OVERLAY_DIR / "bakar-tuning-hub-cache.yml"


def _cfg(
    tmp_path: Path,
    *,
    dl_mirror_paths: str | None = "/mnt/a /mnt/b",
    dl_mirror_url: str | None = None,
    host_mode: bool = True,
) -> BuildConfig:
    return BuildConfig(
        workspace=tmp_path,
        bsp_family="nxp",
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        kas_container_image="img:latest",
        dl_mirror_paths=dl_mirror_paths,
        dl_mirror_url=dl_mirror_url,
        host_mode=host_mode,
    )


@pytest.fixture(autouse=True)
def _clear_memos() -> None:
    hub_mount._PROBE_MEMO.clear()
    _overlays._HUB_MOUNT_WARNED.clear()
    _overlays._HUB_PROBE_MEMO.clear()


class _Probe:
    """Stand-in for ``mounts.probe_statfs`` that records calls."""

    def __init__(self, states: dict[str, str]) -> None:
        self.states = states
        self.calls: list[list[Path]] = []

    def __call__(self, dirs: Sequence[Path], **_kw: object) -> dict[Path, tuple[str, str]]:
        self.calls.append(list(dirs))
        return {d: (self.states.get(str(d), "ready"), "") for d in dirs}


def _install(monkeypatch: pytest.MonkeyPatch, **states: str) -> _Probe:
    probe = _Probe({f"/mnt/{k}": v for k, v in states.items()})
    monkeypatch.setattr("bakar.mounts.probe_statfs", probe)
    return probe


def _warnings(capsys: pytest.CaptureFixture[str]) -> list[str]:
    out = capsys.readouterr()
    return [ln for ln in (out.out + out.err).splitlines() if "hub mirror directory" in ln]


def test_ready_directories_include_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)
    assert _MOUNT in _tuning_extra_overlays(_cfg(tmp_path))


def test_key_unset_yields_no_overlay_and_no_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _install(monkeypatch)
    assert _MOUNT not in _tuning_extra_overlays(_cfg(tmp_path, dl_mirror_paths=None))
    assert probe.calls == []


def test_container_mode_yields_no_overlay_and_no_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _install(monkeypatch)
    assert _MOUNT not in _tuning_extra_overlays(_cfg(tmp_path, host_mode=False))
    assert probe.calls == []


def test_one_missing_directory_warns_once_and_others_are_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, a="missing")
    assert _MOUNT in _tuning_extra_overlays(_cfg(tmp_path))
    warnings = _warnings(capsys)
    assert len(warnings) == 1
    assert "/mnt/a" in warnings[0]
    assert "missing" in warnings[0]
    assert "/mnt/b" not in warnings[0]


def test_every_directory_dropped_yields_no_overlay_and_one_warning_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, a="missing", b="unresponsive")
    assert _MOUNT not in _tuning_extra_overlays(_cfg(tmp_path))
    warnings = _warnings(capsys)
    assert len(warnings) == 2
    assert any("/mnt/a" in w and "missing" in w for w in warnings)
    assert any("/mnt/b" in w and "unresponsive" in w for w in warnings)


def test_resolving_twice_warns_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, a="missing")
    cfg = _cfg(tmp_path)
    _tuning_extra_overlays(cfg)
    _tuning_extra_overlays(cfg)
    assert len(_warnings(capsys)) == 1


def test_second_resolution_does_not_probe_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _install(monkeypatch)
    cfg = _cfg(tmp_path)
    _tuning_extra_overlays(cfg)
    _tuning_extra_overlays(cfg)
    assert len(probe.calls) == 1


def test_mount_overlay_present_without_hub_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)
    overlays = _tuning_extra_overlays(_cfg(tmp_path, dl_mirror_url=None))
    assert _MOUNT in overlays
    assert _CACHE not in overlays
