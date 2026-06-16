"""Tests for the read-only host profiler in ``bakar.setup.profile``.

Covers the distro->package-manager mapping (including unknown distros),
docker-installed detection, and that :func:`HostProfile.detect` performs no
mutating operation - asserted by monkeypatching ``subprocess.run`` and
proving it is never called during detection.
"""

from __future__ import annotations

import grp
import subprocess
from typing import TYPE_CHECKING

import pytest

from bakar.setup import profile as profile_mod
from bakar.setup.profile import HostProfile, _detect_pkg_manager, _parse_os_release

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("os_release", "expected_id", "expected_mgr"),
    [
        ("ID=arch\n", "arch", "pacman"),
        ("ID=debian\n", "debian", "apt"),
        ("ID=ubuntu\nID_LIKE=debian\n", "ubuntu", "apt"),
        ("ID=fedora\n", "fedora", "dnf"),
        ('ID="rhel"\n', "rhel", "dnf"),
        # ID unknown but ID_LIKE maps -> falls back to the like token.
        ("ID=manjaro\nID_LIKE=arch\n", "manjaro", "pacman"),
        ('ID=linuxmint\nID_LIKE="ubuntu debian"\n', "linuxmint", "apt"),
    ],
)
def test_distro_maps_to_pkg_manager(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    os_release: str,
    expected_id: str,
    expected_mgr: str,
) -> None:
    """Known distro IDs (and ID_LIKE fallbacks) resolve to a package manager."""
    os_release_file = tmp_path / "os-release"
    os_release_file.write_text(os_release)
    monkeypatch.setattr(profile_mod, "_OS_RELEASE", os_release_file)
    distro_id, manager = _detect_pkg_manager()
    assert distro_id == expected_id
    assert manager == expected_mgr


def test_unknown_distro_yields_no_pkg_manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unrecognised distro keeps its ID but reports no package manager."""
    os_release_file = tmp_path / "os-release"
    os_release_file.write_text("ID=gentoo\nID_LIKE=\n")
    monkeypatch.setattr(profile_mod, "_OS_RELEASE", os_release_file)
    distro_id, manager = _detect_pkg_manager()
    assert distro_id == "gentoo"
    assert manager is None


def test_missing_os_release_yields_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing /etc/os-release degrades to (None, None), never raises."""
    monkeypatch.setattr(profile_mod, "_OS_RELEASE", tmp_path / "absent")
    assert _detect_pkg_manager() == (None, None)


def test_parse_os_release_strips_quotes_and_comments() -> None:
    """Quoted values are unquoted and comment/blank lines are skipped."""
    parsed = _parse_os_release("# comment\n\nID=\"arch\"\nNAME='Arch Linux'\n")
    assert parsed["ID"] == "arch"
    assert parsed["NAME"] == "Arch Linux"
    assert "# comment" not in parsed


def test_docker_installed_true_when_binary_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """``docker_installed`` is True when ``docker`` is on PATH."""
    monkeypatch.setattr(profile_mod.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(profile_mod, "_detect_pkg_manager", lambda: ("arch", "pacman"))
    prof = HostProfile.detect()
    assert prof.docker_installed is True


def test_docker_installed_false_when_binary_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """``docker_installed`` is False when the docker binary is missing."""
    monkeypatch.setattr(profile_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(profile_mod, "_detect_pkg_manager", lambda: ("arch", "pacman"))
    prof = HostProfile.detect()
    assert prof.docker_installed is False


def test_detect_performs_no_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """``detect()`` must spawn no subprocess - read-only profiling only.

    A mutating remediation would shell out; the profiler never does. We trip
    a guard if ``subprocess.run`` is invoked at all during detection.
    """

    def _fail_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("detect() must not invoke subprocess.run")

    monkeypatch.setattr(subprocess, "run", _fail_run)
    monkeypatch.setattr(profile_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(profile_mod, "_detect_pkg_manager", lambda: ("arch", "pacman"))

    prof = HostProfile.detect()

    assert isinstance(prof, HostProfile)
    assert prof.pkg_manager == "pacman"


def test_in_docker_group_false_without_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``docker`` group on the host means the user is not a member."""

    def _raise(_name: str) -> object:
        raise KeyError(_name)

    monkeypatch.setattr(grp, "getgrnam", _raise)
    assert profile_mod._in_docker_group() is False
