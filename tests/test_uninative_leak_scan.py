"""Tests for the post-build native-artifact glibc leak scan (``uninative-leak``).

The scan reaches a version node two ways: an artifact's own symbol table, and
the symbol table of every ``DT_NEEDED`` dependency that resolves outside the
sanctioned trees. The second path is the one the check exists for - a host
library built against the host glibc carries the fault one edge away while the
artifact reads clean - so ``test_leak_via_dt_needed_edge`` calibrates the
ceiling to sit *between* the artifact's highest node and its libc's, where an
implementation that only read the artifact would report a false all-clear.

Fixtures use real system binaries rather than synthesised ELF bytes: the scan
shells out to ``objdump``, so hand-crafted headers would only prove the reader
mock agrees with itself. The work trees hold two files at most, keeping the
process count (and the suite) small.

DESTRUCTIVE-TEST GUARD: ``test_post_build_excluded_by_default`` calls the real
``run_all``, which runs every pre-flight check - including the ``DL_DIR``
cache repair that ``shutil.rmtree``s entries it judges broken, and which
resolves ``DL_DIR``/``SSTATE_DIR`` environment-first. The module-scope
``_neutralise_cache_env`` fixture below is ``autouse=True`` and unsets both, so
no test here can reach the operator's real caches.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bakar import diagnostics
from bakar.config import BuildConfig
from bakar.diagnostics import BuildtoolsToolchain, Severity, Status

_OBJDUMP = shutil.which("objdump")
requires_objdump = pytest.mark.skipif(_OBJDUMP is None, reason="the scan needs objdump to read ELF fixtures")

# Two real binaries with a small, stable DT_NEEDED set. ``_CLEAN`` needs only
# libc, which makes the calibrated-ceiling leak test depend on one edge.
_CLEAN = Path("/usr/bin/true")
_SECOND = Path("/usr/bin/ls")
_HOST_LIBC = Path("/usr/lib/libc.so.6")

_VERSION = "2.44+r5+g7cba77790f32"
_CHECKSUM = "ab" * 32

# Above every glibc version node that exists, so a fixture scanned against it
# is clean regardless of the host's glibc.
_UNREACHABLE_CEILING = "99.0"


@pytest.fixture(autouse=True)
def _neutralise_cache_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset the real cache environment before every test in this module.

    See the module docstring: ``run_all`` reaches checks that write to and
    delete from whatever ``DL_DIR``/``SSTATE_DIR`` name.
    """
    monkeypatch.delenv("DL_DIR", raising=False)
    monkeypatch.delenv("SSTATE_DIR", raising=False)


def _cfg(workspace: Path, *, host_mode: bool = True, uninative: bool = True) -> BuildConfig:
    """Return a minimal BuildConfig rooted at ``workspace``."""
    return BuildConfig(
        workspace=workspace,
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


def _patch_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    max_glibc: str,
) -> None:
    """Point the gate at an Arch-family os-release and a fixture fragment.

    ``_uninative_gate`` requires an Arch-family host, so without the os-release
    fixture every assertion here would be vacuous against a SKIP.
    ``detect_buildtools`` is stubbed absent so the sanctioned set is exactly the
    uninative sysroot under the fixture tmpdir, not whatever toolchain the host
    running the suite happens to have installed.
    """
    release = tmp_path / "os-release"
    release.write_text('ID=arch\nID_LIKE=""\n', encoding="utf-8")
    monkeypatch.setattr(diagnostics, "_UNINATIVE_OS_RELEASE", release)

    fragment = tmp_path / "uninative.inc"
    fragment.write_text(
        f'UNINATIVE_URL = "file://{tmp_path / "mirror"}/"\n'
        f'UNINATIVE_VERSION:forcevariable = "{_VERSION}"\n'
        f'UNINATIVE_CHECKSUM[x86_64] = "{_CHECKSUM}"\n'
        f'UNINATIVE_MAXGLIBCVERSION:forcevariable = "{max_glibc}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(diagnostics, "_UNINATIVE_FRAGMENT", fragment)
    monkeypatch.setattr(
        diagnostics,
        "detect_buildtools",
        lambda release_key=None: BuildtoolsToolchain(present=False, detail="stubbed absent"),
    )


def _work_tree(cfg: BuildConfig) -> Path:
    """Create and return the native work tree the scan walks."""
    work = cfg.resolved_tmpdir / "work" / "x86_64-linux"
    work.mkdir(parents=True, exist_ok=True)
    return work


def _place(work: Path, recipe: str, source: Path) -> Path:
    """Copy ``source`` into ``<work>/<recipe>/1.0/image/usr/bin/`` and return it.

    The first component under the native work tree is the producing recipe
    (``WORKDIR``'s layout), which is what the remediation hint has to name.
    """
    target_dir = work / recipe / "1.0" / "image" / "usr" / "bin"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    shutil.copy2(source, target)
    return target


def _max_node(path: Path) -> tuple[int, ...]:
    """Highest glibc version node ``path`` declares, read with the real reader."""
    reader = diagnostics._elf_reader()
    assert reader is not None
    info = diagnostics._read_elf(reader, path)
    assert info is not None, f"{path} could not be read by {reader}"
    parsed = [v for v in (diagnostics._version_tuple(n) for n in info.nodes) if v is not None]
    assert parsed, f"{path} declares no glibc version node"
    return max(parsed)


def _ceiling_between(artifact: Path, dependency: Path) -> str:
    """A ceiling clearing ``artifact``'s own nodes but not ``dependency``'s.

    Calibrated at run time rather than hardcoded so a glibc bump on the host
    running the suite cannot quietly turn the DT_NEEDED test vacuous.
    """
    own = _max_node(artifact)
    dep = _max_node(dependency)
    assert dep > own, f"{dependency} ({dep}) must carry a higher node than {artifact} ({own})"
    return ".".join(str(part) for part in own)


@pytest.mark.unit
@requires_objdump
def test_clean_artifact_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An artifact and its resolvable dependency both under the ceiling PASS."""
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    _place(_work_tree(cfg), "zlib-native", _CLEAN)

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.PASS
    assert result.severity is Severity.BLOCK
    assert "no glibc version node above the uninative ceiling" in result.message
    assert "1 native artifact(s) scanned" in result.message


@pytest.mark.unit
@requires_objdump
def test_own_high_node_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An artifact whose own symbol table exceeds the ceiling BLOCKs on itself."""
    _patch_host(monkeypatch, tmp_path, max_glibc="2.2")
    cfg = _cfg(tmp_path)
    _place(_work_tree(cfg), "zlib-native", _CLEAN)

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "the artifact itself" in result.message
    assert result.fix_hint is not None
    assert "cleansstate zlib-native" in result.fix_hint


@pytest.mark.unit
@requires_objdump
def test_leak_via_dt_needed_edge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A clean artifact whose host-library edge carries a high node still BLOCKs.

    The ceiling is calibrated to the artifact's own highest node, so its own
    symbol table is clean and the only finding can come from following
    ``DT_NEEDED`` to the host libc. A scan that never resolved those edges would
    report PASS here.
    """
    ceiling = _ceiling_between(_CLEAN, _HOST_LIBC)
    _patch_host(monkeypatch, tmp_path, max_glibc=ceiling)
    cfg = _cfg(tmp_path)
    artifact = _place(_work_tree(cfg), "zlib-native", _CLEAN)

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "the artifact itself" not in result.message
    assert f"dependency libc.so.6 at {_HOST_LIBC}" in result.message
    assert f"{artifact} (recipe zlib-native)" in result.message


@pytest.mark.unit
@requires_objdump
def test_dependency_inside_buildtools_sysroot_is_sanctioned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The same edge resolved inside the uninative sysroot produces no finding.

    Same binary and same calibrated ceiling as ``test_leak_via_dt_needed_edge``;
    the only difference is that the soname now resolves into a sanctioned tree,
    whose libraries are safe by construction because the loader that will load
    the artifact comes from there.
    """
    ceiling = _ceiling_between(_CLEAN, _HOST_LIBC)
    _patch_host(monkeypatch, tmp_path, max_glibc=ceiling)
    cfg = _cfg(tmp_path)
    _place(_work_tree(cfg), "zlib-native", _CLEAN)

    sanctioned_lib = cfg.resolved_tmpdir / "sysroots-uninative" / "lib"
    sanctioned_lib.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_HOST_LIBC, sanctioned_lib / _HOST_LIBC.name)
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", (str(sanctioned_lib),))

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.PASS
    assert result.severity is Severity.BLOCK


@pytest.mark.unit
@requires_objdump
def test_unresolvable_dependency_warns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A soname that resolves to no file is a WARN, never a silent PASS."""
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    _place(_work_tree(cfg), "zlib-native", _CLEAN)
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", ())

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert "declares libc.so.6, which resolves to no file" in result.message
    assert "an unchecked dependency is not evidence of a clean tree" in result.message


@pytest.mark.unit
@requires_objdump
def test_unreadable_dependency_warns_not_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A dependency that exists but cannot be read is unchecked, not clean.

    It is reported at WARN alongside the unresolvable ones: an unread file
    carries no evidence either way, so escalating it to BLOCK would fail a build
    over a missing fact rather than a found leak.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    _place(_work_tree(cfg), "zlib-native", _CLEAN)

    fake_libs = tmp_path / "fake-libs"
    fake_libs.mkdir()
    (fake_libs / _HOST_LIBC.name).write_bytes(b"not an ELF file at all")
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", (str(fake_libs),))

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert "could not read" in result.message


@pytest.mark.unit
@requires_objdump
def test_multiple_simultaneous_leaks_are_all_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Two leaking recipes both reach the verdict, and the tail is summarized."""
    _patch_host(monkeypatch, tmp_path, max_glibc="2.2")
    cfg = _cfg(tmp_path)
    work = _work_tree(cfg)
    _place(work, "zlib-native", _CLEAN)
    _place(work, "openssl-native", _SECOND)

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert result.fix_hint is not None
    assert "cleansstate openssl-native zlib-native" in result.fix_hint
    # Far more than _LEAK_REPORT_LIMIT findings, so the message must stay
    # bounded rather than enumerate every node of two binaries plus their libc.
    assert " more" in result.message
    assert result.message.count(";") <= diagnostics._LEAK_REPORT_LIMIT + 2


@pytest.mark.unit
def test_absent_work_tree_skips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Nothing built yet is a SKIP naming the missing tree, not a PASS."""
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.SKIP
    assert "does not exist, so nothing has been built to scan" in result.message


@pytest.mark.unit
def test_absent_elf_reader_skips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With no ELF reader the tree is unscanned - which must not read as clean."""
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    _place(_work_tree(cfg), "zlib-native", _CLEAN)
    monkeypatch.setattr(diagnostics, "_elf_reader", lambda: None)

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.SKIP
    assert result.status is not Status.PASS
    assert "this is unscanned, not clean" in result.message


@pytest.mark.unit
def test_non_numeric_ceiling_skips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A ceiling that is not a dotted numeric version leaves nothing to compare."""
    _patch_host(monkeypatch, tmp_path, max_glibc="2.44-arch1")
    cfg = _cfg(tmp_path)
    _work_tree(cfg)

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.SKIP
    assert "is not a dotted numeric version" in result.message


@pytest.mark.unit
def test_post_build_excluded_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``run_all`` omits the leak scan unless the caller asks for post-build.

    The walk costs a full native-tree traversal and has nothing to read before a
    build, so an ordinary ``bakar doctor`` must not pay for it; ``post_build`` is
    additive, so the post-build run is a superset of the default one.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)

    assert diagnostics.check_uninative_leak in diagnostics.SHARED_CHECKS
    assert diagnostics.check_uninative_leak in diagnostics._POST_BUILD_CHECKS

    default_names = [r.name for r in diagnostics.run_all(cfg)]
    post_build_names = [r.name for r in diagnostics.run_all(cfg, post_build=True)]

    assert "uninative-leak" not in default_names
    assert "uninative-leak" in post_build_names
    assert set(default_names) < set(post_build_names)


@pytest.mark.unit
def test_every_new_check_is_grouped() -> None:
    """Every uninative check this change registers is placed in some group.

    ``group_results`` buckets by name, so a registered check missing from
    ``CHECK_GROUPS`` lands in the trailing "Other" group - visible, but detached
    from the uninative findings an operator reads it beside. Asserted as a subset
    over the union of all groups rather than against one group's tuple, so
    reordering or resplitting the groups does not break the test.
    """
    uninative_checks = (
        diagnostics.check_uninative_fragment,
        diagnostics.check_uninative_glibc,
        diagnostics.check_uninative_checksum,
        diagnostics.check_uninative_dldir_links,
        diagnostics.check_uninative_mirror_hit,
        diagnostics.check_uninative_cluster_consistency,
        diagnostics.check_uninative_leak,
    )
    registered = {diagnostics._CHECK_NAME[check] for check in uninative_checks}
    assert len(registered) == len(uninative_checks), "two uninative checks register the same name"

    grouped = {name for _group, names in diagnostics.CHECK_GROUPS for name in names}
    assert registered <= grouped, f"ungrouped uninative checks: {sorted(registered - grouped)}"
