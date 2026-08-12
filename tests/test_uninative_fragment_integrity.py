"""Tests for the uninative wiring integrity preflight checks.

Covers the shared fragment parser, the four-condition check gate, the numeric
version comparator, and the three checks themselves (``uninative-fragment``,
``uninative-glibc``, ``uninative-checksum``).

Every test patches ``diagnostics._UNINATIVE_FRAGMENT`` and
``diagnostics._UNINATIVE_OS_RELEASE`` at fixture files the way
``tests/test_uninative_helpers.py`` patches the ``commands/_helpers`` copies, so
no result depends on the distro running the suite. The glibc tests go one step
further and stand up a fake buildtools sysroot with an executable ``libc.so.6``
stub, so an implementation that reached a verdict without consulting the sysroot
would fail them rather than pass by coincidence.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from bakar import diagnostics
from bakar.config import BuildConfig
from bakar.diagnostics import BuildtoolsToolchain, Severity, Status

_VERSION = "2.44+r5+g7cba77790f32"


def _cfg(*, host_mode: bool = True, uninative: bool = True) -> BuildConfig:
    """Return a minimal BuildConfig for the uninative integrity checks."""
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


def _fragment_text(
    *,
    version: str = _VERSION,
    max_glibc: str = "2.44",
    checksum: str = "ab" * 32,
    url: str = "file:///usr/share/yocto-uninative/mirror/",
    omit: tuple[str, ...] = (),
) -> str:
    """Render a fragment mirroring the shipped one's mix of assignment forms.

    ``UNINATIVE_VERSION`` and ``UNINATIVE_MAXGLIBCVERSION`` carry the
    ``:forcevariable`` suffix, ``UNINATIVE_CHECKSUM`` carries an ``[x86_64]``
    varflag, and ``UNINATIVE_URL`` is plain - exactly as the package generates.
    """
    lines = {
        "UNINATIVE_URL": f'UNINATIVE_URL = "{url}"',
        "UNINATIVE_VERSION": f'UNINATIVE_VERSION:forcevariable = "{version}"',
        "UNINATIVE_CHECKSUM": f'UNINATIVE_CHECKSUM[x86_64] = "{checksum}"',
        "UNINATIVE_MAXGLIBCVERSION": f'UNINATIVE_MAXGLIBCVERSION:forcevariable = "{max_glibc}"',
    }
    return "".join(f"{body}\n" for var, body in lines.items() if var not in omit)


def _patch_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    fragment: str | None,
    arch: bool = True,
) -> Path:
    """Point the checks at fixture files instead of the real host.

    Returns the fragment path, which is absent on disk when ``fragment`` is None.
    """
    os_release = tmp_path / "os-release"
    os_release.write_text(
        "ID=cachyos\nID_LIKE=arch\n" if arch else 'ID=debian\nID_LIKE="ubuntu"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(diagnostics, "_UNINATIVE_OS_RELEASE", os_release)

    fragment_path = tmp_path / "uninative.inc"
    if fragment is not None:
        fragment_path.write_text(fragment, encoding="utf-8")
    monkeypatch.setattr(diagnostics, "_UNINATIVE_FRAGMENT", fragment_path)
    return fragment_path


def _fake_sysroot(tmp_path: Path, glibc: str = "2.44", *, libc: bool = True) -> Path:
    """Build a buildtools sysroot tree whose libc.so.6 reports ``glibc``."""
    sysroot = tmp_path / "sysroot"
    (sysroot / "usr" / "bin").mkdir(parents=True)
    (sysroot / "usr" / "bin" / "gcc").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    if libc:
        lib = sysroot / "lib"
        lib.mkdir()
        libc_stub = lib / "libc.so.6"
        libc_stub.write_text(
            f'#!/bin/sh\necho "GNU C Library (GNU libc) stable release version {glibc}."\n',
            encoding="utf-8",
        )
        libc_stub.chmod(0o755)
    return sysroot


def _patch_buildtools(monkeypatch: pytest.MonkeyPatch, sysroot: Path | None) -> None:
    """Resolve buildtools to ``sysroot``, or to absent when it is None."""
    monkeypatch.setattr(diagnostics, "resolve_oe_core_release_key", lambda _workspace: None)
    toolchain = (
        BuildtoolsToolchain(present=True, sysroot=sysroot, detail="fixture toolchain")
        if sysroot is not None
        else BuildtoolsToolchain(present=False, detail="no fixture toolchain")
    )
    monkeypatch.setattr(diagnostics, "detect_buildtools", lambda release_key=None: toolchain)


def _mirror_with_payload(tmp_path: Path, body: bytes = b"payload") -> tuple[Path, str]:
    """Write a payload into a fixture mirror and return (mirror dir, its sha256)."""
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    payload = mirror / f"x86_64-nativesdk-libc-{_VERSION}.tar.xz"
    payload.write_bytes(body)
    return mirror, hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parser_reads_forcevariable_and_plain_forms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """All four values parse whether or not the name carries :forcevariable."""
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text())

    fragment = diagnostics.parse_uninative_fragment()

    assert fragment.present is True
    assert fragment.error is None
    assert fragment.version == _VERSION
    assert fragment.max_glibc == "2.44"
    assert fragment.checksum == "ab" * 32
    assert fragment.url == "file:///usr/share/yocto-uninative/mirror/"


@pytest.mark.unit
def test_parser_absent_fragment_is_not_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An uninstalled package parses to present=False, not to an empty success."""
    path = _patch_host(monkeypatch, tmp_path, fragment=None)

    fragment = diagnostics.parse_uninative_fragment()

    assert fragment.present is False
    assert fragment.path == path
    assert fragment.error is not None
    assert "not installed" in fragment.error


@pytest.mark.unit
def test_parser_accepts_an_explicit_path(tmp_path: Path) -> None:
    """The path argument overrides the module-level fragment location."""
    other = tmp_path / "elsewhere.inc"
    other.write_text(_fragment_text(max_glibc="2.41"), encoding="utf-8")

    fragment = diagnostics.parse_uninative_fragment(other)

    assert fragment.path == other
    assert fragment.max_glibc == "2.41"


@pytest.mark.unit
@pytest.mark.parametrize(
    "omitted",
    ["UNINATIVE_URL", "UNINATIVE_VERSION", "UNINATIVE_CHECKSUM", "UNINATIVE_MAXGLIBCVERSION"],
)
def test_parser_reports_each_missing_variable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, omitted: str) -> None:
    """Dropping any single parsed variable is present-but-error, never a pass."""
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text(omit=(omitted,)))

    fragment = diagnostics.parse_uninative_fragment()

    assert fragment.present is True
    assert fragment.error is not None
    assert omitted in fragment.error


@pytest.mark.unit
def test_parser_treats_an_empty_value_as_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An assignment present but empty must not satisfy the requirement."""
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text(max_glibc=""))

    fragment = diagnostics.parse_uninative_fragment()

    assert fragment.error is not None
    assert "UNINATIVE_MAXGLIBCVERSION" in fragment.error


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_gate_applies_on_an_arch_host_in_host_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """None from the gate means the checks run."""
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text())

    assert diagnostics._uninative_gate(_cfg()) is None


@pytest.mark.unit
def test_gate_skips_when_the_toggle_is_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[build] uninative = false takes precedence over everything else."""
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text())

    reason = diagnostics._uninative_gate(_cfg(uninative=False))

    assert reason is not None
    assert "uninative is off" in reason


@pytest.mark.unit
def test_gate_skips_on_a_non_arch_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A Debian host cannot install the providing package, so nothing is asserted."""
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text(), arch=False)

    reason = diagnostics._uninative_gate(_cfg())

    assert reason is not None
    assert "not Arch-family" in reason


@pytest.mark.unit
def test_gate_skips_when_os_release_is_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unreadable os-release skips rather than demanding an Arch package."""
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text())
    monkeypatch.setattr(diagnostics, "_UNINATIVE_OS_RELEASE", tmp_path / "absent-os-release")

    assert diagnostics._uninative_gate(_cfg()) is not None


@pytest.mark.unit
def test_gate_ignores_a_missing_fragment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The gate must not include the fragment condition.

    If it did, the check whose whole job is to report an absent fragment would
    skip itself precisely when the fragment is absent.
    """
    _patch_host(monkeypatch, tmp_path, fragment=None)

    assert diagnostics._uninative_gate(_cfg()) is None


@pytest.mark.unit
def test_container_mode_skips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """All three checks self-skip in container mode; the file:// mirror is host-only."""
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text())
    cfg = _cfg(host_mode=False)

    reason = diagnostics._uninative_gate(cfg)
    assert reason is not None
    assert "container build" in reason

    for check in (
        diagnostics.check_uninative_fragment,
        diagnostics.check_uninative_glibc,
        diagnostics.check_uninative_checksum,
    ):
        result = check(cfg)
        assert result.status is Status.SKIP, check.__name__
        assert result.severity is Severity.INFO, check.__name__
        assert "container build" in result.message


# ---------------------------------------------------------------------------
# comparator
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_version_tuple_orders_numerically_not_lexically() -> None:
    """2.44 outranks 2.9, which string comparison gets backwards."""
    assert diagnostics._version_tuple("2.44") == (2, 44)
    assert diagnostics._version_tuple("2.9") < diagnostics._version_tuple("2.44")
    assert diagnostics._version_tuple(" 2.41 ") == (2, 41)


@pytest.mark.unit
@pytest.mark.parametrize("value", [_VERSION, "2.44-rc1", "", "abc", "2..3"])
def test_version_tuple_rejects_non_numeric(value: str) -> None:
    """Anything the comparator cannot order returns None instead of guessing."""
    assert diagnostics._version_tuple(value) is None


# ---------------------------------------------------------------------------
# uninative-fragment
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fragment_present_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A complete fragment passes and reports the version and glibc ceiling."""
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text())

    result = diagnostics.check_uninative_fragment(_cfg())

    assert result.name == "uninative-fragment"
    assert result.status is Status.PASS
    assert result.severity is Severity.BLOCK
    assert _VERSION in result.message
    assert "2.44" in result.message


@pytest.mark.unit
def test_fragment_absent_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An uninstalled package BLOCKs; the overlay gate would drop the wiring silently."""
    path = _patch_host(monkeypatch, tmp_path, fragment=None)

    result = diagnostics.check_uninative_fragment(_cfg())

    assert result.name == "uninative-fragment"
    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert str(path) in result.message
    assert result.fix_hint is not None
    assert "yocto-uninative-tarball" in result.fix_hint


@pytest.mark.unit
def test_malformed_fragment_fails_loudly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A present-but-incomplete fragment BLOCKs in all three checks.

    A safety gate that silently stops comparing is indistinguishable from one
    that always agrees, so none of the three may degrade to a pass or a skip.
    """
    _patch_host(monkeypatch, tmp_path, fragment='UNINATIVE_URL = "file:///x/"\n')
    _patch_buildtools(monkeypatch, _fake_sysroot(tmp_path))
    cfg = _cfg()

    for check in (
        diagnostics.check_uninative_fragment,
        diagnostics.check_uninative_glibc,
        diagnostics.check_uninative_checksum,
    ):
        result = check(cfg)
        assert result.status is Status.FAIL, check.__name__
        assert result.severity is Severity.BLOCK, check.__name__
        assert "UNINATIVE_VERSION" in result.message, check.__name__


# ---------------------------------------------------------------------------
# uninative-glibc
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_glibc_pin_above_sysroot_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A ceiling above the sysroot's glibc leaves headroom for a buildtools bump."""
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text(max_glibc="2.45"))
    _patch_buildtools(monkeypatch, _fake_sysroot(tmp_path, "2.44"))

    result = diagnostics.check_uninative_glibc(_cfg())

    assert result.name == "uninative-glibc"
    assert result.status is Status.PASS
    assert "2.45" in result.message
    assert "2.44" in result.message


@pytest.mark.unit
def test_glibc_pin_equal_to_sysroot_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Equality holds the invariant, and the message says there is no headroom."""
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text(max_glibc="2.44"))
    _patch_buildtools(monkeypatch, _fake_sysroot(tmp_path, "2.44"))

    result = diagnostics.check_uninative_glibc(_cfg())

    assert result.status is Status.PASS
    assert "no headroom" in result.message


@pytest.mark.unit
def test_glibc_pin_below_sysroot_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A ceiling under the sysroot's glibc BLOCKs.

    The sysroot glibc is read from the fixture's executable libc.so.6 rather
    than injected, so an implementation that never consults the sysroot cannot
    reach this verdict. 2.9 against 2.44 also pins the comparison as numeric.
    """
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text(max_glibc="2.9"))
    _patch_buildtools(monkeypatch, _fake_sysroot(tmp_path, "2.44"))

    result = diagnostics.check_uninative_glibc(_cfg())

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "2.9" in result.message
    assert "2.44" in result.message
    assert result.fix_hint is not None


@pytest.mark.unit
def test_glibc_reads_the_sysroot_reported_version(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Flipping only the sysroot's reported glibc flips the verdict.

    Same fragment both times: the check must be reading libc.so.6's output.
    """
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text(max_glibc="2.40"))

    _patch_buildtools(monkeypatch, _fake_sysroot(tmp_path / "low", "2.39"))
    assert diagnostics.check_uninative_glibc(_cfg()).status is Status.PASS

    _patch_buildtools(monkeypatch, _fake_sysroot(tmp_path / "high", "2.41"))
    assert diagnostics.check_uninative_glibc(_cfg()).status is Status.FAIL


@pytest.mark.unit
def test_glibc_skips_when_buildtools_is_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No toolchain means no ceiling to compare against, so the check skips."""
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text())
    _patch_buildtools(monkeypatch, None)

    result = diagnostics.check_uninative_glibc(_cfg())

    assert result.status is Status.SKIP
    assert result.severity is Severity.INFO
    assert "no buildtools toolchain" in result.message


@pytest.mark.unit
def test_glibc_skips_when_the_sysroot_libc_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A sysroot with no libc.so.6 reports the reason rather than a verdict."""
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text())
    _patch_buildtools(monkeypatch, _fake_sysroot(tmp_path, libc=False))

    result = diagnostics.check_uninative_glibc(_cfg())

    assert result.status is Status.SKIP
    assert "no libc.so.6" in result.message


@pytest.mark.unit
def test_glibc_skips_when_the_sysroot_prints_no_version(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unrecognizable libc.so.6 banner skips instead of comparing garbage."""
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text())
    sysroot = _fake_sysroot(tmp_path, "2.44")
    (sysroot / "lib" / "libc.so.6").write_text('#!/bin/sh\necho "not a libc banner"\n', encoding="utf-8")
    (sysroot / "lib" / "libc.so.6").chmod(0o755)
    _patch_buildtools(monkeypatch, sysroot)

    result = diagnostics.check_uninative_glibc(_cfg())

    assert result.status is Status.SKIP
    assert "no recognizable release version" in result.message


@pytest.mark.unit
def test_glibc_skips_on_a_non_numeric_ceiling(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A ceiling the comparator cannot order skips rather than guessing."""
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text(max_glibc="2.44-rc1"))
    _patch_buildtools(monkeypatch, _fake_sysroot(tmp_path, "2.44"))

    result = diagnostics.check_uninative_glibc(_cfg())

    assert result.status is Status.SKIP
    assert "non-numeric" in result.message


@pytest.mark.unit
def test_glibc_skips_when_the_fragment_is_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """One missing package yields one finding, from uninative-fragment only."""
    _patch_host(monkeypatch, tmp_path, fragment=None)
    _patch_buildtools(monkeypatch, _fake_sysroot(tmp_path))

    result = diagnostics.check_uninative_glibc(_cfg())

    assert result.status is Status.SKIP
    assert "uninative-fragment" in result.message


# ---------------------------------------------------------------------------
# uninative-checksum
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_checksum_match_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The real sha256 of the mirrored payload matching the fragment passes."""
    mirror, digest = _mirror_with_payload(tmp_path)
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text(checksum=digest, url=f"file://{mirror}/"))

    result = diagnostics.check_uninative_checksum(_cfg())

    assert result.name == "uninative-checksum"
    assert result.status is Status.PASS
    assert result.severity is Severity.BLOCK
    assert digest in result.message


@pytest.mark.unit
def test_checksum_mismatch_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A payload that hashes to something else BLOCKs and reports both values."""
    mirror, digest = _mirror_with_payload(tmp_path)
    declared = "00" * 32
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text(checksum=declared, url=f"file://{mirror}/"))

    result = diagnostics.check_uninative_checksum(_cfg())

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert declared in result.message
    assert digest in result.message


@pytest.mark.unit
def test_checksum_payload_absent_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An empty mirror BLOCKs; uninative would fall back to a network fetch."""
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text(url=f"file://{mirror}/"))

    result = diagnostics.check_uninative_checksum(_cfg())

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert f"x86_64-nativesdk-libc-{_VERSION}.tar.xz" in result.message
    assert result.fix_hint is not None


@pytest.mark.unit
def test_checksum_payload_name_tracks_the_declared_version(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The payload filename is derived from UNINATIVE_VERSION, as uninative does."""
    mirror, digest = _mirror_with_payload(tmp_path)
    _patch_host(
        monkeypatch,
        tmp_path,
        fragment=_fragment_text(version="9.99+r1+gdeadbeef", checksum=digest, url=f"file://{mirror}/"),
    )

    result = diagnostics.check_uninative_checksum(_cfg())

    assert result.status is Status.FAIL
    assert "x86_64-nativesdk-libc-9.99+r1+gdeadbeef.tar.xz" in result.message


@pytest.mark.unit
def test_checksum_skips_on_a_non_local_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A remote UNINATIVE_URL has no local payload to hash, so the check skips."""
    _patch_host(monkeypatch, tmp_path, fragment=_fragment_text(url="https://example.com/mirror/"))

    result = diagnostics.check_uninative_checksum(_cfg())

    assert result.status is Status.SKIP
    assert result.severity is Severity.INFO
    assert "not a local mirror" in result.message


@pytest.mark.unit
def test_checksum_skips_when_the_fragment_is_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The absent package is reported once, by uninative-fragment."""
    _patch_host(monkeypatch, tmp_path, fragment=None)

    result = diagnostics.check_uninative_checksum(_cfg())

    assert result.status is Status.SKIP
    assert "uninative-fragment" in result.message


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_three_checks_are_registered_under_one_group() -> None:
    """The three checks ship in SHARED_CHECKS, the metadata table, and one group."""
    names = ("uninative-fragment", "uninative-glibc", "uninative-checksum")
    funcs = (
        diagnostics.check_uninative_fragment,
        diagnostics.check_uninative_glibc,
        diagnostics.check_uninative_checksum,
    )
    assert all(func in diagnostics.SHARED_CHECKS for func in funcs)

    metadata = {name: severity for func, name, severity in diagnostics._CHECK_METADATA if func in funcs}
    assert metadata == dict.fromkeys(names, Severity.BLOCK)

    groups = dict(diagnostics.CHECK_GROUPS)
    assert groups["Uninative wiring"] == names
