"""Tests for the host-provided uninative overlay selection helpers.

Covers ``_host_is_arch_like`` os-release parsing and the three-way gate in
``_uninative_extra_overlays`` (host mode, Arch-family host, fragment present),
plus the overlay's presence in ``_tuning_extra_overlays``.

Every test that exercises the gate patches both the os-release path and the
fragment path, so the result does not depend on the distro running the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar.config import BuildConfig


def _uninative_cfg(*, host_mode: bool = True, uninative: bool = True) -> BuildConfig:
    """Return a minimal BuildConfig for the uninative overlay helper tests."""
    return BuildConfig(
        workspace=Path("/tmp"),
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        kas_container_image="img:latest",
        host_mode=host_mode,
        uninative=uninative,
    )


def _patch_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    os_release: str | None,
    fragment: bool,
) -> None:
    """Point the uninative gate at fixture files instead of the real host."""
    from bakar.commands import _helpers

    if os_release is None:
        os_release_path = tmp_path / "absent-os-release"
    else:
        os_release_path = tmp_path / "os-release"
        os_release_path.write_text(os_release, encoding="utf-8")
    monkeypatch.setattr(_helpers, "_UNINATIVE_OS_RELEASE", os_release_path)

    fragment_path = tmp_path / "uninative.inc"
    if fragment:
        fragment_path.write_text('UNINATIVE_URL = "file:///x/"\n', encoding="utf-8")
    monkeypatch.setattr(_helpers, "_UNINATIVE_FRAGMENT", fragment_path)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("os_release", "expected"),
    [
        ("ID=arch\n", True),
        ("ID=cachyos\nID_LIKE=arch\n", True),
        ('ID=endeavouros\nID_LIKE="arch"\n', True),
        ("ID=ubuntu\nID_LIKE=debian\n", False),
        ("ID=fedora\n", False),
        ('NAME="No IDs here"\n', False),
    ],
)
def test_host_is_arch_like_parses_os_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, os_release: str, expected: bool
) -> None:
    """ID or ID_LIKE containing 'arch' identifies the Arch family; others do not."""
    from bakar.commands._helpers import _host_is_arch_like

    _patch_env(monkeypatch, tmp_path, os_release=os_release, fragment=True)

    assert _host_is_arch_like() is expected


@pytest.mark.unit
def test_host_is_arch_like_false_when_os_release_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unreadable /etc/os-release fails closed rather than raising."""
    from bakar.commands._helpers import _host_is_arch_like

    _patch_env(monkeypatch, tmp_path, os_release=None, fragment=True)

    assert _host_is_arch_like() is False


@pytest.mark.unit
def test_uninative_overlay_selected_when_all_conditions_met(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Host mode + Arch-family host + fragment present yields the overlay."""
    from bakar.commands._helpers import _uninative_extra_overlays

    _patch_env(monkeypatch, tmp_path, os_release="ID=cachyos\nID_LIKE=arch\n", fragment=True)

    result = _uninative_extra_overlays(_uninative_cfg(host_mode=True))

    assert len(result) == 1
    assert result[0].name == "bakar-tuning-uninative.yml"
    assert result[0].is_file(), "overlay file must exist in the installed overlays/ dir"


@pytest.mark.unit
def test_uninative_overlay_skipped_in_container_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Container builds must not require a fragment holding host-only paths."""
    from bakar.commands._helpers import _uninative_extra_overlays

    _patch_env(monkeypatch, tmp_path, os_release="ID=arch\n", fragment=True)

    assert _uninative_extra_overlays(_uninative_cfg(host_mode=False)) == []


@pytest.mark.unit
def test_uninative_overlay_skipped_on_non_arch_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A Debian-family host gets no overlay even if a fragment somehow exists."""
    from bakar.commands._helpers import _uninative_extra_overlays

    _patch_env(monkeypatch, tmp_path, os_release="ID=ubuntu\nID_LIKE=debian\n", fragment=True)

    assert _uninative_extra_overlays(_uninative_cfg(host_mode=True)) == []


@pytest.mark.unit
def test_uninative_overlay_skipped_when_package_not_installed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No fragment means the providing package is absent - skip, do not require."""
    from bakar.commands._helpers import _uninative_extra_overlays

    _patch_env(monkeypatch, tmp_path, os_release="ID=arch\n", fragment=False)

    assert _uninative_extra_overlays(_uninative_cfg(host_mode=True)) == []


@pytest.mark.unit
def test_uninative_overlay_in_tuning_stack(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_tuning_extra_overlays includes the overlay when the gate passes."""
    from bakar.commands._helpers import _tuning_extra_overlays

    _patch_env(monkeypatch, tmp_path, os_release="ID=arch\n", fragment=True)

    names = [p.name for p in _tuning_extra_overlays(_uninative_cfg(host_mode=True))]

    assert "bakar-tuning-uninative.yml" in names


@pytest.mark.unit
def test_uninative_overlay_absent_from_tuning_stack_on_non_arch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tuning stack is unchanged on a non-Arch host (falsifier)."""
    from bakar.commands._helpers import _tuning_extra_overlays

    _patch_env(monkeypatch, tmp_path, os_release="ID=debian\n", fragment=True)

    names = [p.name for p in _tuning_extra_overlays(_uninative_cfg(host_mode=True))]

    assert "bakar-tuning-uninative.yml" not in names


@pytest.mark.unit
def test_uninative_overlay_skipped_when_toggle_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Default-off toggle keeps the tuning stack byte-identical on an Arch host."""
    from bakar.commands._helpers import _uninative_extra_overlays

    _patch_env(monkeypatch, tmp_path, os_release="ID=cachyos\nID_LIKE=arch\n", fragment=True)

    assert _uninative_extra_overlays(_uninative_cfg(host_mode=True, uninative=False)) == []
