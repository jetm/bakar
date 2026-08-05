"""Tests for the Arch-host native-probe overlay selection helper.

Arch-family distros ship development headers in the base system that
Debian-family build hosts keep in separate ``-dev`` packages. OE native
recipes that run their own ``check_include_file`` / ``check_library_exists``
probes then find those host headers, link against host ``/usr/lib``, and
produce a binary the uninative loader cannot start - nothing stages the
library into ``recipe-sysroot-native``, so RUNPATH never covers it.

Covers the three-way gate in ``_arch_probe_extra_overlays`` (uninative on,
host mode, Arch-family host) plus the overlay's presence in
``_tuning_extra_overlays``. Every test patches the os-release path, so the
result does not depend on the distro running the suite.

The uninative condition is causal, not cosmetic: the leak is only fatal
because uninative swapped the program interpreter for one that never searches
the host's default library paths. It also keeps the tuning stack independent
of the machine, which several sibling suites assert byte-for-byte.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar.config import BuildConfig

OVERLAY = "bakar-tuning-arch-probes.yml"


def _cfg(*, host_mode: bool = True, uninative: bool = True) -> BuildConfig:
    """Return a minimal BuildConfig for the arch-probe overlay helper tests."""
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


def _patch_os_release(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contents: str) -> None:
    """Point the Arch-family check at a fixture file instead of the real host."""
    from bakar.commands import _helpers

    os_release_path = tmp_path / "os-release"
    os_release_path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(_helpers, "_UNINATIVE_OS_RELEASE", os_release_path)


@pytest.mark.unit
def test_arch_probe_overlay_selected_on_arch_host_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Host mode on an Arch-family host yields the overlay."""
    from bakar.commands._helpers import _arch_probe_extra_overlays

    _patch_os_release(monkeypatch, tmp_path, "ID=cachyos\nID_LIKE=arch\n")

    result = _arch_probe_extra_overlays(_cfg(host_mode=True))

    assert len(result) == 1
    assert result[0].name == OVERLAY
    assert result[0].is_file(), "overlay file must exist in the installed overlays/ dir"


@pytest.mark.unit
def test_arch_probe_overlay_skipped_in_container_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Container builds run on a clean image, so no host headers can leak in."""
    from bakar.commands._helpers import _arch_probe_extra_overlays

    _patch_os_release(monkeypatch, tmp_path, "ID=arch\n")

    assert _arch_probe_extra_overlays(_cfg(host_mode=False)) == []


@pytest.mark.unit
def test_arch_probe_overlay_skipped_on_non_arch_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A Debian-family host does not ship idn2.h by default - leave its signatures alone."""
    from bakar.commands._helpers import _arch_probe_extra_overlays

    _patch_os_release(monkeypatch, tmp_path, "ID=ubuntu\nID_LIKE=debian\n")

    assert _arch_probe_extra_overlays(_cfg(host_mode=True)) == []


@pytest.mark.unit
def test_arch_probe_overlay_skipped_when_uninative_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without uninative the host loader resolves the leaked library, so nothing is broken.

    This also keeps the tuning stack from varying with the build machine, which
    the getvar, bitbake, colon-overlay and mold suites assert byte-for-byte.
    """
    from bakar.commands._helpers import _arch_probe_extra_overlays

    _patch_os_release(monkeypatch, tmp_path, "ID=cachyos\nID_LIKE=arch\n")

    assert _arch_probe_extra_overlays(_cfg(host_mode=True, uninative=False)) == []


@pytest.mark.unit
def test_arch_probe_overlay_in_tuning_stack(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_tuning_extra_overlays includes the overlay when the gate passes."""
    from bakar.commands._helpers import _tuning_extra_overlays

    _patch_os_release(monkeypatch, tmp_path, "ID=arch\n")

    names = [p.name for p in _tuning_extra_overlays(_cfg(host_mode=True))]

    assert OVERLAY in names


@pytest.mark.unit
def test_arch_probe_overlay_absent_from_tuning_stack_on_non_arch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tuning stack is byte-identical on a non-Arch host (falsifier)."""
    from bakar.commands._helpers import _tuning_extra_overlays

    _patch_os_release(monkeypatch, tmp_path, "ID=debian\n")

    names = [p.name for p in _tuning_extra_overlays(_cfg(host_mode=True))]

    assert OVERLAY not in names


@pytest.mark.unit
def test_overlay_disables_both_the_feature_and_the_probe() -> None:
    """The overlay must pre-seed HAVE_LIBIDN2, not just set USE_LIBIDN2.

    ``check_library_exists`` searches the default linker path and runs
    independently of the feature switch, so ``-DUSE_LIBIDN2=0`` alone leaves
    ``HAVE_LIBIDN2=1`` in the cache and ``-lidn2`` on the link line. This
    mirrors oe-core's own ACL handling in the same recipe
    (``-DENABLE_ACL=0 -DHAVE_ACL_LIBACL_H=0 -DHAVE_SYS_ACL_H=0``).
    """
    from bakar.config import _overlay_dir

    body = (_overlay_dir() / OVERLAY).read_text(encoding="utf-8")

    assert "-DUSE_LIBIDN2=0" in body
    assert "-DHAVE_LIBIDN2=0" in body
    assert "pn-cmake-native" in body
