"""Tests for the post-build native-artifact glibc leak scan (``uninative-leak``).

The scan reaches a version node two ways: an artifact's own symbol table, and
the symbol table of every ``DT_NEEDED`` dependency that resolves outside the
sanctioned trees. The second path is the one the check exists for - a host
library built against the host glibc carries the fault one edge away while the
artifact reads clean - so ``test_leak_via_dt_needed_edge`` calibrates the
ceiling to sit *between* the artifact's highest node and its libc's, where an
implementation that only read the artifact would report a false all-clear.

What an edge contributes is what the dependency REQUIRES, read from
``DT_VERNEED``. It is emphatically not everything ``objdump -T`` parenthesises:
that tracks a non-default version binding and so sweeps in compat definitions
the artifact never calls. So the same test runs a second ceiling at libc's
highest real requirement, strictly below its highest parenthesised node, where
the tree must go clean - the half that fails if the parenthesis rule comes
back. Every ceiling here is read off the host at run time: a literal would go
vacuous on the next glibc bump.

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

import inspect
import io
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from rich.markup import escape

if TYPE_CHECKING:
    from collections.abc import Iterable

from bakar import diagnostics
from bakar.config import BuildConfig
from bakar.diagnostics import _VERNEED_HEADER, BuildtoolsToolchain, CheckResult, Severity, Status

_OBJDUMP = shutil.which("objdump")
requires_objdump = pytest.mark.skipif(_OBJDUMP is None, reason="the scan needs objdump to read ELF fixtures")

# Two real binaries with a small, stable DT_NEEDED set. ``_CLEAN`` needs only
# libc, which makes the calibrated-ceiling leak test depend on one edge.
_CLEAN = Path("/usr/bin/true")
_SECOND = Path("/usr/bin/ls")


def _find_host_libc() -> Path:
    """Locate the host's libc the same way the scan does.

    Hardcoding /usr/lib/libc.so.6 is an Arch-ism: on a multiarch host (Debian,
    Ubuntu, and so the CI runner) libc lives in /usr/lib/<gnu-triplet>. Search
    the same directories the scan searches so this fixture tracks the product
    rather than one distribution's layout. Falls back to the Arch path so the
    ``.exists()`` skip guards below still read naturally when nothing is found.
    """
    for directory in diagnostics._HOST_LIB_DIRS:
        candidate = Path(directory) / "libc.so.6"
        if candidate.is_file():
            return candidate
    return Path("/usr/lib/libc.so.6")


_HOST_LIBC = _find_host_libc()

_VERSION = "2.44+r5+g7cba77790f32"
_CHECKSUM = "ab" * 32

# Above every glibc version node that exists, so a fixture scanned against it
# is clean regardless of the host's glibc.
_UNREACHABLE_CEILING = "99.0"

# Every documented way to select message translations, one case each, because
# they do not reduce to one another: LC_ALL, LANG and LC_MESSAGES are three
# locale categories of differing strength, and LANGUAGE is a separate gettext
# override consulted ahead of all of them. The last two entries pit LANGUAGE
# against the reader's own LC_ALL pin; on glibc 2.42 the pin wins and they
# self-skip as non-discriminating, but they stay because that precedence is an
# implementation detail rather than a documented guarantee. Every case
# self-guards on whether the translation is actually installed here, so an
# unpinned reader cannot pass by finding no bfd.mo to trip over.
_LOCALE_MATRIX: tuple[dict[str, str], ...] = (
    {"LC_ALL": "fr_FR.UTF-8"},
    {"LANGUAGE": "fr_FR"},
    {"LANG": "es_ES.UTF-8"},
    {"LC_MESSAGES": "da_DK.UTF-8"},
    {"LANGUAGE": "fr", "LC_ALL": "C.UTF-8"},
    {"LANGUAGE": "fr_FR:fr", "LC_ALL": "C.UTF-8"},
)


def _locale_id(overlay: dict[str, str]) -> str:
    """A readable pytest id for one ``_LOCALE_MATRIX`` entry."""
    return "+".join(f"{key}={value}" for key, value in overlay.items())


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


def _foreign_elf_header() -> bytes:
    """A 20-byte ELF header naming a platform this host does not run.

    ``ELFOSABI_NETBSD`` (2) and ``EM_SPARCV9`` (43), which is what the upstream
    test fixtures the scan trips over look like. Only the two fields
    ``_host_platform_elf`` reads are meaningful; the rest is the fixed prologue
    it needs to reach them and the magic ``_is_elf`` matches on.
    """
    header = bytearray(20)
    header[0:4] = b"\x7fELF"
    header[4] = 2  # ELFCLASS64
    header[5] = 1  # ELFDATA2LSB
    header[6] = 1  # EV_CURRENT
    header[7] = 2  # ELFOSABI_NETBSD
    header[16:18] = (2).to_bytes(2, "little")  # ET_EXEC
    header[18:20] = (43).to_bytes(2, "little")  # EM_SPARCV9
    return bytes(header)


def _dotted(version: tuple[int, ...]) -> str:
    """Render a parsed version tuple back into the fragment's dotted form."""
    return ".".join(str(part) for part in version)


def _parsed_nodes(nodes: Iterable[str]) -> list[tuple[int, ...]]:
    """Parse dotted node strings, dropping anything unparseable."""
    return [v for v in (diagnostics._version_tuple(n) for n in nodes) if v is not None]


def _max_required_node(path: Path) -> tuple[int, ...]:
    """Highest glibc version node ``path`` REQUIRES, read with the real reader.

    ``_read_elf`` keeps requirements only, so this is what the scan compares
    against the ceiling. It is emphatically not the highest node ``path``
    mentions: ``libc.so.6`` mentions every node it defines.
    """
    reader = diagnostics._elf_reader()
    assert reader is not None
    info = diagnostics._read_elf(reader, path)
    assert info is not None, f"{path} could not be read by {reader}"
    parsed = _parsed_nodes(info.nodes)
    assert parsed, f"{path} requires no glibc version node"
    return max(parsed)


def _objdump(*args: str, env: dict[str, str] | None = None) -> str:
    """Run the real ``objdump`` and return its stdout.

    Defaults to an English-pinned environment, spelled out here rather than
    imported from ``diagnostics`` so a calibration read through this helper
    cannot inherit the very locale bug it is meant to catch. Pass ``env`` to
    probe what an unpinned reader would have seen.
    """
    assert _OBJDUMP is not None
    return subprocess.run(
        [_OBJDUMP, *args],
        capture_output=True,
        text=True,
        errors="replace",
        env={**os.environ, "LC_ALL": "C.UTF-8", "LANGUAGE": ""} if env is None else env,
        check=False,
    ).stdout


def _highest(nodes: Iterable[str]) -> tuple[int, ...] | None:
    """Highest parseable node in ``nodes``, or None when there is none."""
    parsed = _parsed_nodes(nodes)
    return max(parsed) if parsed else None


def _host_max_nodes(path: Path) -> tuple[tuple[int, ...] | None, tuple[int, ...] | None]:
    """``(highest required, highest parenthesised)`` for ``path``, per half None-able.

    Each half is derived by a rule the code under test does not use, so a
    regression cannot move the calibration along with itself. The required half
    comes from ``objdump -p``'s ``Version References`` block, which is
    ``DT_VERNEED`` and holds requirements only. The second half is what
    ``objdump -T`` parenthesises - deliberately NOT called "defined", because
    parenthesisation tracks a non-default version binding and so covers compat
    definitions as well as undefined symbols. It is the discredited rule, kept
    only so the discriminating test can calibrate a ceiling that separates the
    two.
    """
    required = _highest(_version_references(_objdump("-p", str(path))))
    parenthesised = _highest(re.findall(r"\(GLIBC_(\d+(?:\.\d+)+)\)", _objdump("-T", str(path))))
    return required, parenthesised


def _ceiling_between(artifact: Path, dependency: Path) -> str:
    """A ceiling clearing ``artifact``'s own nodes but not ``dependency``'s.

    Calibrated at run time rather than hardcoded so a glibc bump on the host
    running the suite cannot quietly turn the DT_NEEDED test vacuous.
    """
    own = _max_required_node(artifact)
    dep = _max_required_node(dependency)
    assert dep > own, f"{dependency} ({dep}) must require a higher node than {artifact} ({own})"
    return _dotted(own)


def _version_references(dump: str) -> set[str]:
    """``GLIBC_`` nodes named in ``objdump -p``'s ``Version References`` block.

    That block is ``DT_VERNEED`` and holds requirements only, which is what the
    scan now reads. Parsed independently of ``diagnostics._required_glibc_nodes``
    so the calibration cannot inherit a bug from the parser it calibrates.
    """
    block: list[str] = []
    inside = False
    for line in dump.splitlines():
        if line.startswith("Version References:"):
            inside = True
            continue
        if inside:
            if line and not line.startswith(" "):
                break
            block.append(line)
    return set(re.findall(r"\bGLIBC_(\d+(?:\.\d+)+)\b", "\n".join(block)))


@pytest.mark.unit
def test_ld_so_conf_supplies_the_multiarch_dir(tmp_path: Path) -> None:
    """A Debian/Ubuntu host keeps libc in /usr/lib/<triplet>; the floor cannot name it.

    Built as a fake config tree rather than read off this host, so the case is
    covered on a non-multiarch machine too - which is exactly the asymmetry that
    let the gap ship: it is invisible on Arch and breaks every artifact on
    Ubuntu.
    """
    conf_d = tmp_path / "ld.so.conf.d"
    conf_d.mkdir()
    (conf_d / "x86_64-linux-gnu.conf").write_text(
        "# Multiarch support\n/usr/lib/x86_64-linux-gnu\n/lib/x86_64-linux-gnu\n"
    )
    conf = tmp_path / "ld.so.conf"
    conf.write_text(f"include {conf_d}/*.conf\n")

    assert diagnostics._ld_so_conf_dirs(conf) == ["/usr/lib/x86_64-linux-gnu", "/lib/x86_64-linux-gnu"]


def test_ld_so_conf_include_is_relative_to_the_including_file(tmp_path: Path) -> None:
    """A bare `include ld.so.conf.d/*.conf` must not glob the process CWD."""
    conf_d = tmp_path / "ld.so.conf.d"
    conf_d.mkdir()
    (conf_d / "local.conf").write_text("/opt/lib\n")
    conf = tmp_path / "ld.so.conf"
    conf.write_text("include ld.so.conf.d/*.conf\n")

    assert diagnostics._ld_so_conf_dirs(conf) == ["/opt/lib"]


def test_ld_so_conf_survives_an_include_cycle(tmp_path: Path) -> None:
    """A config that includes itself must terminate rather than hang the doctor."""
    conf = tmp_path / "ld.so.conf"
    conf.write_text(f"/usr/lib/first\ninclude {conf}\n")

    assert diagnostics._ld_so_conf_dirs(conf) == ["/usr/lib/first"]


def test_ld_so_conf_absent_yields_nothing(tmp_path: Path) -> None:
    """No loader config (musl, a stripped container) leaves the static floor alone."""
    assert diagnostics._ld_so_conf_dirs(tmp_path / "nope") == []


def test_ld_so_conf_ignores_comments_and_blank_lines(tmp_path: Path) -> None:
    conf = tmp_path / "ld.so.conf"
    conf.write_text("# a comment\n\n/usr/lib/real   # trailing\n   \n")

    assert diagnostics._ld_so_conf_dirs(conf) == ["/usr/lib/real"]


def test_host_lib_dirs_are_deduplicated() -> None:
    """ld.so.conf routinely repeats a floor entry; search order must stay stable."""
    assert len(diagnostics._HOST_LIB_DIRS) == len(set(diagnostics._HOST_LIB_DIRS))
    assert diagnostics._HOST_LIB_DIRS[0] == "/usr/lib"


def test_version_references_block_is_bounded() -> None:
    """The block parser stops at the next section instead of running to EOF.

    Not reachable through a real binary: ``objdump -p`` prints
    ``Version References`` last, so on every host binary a parser with no
    terminator produces the same answer as one with. What it would sweep in is
    right there in the same output though - ``Version definitions`` carries the
    nodes libc DEFINES, ``GLIBC_2.44`` among them on the host this was written
    on - so if ``objdump`` ever emits the two blocks the other way round, an
    unbounded parser silently resurrects the exact overcount this scan was
    rewritten to remove. Synthesised text rather than a synthesised binary: this
    parses ``objdump``'s output, so there are no hand-crafted ELF headers here.
    """
    dump = (
        "Dynamic Section:\n"
        "  NEEDED               libc.so.6\n"
        "\n"
        "Version References:\n"
        "  required from libc.so.6:\n"
        "    0x09691a75 0x00 03 GLIBC_2.4\n"
        "    0x0963cf85 0x00 02 GLIBC_PRIVATE\n"
        "\n"
        "Version definitions:\n"
        "1 0x01 0x0865f4e6 GLIBC_2.99\n"
        "\tGLIBC_2.98 \n"
    )

    assert diagnostics._required_glibc_nodes(dump) == {"2.4"}, (
        "the parser must stop at 'Version definitions:' - taking GLIBC_2.99 or GLIBC_2.98 means "
        "it ran past the block and is counting definitions again"
    )
    # GLIBC_PRIVATE carries no version digits, so it drops out by construction
    # rather than by a special case; an unversioned node has no dotted form to
    # compare against the ceiling.
    assert "PRIVATE" not in "".join(diagnostics._required_glibc_nodes(dump))
    assert diagnostics._required_glibc_nodes("Dynamic Section:\n  NEEDED libc.so.6\n") == frozenset()


@pytest.mark.unit
@requires_objdump
@pytest.mark.skipif(not _HOST_LIBC.exists(), reason="the format pin reads the host's libc.so.6")
def test_objdump_format_pin() -> None:
    """Pin the ``Version References`` format, and pin the trap it replaced.

    Every other fixture in this module reads real binaries through the real
    reader, so a binutils format change would move what they measure without
    any one of them naming the cause. This one names it. The assertions are
    structural rather than versioned so a host glibc bump cannot invalidate
    them: no literal version appears below.
    """
    listed = _objdump("-p", str(_HOST_LIBC))
    assert _VERNEED_HEADER in listed, (
        f"objdump -p no longer prints a {_VERNEED_HEADER!r} block for {_HOST_LIBC}; "
        "the scan reads its requirements from that block and would silently see none"
    )
    referenced = _version_references(listed)
    assert referenced, f"{_HOST_LIBC} listed no GLIBC_ node under {_VERNEED_HEADER}"
    assert referenced == diagnostics._required_glibc_nodes(listed), (
        "the scan's own block parser disagrees with this test's on real objdump output"
    )

    # The trap that produced the defect this test exists to prevent. objdump
    # parenthesises on a non-default version binding, which binutils sets for
    # compat DEFINITIONS as well as for undefined symbols, so a parenthesised
    # node is not evidence of a requirement. Asserted positively - a future
    # reader reaching for the parenthesis rule gets a red test, not a silent
    # regression.
    dumped = _objdump("-T", str(_HOST_LIBC))
    # A definition sits at a non-zero address in a named section; an import is
    # ``*UND*`` at address zero.
    definition = re.compile(r"^0*[1-9a-f][0-9a-f]*\s.*\s(\.\w[\w.]*)\s+.*\(GLIBC_\d+(?:\.\d+)+\)")
    definitions = [line for line in dumped.splitlines() if definition.match(line)]
    assert definitions, (
        f"no parenthesised line in objdump -T {_HOST_LIBC} is a definition; if that is genuinely "
        "true of this binutils then parenthesisation may separate required from defined here, but "
        "it did not on the toolchain this scan was written against and the scan must not rely on it"
    )

    parenthesised = _highest(re.findall(r"\(GLIBC_(\d+(?:\.\d+)+)\)", dumped))
    required = _highest(referenced)
    assert parenthesised is not None
    assert required is not None
    assert parenthesised > required, (
        f"{_HOST_LIBC} parenthesises up to {parenthesised} while requiring at most {required}; "
        "an equal or inverted pair would mean the discredited parenthesis rule no longer "
        "overcounts here and the discriminating test has nothing left to discriminate"
    )


@pytest.mark.unit
@requires_objdump
@pytest.mark.parametrize("overlay", _LOCALE_MATRIX, ids=[_locale_id(o) for o in _LOCALE_MATRIX])
def test_reader_output_is_locale_immune(monkeypatch: pytest.MonkeyPatch, overlay: dict[str, str]) -> None:
    """A translated host must not silently empty the reader's node set.

    ``Version References:`` is a gettext msgid in bfd with translations shipped
    for a dozen languages, and an unpinned reader finds none of them - which
    reads as "this artifact requires nothing", i.e. a BLOCK-severity all-clear
    over a tree the check never understood. The baseline comes from this
    module's own English-pinned parse, so a reader that stopped pinning cannot
    move the baseline along with itself.

    Each case first checks that the overlay really does translate on this host;
    where the translation is not installed there is nothing to prove and the
    case skips rather than passing vacuously.
    """
    reader = diagnostics._elf_reader()
    assert reader is not None
    baseline = _version_references(_objdump("-p", str(_CLEAN)))
    assert baseline, f"{_CLEAN} lists no GLIBC_ node under {_VERNEED_HEADER} even in English"

    probe_env = {**os.environ, **overlay}
    if _VERNEED_HEADER in _objdump("-p", str(_CLEAN), env=probe_env):
        pytest.skip(f"objdump does not translate {_VERNEED_HEADER!r} under {overlay}; nothing to pin here")

    for key, value in overlay.items():
        monkeypatch.setenv(key, value)
    info = diagnostics._read_elf(reader, _CLEAN)

    assert info is not None
    assert set(info.nodes) == baseline, (
        f"under {overlay} the reader saw {sorted(info.nodes)} instead of {sorted(baseline)}; the reader "
        "is matching a translated bfd string and would report every artifact as requiring nothing"
    )


@pytest.mark.unit
@requires_objdump
def test_object_files_are_not_counted_as_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A tree holding only non-dynamic ELFs cannot clear the zero-evidence floor.

    A relocatable ``.o`` has no dynamic section, so ``objdump -p`` prints an
    empty dump for it and it states no requirement at all. Counting one as a
    scanned artifact is what let an ``rm_work``-stripped tree that kept a single
    leftover ``<pn>/<pv>/build/foo.o`` return a confident PASS. The object is
    compiled at run time rather than synthesised: the scan shells out to
    ``objdump``, so a hand-crafted ELF header would only prove the fixture
    agrees with itself.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    work = _work_tree(cfg)
    build_dir = work / "zlib-native" / "1.0" / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    stray = build_dir / "stray.o"
    subprocess.run(
        ["gcc", "-x", "c", "-c", "-o", str(stray), "-"],
        input="int bakar_leak_fixture;\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if not stray.is_file() or stray.read_bytes()[:4] != b"\x7fELF":
        pytest.skip("no working C compiler here, so there is no relocatable object to plant")

    reader = diagnostics._elf_reader()
    assert reader is not None
    info = diagnostics._read_elf(reader, stray)
    assert info is not None, "a relocatable object must read as non-dynamic, not as unreadable"
    assert not info.dynamic

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.SKIP
    assert result.severity is Severity.INFO
    assert "holds no dynamically linked native artifact this scan could read" in result.message


@pytest.mark.unit
@requires_objdump
def test_zero_evidence_skip_still_names_unreadable_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unreadable ELF is reported even when nothing readable was found.

    ``scanned == 0`` with a non-empty unresolved list contradicts the SKIP's own
    rm_work explanation - rm_work leaves an empty tree, not a truncated one - so
    dropping the list would leave the operator with a message asserting the
    wrong cause and naming no path to look at.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    work = _work_tree(cfg)
    target_dir = work / "zlib-native" / "1.0" / "image"
    target_dir.mkdir(parents=True, exist_ok=True)
    # Truncated rather than non-ELF: _is_elf must accept it so the reader is
    # actually invoked and actually fails.
    truncated = target_dir / "half-written"
    truncated.write_bytes(_CLEAN.read_bytes()[:64])

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.SKIP
    assert result.severity is Severity.INFO
    assert str(truncated) in result.message, "the SKIP dropped the unreadable path it was handed"
    assert "could not be read" in result.message


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
    assert "1 dynamically linked native artifact(s) scanned" in result.message


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
    """The ``DT_NEEDED`` walk runs: a clean artifact leaks through its libc edge.

    The ceiling sits between the artifact's own highest requirement and libc's,
    so the artifact reads clean and only the edge can trip the gate. This is the
    sole coverage of the dependency walk, so its guards are deliberately narrow:
    they name a missing libc and a libc that requires nothing, and nothing else.
    The parenthesisation calibration that the discriminating half needs lives in
    ``test_dt_needed_edge_counts_requirements_only`` precisely so its skip cannot
    take this test down with it.
    """
    if not _HOST_LIBC.exists():
        pytest.skip(f"{_HOST_LIBC} is absent, so there is no libc edge to follow")
    required, _parenthesised = _host_max_nodes(_HOST_LIBC)
    if required is None:
        pytest.skip(f"{_HOST_LIBC} requires no glibc version node, so there is no ceiling to calibrate")

    cfg = _cfg(tmp_path)
    artifact = _place(_work_tree(cfg), "zlib-native", _CLEAN)

    _patch_host(monkeypatch, tmp_path, max_glibc=_ceiling_between(_CLEAN, _HOST_LIBC))
    tripped = diagnostics.check_uninative_leak(cfg)

    assert tripped.status is Status.FAIL
    assert tripped.severity is Severity.BLOCK
    assert "the artifact itself" not in tripped.message
    assert f"dependency libc.so.6 at {_HOST_LIBC}" in tripped.message
    assert f"{artifact} (recipe zlib-native)" in tripped.message


@pytest.mark.unit
@requires_objdump
def test_dt_needed_edge_counts_requirements_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The libc edge contributes what libc REQUIRES, and nothing more.

    Same tree as ``test_leak_via_dt_needed_edge``, one ceiling higher: at libc's
    highest ``DT_VERNEED`` requirement, strictly below its highest parenthesised
    node, the tree must go clean. Reverting the extraction to the parenthesised
    form makes libc contribute a node above this ceiling and turns the PASS into
    a FAIL. Both figures are read off the host at run time.
    """
    if not _HOST_LIBC.exists():
        pytest.skip(f"{_HOST_LIBC} is absent, so there is no libc edge to follow")
    required, parenthesised = _host_max_nodes(_HOST_LIBC)
    if required is None:
        pytest.skip(f"{_HOST_LIBC} requires no glibc version node, so there is no ceiling to calibrate")
    if parenthesised is None or parenthesised <= required:
        # An environment fact, not a defect: with no parenthesised node above
        # the highest real requirement the two extractions agree here and this
        # test would pass while discriminating nothing.
        pytest.skip(
            f"{_HOST_LIBC} parenthesises no node above its highest requirement {_dotted(required)}, "
            "so the corrected and parenthesised extractions are indistinguishable on this host"
        )

    cfg = _cfg(tmp_path)
    _place(_work_tree(cfg), "zlib-native", _CLEAN)

    _patch_host(monkeypatch, tmp_path, max_glibc=_dotted(required))
    clean = diagnostics.check_uninative_leak(cfg)

    assert clean.status is Status.PASS, (
        f"at libc's highest real requirement {_dotted(required)} the tree must be clean; a FAIL here "
        f"means the extraction is counting up to its highest parenthesised node {_dotted(parenthesised)}, "
        "which covers compat definitions the artifact never calls"
    )
    assert "libc.so.6" not in clean.message


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
@requires_objdump
def test_truncated_report_names_findings_deterministically(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """More findings than the report names, so which ones it names must be pinned.

    Not "run it twice and compare": ``os.walk`` hands back the same readdir order
    on every call over an unchanged tree in one process, so that assertion passes
    against an unsorted walk and proves nothing. Instead the tree carries one
    finding per recipe, more than ``_LEAK_REPORT_LIMIT`` of them, planted under
    names whose on-disk order is not lexicographic - the named subset is then a
    pinned prefix only if the walk itself is sorted. The walk's order is
    deterministic rather than lexicographic by full path (``os.walk`` is
    top-down); every artifact here sits at one depth, where the two coincide.
    """
    if not _HOST_LIBC.exists():
        pytest.skip(f"{_HOST_LIBC} is absent, so the libc edge cannot be sanctioned away")
    # Read through this module's own block parser, not through _read_elf: a
    # production bug that shrinks the node set must fail this test rather than
    # move the ceiling with itself and skip it.
    nodes = sorted(set(_parsed_nodes(_version_references(_objdump("-p", str(_CLEAN))))))
    assert len(nodes) >= 2, (
        f"{_CLEAN} requires {len(nodes)} distinct glibc node(s); this test needs two to place a ceiling "
        "that yields exactly one finding per planted recipe"
    )
    # Second-highest required node: exactly one node clears it, so each planted
    # recipe contributes exactly one finding and the report's cut lands between
    # recipes rather than inside one recipe's node list.
    ceiling = _dotted(nodes[-2])

    cfg = _cfg(tmp_path)
    work = _work_tree(cfg)
    planted = [_place(work, f"leak{index:02d}-native", _CLEAN) for index in range(diagnostics._LEAK_REPORT_LIMIT + 2)]
    if os.listdir(work) == sorted(os.listdir(work)):
        pytest.skip("this filesystem enumerates the planted names in lexicographic order already")

    _patch_host(monkeypatch, tmp_path, max_glibc=ceiling)
    # The libc edge is sanctioned away so every finding comes from an artifact's
    # own node and each recipe contributes exactly one.
    sanctioned_lib = cfg.resolved_tmpdir / "sysroots-uninative" / "lib"
    sanctioned_lib.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_HOST_LIBC, sanctioned_lib / _HOST_LIBC.name)
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", (str(sanctioned_lib),))

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert " more" in result.message
    named = re.findall(r"(\S+) \(recipe leak\d\d-native\)", result.message)
    assert named == [str(path) for path in sorted(planted)][: diagnostics._LEAK_REPORT_LIMIT]


@pytest.mark.unit
def test_absent_work_tree_skips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Nothing built yet is a SKIP naming the missing tree, not a PASS."""
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.SKIP
    assert "does not exist, so nothing has been built to scan" in result.message


@pytest.mark.unit
@requires_objdump
def test_empty_work_tree_skips_rather_than_claiming_a_clean_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tree that exists but yields nothing readable cannot support a PASS.

    This is the shape an ``rm_work`` build leaves behind, whether the class was
    inherited from bakar's own config, from the distro, or from local.conf - so
    the condition is derived from what the walk read rather than from a config
    flag that only covers one of those causes.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    _work_tree(cfg).mkdir(parents=True, exist_ok=True)

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.SKIP
    assert result.severity is Severity.INFO
    assert "holds no dynamically linked native artifact this scan could read" in result.message
    assert "rm_work" in result.message
    # Rich parses a message's markup, so a literal bracket pair would be eaten.
    assert "[" not in result.message


@pytest.mark.unit
@requires_objdump
def test_one_readable_artifact_still_reaches_the_scan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Zero is the floor: a single readable artifact is evidence, so the walk reports."""
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    _place(_work_tree(cfg), "zlib-native", _CLEAN)

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.PASS
    assert "native artifact(s)" in result.message


@pytest.mark.unit
def test_empty_tree_skip_does_not_shadow_the_absent_work_tree_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With nothing built at all, the missing tree is the reason reported."""
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.SKIP
    assert "does not exist, so nothing has been built to scan" in result.message
    # Not a bare "rm_work" search: pytest's tmp_path carries this test's name.
    assert "holds no dynamically linked native artifact" not in result.message


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


# --- dependency-resolution confinement -------------------------------------
#
# The module docstring's rule against synthesised ELF is about ELF BYTES: a
# hand-crafted header would only prove the reader mock agrees with itself. It
# does not extend to resolution logic, which never touches a byte of ELF. No
# real toolchain can be made to emit ``NEEDED /etc/shadow``, so these tests
# drive ``_resolve_needed`` directly and reach the end-to-end reporting path by
# monkeypatching ``_read_elf`` to return crafted ``_ElfInfo`` values.


@pytest.fixture
def is_file_spy(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every path ``Path.is_file`` is called on.

    The lexical-then-resolved ordering inside ``_resolve_needed`` is invisible
    in the return value - both orders refuse the same candidates. This spy is
    the only thing that distinguishes them: a refused candidate must never be
    stat'd, or the refusal is distinguishable from a miss and the oracle is
    still open.
    """
    seen: list[Path] = []
    real = Path.is_file

    def recording(self: Path, *args: object, **kwargs: object) -> bool:
        seen.append(self)
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "is_file", recording)
    return seen


@pytest.mark.unit
def test_absolute_soname_is_refused_not_resolved(is_file_spy: list[Path]) -> None:
    """``NEEDED /etc/shadow`` must not resolve: the join discards the directory."""
    permitted = (Path("/usr/lib"),)

    resolved, refused = diagnostics._resolve_needed("/etc/shadow", ["/usr/lib"], permitted)

    assert resolved is None
    assert refused is True
    assert Path("/etc/shadow") not in is_file_spy
    assert all(path.is_relative_to("/usr/lib") for path in is_file_spy), is_file_spy


@pytest.mark.unit
def test_runpath_alone_reaches_no_arbitrary_path(is_file_spy: list[Path]) -> None:
    """A bare soname plus a hostile RUNPATH is refused with no separator in sight."""
    permitted = (Path("/usr/lib"),)

    resolved, refused = diagnostics._resolve_needed("shadow", ["/etc"], permitted)

    assert resolved is None
    assert refused is True
    assert is_file_spy == []


@pytest.mark.unit
def test_traversal_out_of_a_permitted_root_is_refused(is_file_spy: list[Path]) -> None:
    """``..`` chains are folded away before the containment test, not after."""
    permitted = (Path("/usr/lib"),)

    resolved, refused = diagnostics._resolve_needed("../../etc/shadow", ["/usr/lib"], permitted)

    assert resolved is None
    assert refused is True
    assert is_file_spy == []


@pytest.mark.unit
def test_permitted_root_containment_is_per_component() -> None:
    """``/usr/libexec`` is not inside ``/usr/lib``; a prefix test would say it is."""
    assert not diagnostics._lexically_within(Path("/usr/libexec/foo.so"), (Path("/usr/lib"),))
    assert diagnostics._lexically_within(Path("/usr/lib/foo.so"), (Path("/usr/lib"),))


@pytest.mark.unit
def test_doubled_leading_slash_stays_in_root() -> None:
    """``normpath`` keeps exactly two leading slashes; a legitimate lib must still resolve."""
    assert diagnostics._lexically_within(Path("//usr/lib/libz.so.1"), (Path("/usr/lib"),))


@pytest.mark.unit
def test_bare_soname_under_a_permitted_root_still_resolves(tmp_path: Path) -> None:
    """The common case is untouched: a plain soname found under a permitted root."""
    libdir = tmp_path / "usr" / "lib"
    libdir.mkdir(parents=True)
    (libdir / "libz.so.1").write_bytes(b"\x7fELF")

    resolved, refused = diagnostics._resolve_needed("libz.so.1", [str(libdir)], diagnostics._resolve_roots([libdir]))

    # realpath on both sides: _resolve_needed returns the RESOLVED path, and
    # tmp_path is only symlink-free by accident of this host.
    assert resolved == Path(os.path.realpath(libdir / "libz.so.1"))
    assert refused is False


@pytest.mark.unit
def test_path_qualified_soname_inside_a_root_resolves(tmp_path: Path) -> None:
    """A path-qualified ``DT_NEEDED`` is legal ELF and must still resolve.

    GNU ld emits it for any library linked by absolute path with no
    ``DT_SONAME``. Refusing it outright - a guard on the soname containing a path
    separator, tried once and reverted - demotes a genuine leak from BLOCK to WARN.
    """
    work = tmp_path / "work"
    target = work / "foo-native" / "1.0" / "libbar.so"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\x7fELF")

    resolved, refused = diagnostics._resolve_needed(str(target), ["/usr/lib"], diagnostics._resolve_roots([work]))

    assert resolved == Path(os.path.realpath(target))
    assert refused is False


@pytest.mark.unit
def test_one_refused_candidate_does_not_abort_the_lookup(tmp_path: Path) -> None:
    """A foreign RPATH ahead of the host directories must not lose the real hit.

    Measured on a real tree: ``pseudo-native``'s ``pseudodb`` carries the
    relative RPATH ``../../sqlite3-native/usr/lib`` while its dependencies all
    resolve under ``/usr/lib`` on the next iteration.
    """
    libdir = tmp_path / "usr" / "lib"
    libdir.mkdir(parents=True)
    (libdir / "libz.so.1").write_bytes(b"\x7fELF")

    resolved, refused = diagnostics._resolve_needed(
        "libz.so.1", ["/etc", "../relative", str(libdir)], diagnostics._resolve_roots([libdir])
    )

    assert resolved == Path(os.path.realpath(libdir / "libz.so.1"))
    assert refused is False


@pytest.mark.unit
def test_symlink_out_of_a_permitted_root_is_refused(tmp_path: Path) -> None:
    """A link inside a permitted root pointing out of one is caught after realpath."""
    libdir = tmp_path / "usr" / "lib"
    libdir.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "libz.so.1").write_bytes(b"\x7fELF")
    (libdir / "libz.so.1").symlink_to(outside / "libz.so.1")

    resolved, refused = diagnostics._resolve_needed("libz.so.1", [str(libdir)], diagnostics._resolve_roots([libdir]))

    assert resolved is None
    assert refused is True


def _crafted_reader(needed: tuple[str, ...], nodes: frozenset[str] = frozenset({"2.2.5"})) -> object:
    """A ``_read_elf`` stand-in declaring ``needed`` for every artifact.

    The default node set is not decoration. A real dynamically linked x86-64
    artifact requires at least one glibc version node, and ``_unchecked_reason``
    treats an artifact requiring none as unable to bear on the ceiling - so a
    stand-in declaring an empty node set would silently opt every test using it
    out of the unresolved branch it is trying to exercise. ``2.2.5`` is the
    oldest node glibc emits and sits below every ceiling used here.
    """

    def fake(reader: str, path: Path) -> diagnostics._ElfInfo:
        return diagnostics._ElfInfo(dynamic=True, nodes=nodes, needed=needed, runpaths=())

    return fake


@pytest.mark.unit
@requires_objdump
@pytest.mark.parametrize("soname", ["/etc/shadow", "/etc/bakar-does-not-exist"], ids=["exists", "absent"])
def test_out_of_scope_dependency_is_reported_and_never_stat_ed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    is_file_spy: list[Path],
    soname: str,
) -> None:
    """An out-of-scope dependency reads the same whether or not the path exists.

    That equality is the point: differing outcomes are exactly the file-existence
    oracle the confinement closes. The entry still joins ``unresolved`` - dropping
    it would let a crafted artifact hide a real edge.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    artifact = _place(_work_tree(cfg), "zlib-native", _CLEAN)
    monkeypatch.setattr(diagnostics, "_read_elf", _crafted_reader((soname,)))

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    expected = (
        f"{artifact} (recipe zlib-native) declares {soname}, which lands outside the scanned roots and is out of scope"
    )
    assert expected in result.message
    assert "resolves to no file" not in result.message
    assert not [path for path in is_file_spy if Path(os.path.normpath(path)).is_relative_to("/etc")], is_file_spy


@pytest.mark.unit
@requires_objdump
def test_unresolved_and_out_of_scope_read_differently(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing library and a refused one are different facts, worded differently."""
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    _place(_work_tree(cfg), "zlib-native", _CLEAN)
    monkeypatch.setattr(diagnostics, "_read_elf", _crafted_reader(("libbakar-absent.so.9", "/etc/shadow")))

    result = diagnostics.check_uninative_leak(cfg)

    assert "declares libbakar-absent.so.9, which resolves to no file" in result.message
    assert "declares /etc/shadow, which lands outside the scanned roots and is out of scope" in result.message


@pytest.mark.unit
def test_a_refused_candidate_never_reaches_the_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lexical stage runs BEFORE symlink resolution, and the order matters.

    ``realpath`` on an attacker-named path stats its intermediate components -
    a weaker oracle than ``is_file`` but still one - so a candidate refused
    lexically must never be handed to it. Nothing in the return value
    distinguishes the two orderings; this spy is what does.
    """
    seen: list[str] = []
    real = os.path.realpath

    def recording(path: object, *args: object, **kwargs: object) -> str:
        seen.append(str(path))
        return real(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os.path, "realpath", recording)

    resolved, refused = diagnostics._resolve_needed("/etc/shadow", ["/usr/lib"], (Path("/usr/lib"),))

    assert resolved is None
    assert refused is True
    assert seen == [], "a lexically refused candidate was resolved against the filesystem"


# --- neutralization of artifact-derived report text -------------------------
#
# Same reasoning as the block above: none of this is about ELF bytes. A crafted
# soname or a crafted directory name is reached by monkeypatching ``_read_elf``
# and by naming real directories, neither of which asks a toolchain to emit
# something no toolchain emits.


def _render(result: CheckResult) -> str:
    """Render one CheckResult through the real doctor table and return the text.

    Goes through ``_print_diagnosis`` rather than a hand-built table because the
    whole point is that ``r.message`` lands in a markup-enabled cell there. A
    local imitation would keep passing if that ever changed.
    """
    from rich.console import Console

    from bakar.commands._helpers import _print_diagnosis

    buffer = io.StringIO()
    console = Console(file=buffer, width=400, force_terminal=False)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("bakar.commands.console", console)
        _print_diagnosis([result])
    return buffer.getvalue()


@pytest.mark.unit
def test_neutralized_escapes_markup() -> None:
    """A closing tag must survive as text, not as markup Rich tries to close."""
    assert diagnostics._neutralized("foo[/]bar") == r"foo\[/]bar"
    assert diagnostics._neutralized("[on red blink]") == r"\[on red blink]"


@pytest.mark.unit
def test_neutralized_strips_control_characters() -> None:
    """ESC, C0 and C1 go, because ``\\S+`` matches them and OSC-0 retitles a terminal."""
    assert diagnostics._neutralized("lib\x1b]0;pwned\x07z.so") == "lib]0;pwnedz.so"
    assert diagnostics._neutralized("a\x00b\x7fc\x9fd") == "abcd"


@pytest.mark.unit
def test_neutralized_bounds_length() -> None:
    """One crafted name must not flood a report the operator has to read."""
    rendered = diagnostics._neutralized("x" * 5000)

    assert len(rendered) == diagnostics._ARTIFACT_TEXT_LIMIT
    assert diagnostics._ELISION in rendered


@pytest.mark.unit
def test_the_bound_clears_a_real_work_tree_path() -> None:
    """A real path must never be truncated: the truncation names nothing on disk.

    Measured on one native work tree, 3,520 ELF artifacts exceeded the old
    240-character bound and the longest path ran to 445 - ``sysroot-destdir/``
    embeds a second absolute copy of the work path and so roughly doubles it.
    """
    longest_measured = "/" + "a" * 444

    assert diagnostics._neutralized(longest_measured) == longest_measured


@pytest.mark.unit
def test_the_bound_elides_the_middle_not_the_tail() -> None:
    """The basename survives, so an over-long name is still recognisable.

    Lopping off the tail leaves a directory prefix that matches nothing the
    operator can look up, and reads as if that were the whole path.
    """
    crafted = "/work/" + "x" * 5000 + "/libcrafted.so.1"

    rendered = diagnostics._neutralized(crafted)

    assert rendered.startswith("/work/xxx")
    assert rendered.endswith("/libcrafted.so.1")


@pytest.mark.unit
@requires_objdump
def test_crafted_soname_does_not_abort_the_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``[/]`` in a declared soname renders literally instead of raising MarkupError.

    Unescaped it raises ``rich.errors.MarkupError: closing tag '[/]' has nothing
    to close`` out of ``_print_diagnosis``, which discards the entire doctor
    report - every finding already made - rather than one row.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    _place(_work_tree(cfg), "zlib-native", _CLEAN)
    monkeypatch.setattr(diagnostics, "_read_elf", _crafted_reader(("lib[/]z.so.1\x1b]0;pwned\x07",)))

    result = diagnostics.check_uninative_leak(cfg)

    assert "\x1b" not in result.message
    assert r"lib\[/]z.so.1]0;pwned" in result.message
    assert "lib[/]z.so.1" in _render(result)


@pytest.mark.unit
@requires_objdump
def test_crafted_artifact_path_does_not_abort_the_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The artifact path is artifact-derived too, and spans directory names.

    A single component cannot hold a ``/``, so the ``[/]`` a crafted tree needs
    is spelled across two of them - which is why escaping the recipe name alone
    would not be enough.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    target_dir = _work_tree(cfg) / "zlib[" / "]native" / "1.0"
    target_dir.mkdir(parents=True)
    shutil.copy2(_CLEAN, target_dir / _CLEAN.name)
    monkeypatch.setattr(diagnostics, "_read_elf", _crafted_reader(("libbakar-absent.so.9",)))

    result = diagnostics.check_uninative_leak(cfg)

    assert r"zlib\[/]native" in result.message
    assert "zlib[/]native" in _render(result)


@pytest.mark.unit
@requires_objdump
def test_resolved_dependency_path_is_neutralized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The path a dependency resolved to is artifact-derived through the join."""
    _patch_host(monkeypatch, tmp_path, max_glibc="2.0")
    cfg = _cfg(tmp_path)
    work = _work_tree(cfg)
    artifact = _place(work, "zlib-native", _CLEAN)
    dep_dir = work / "dep[" / "]lib"
    dep_dir.mkdir(parents=True)
    dependency = dep_dir / "libz.so.1"
    dependency.write_bytes(b"\x7fELF")

    def fake(reader: str, path: Path) -> diagnostics._ElfInfo:
        if path == artifact:
            return diagnostics._ElfInfo(dynamic=True, nodes=frozenset(), needed=(str(dependency),), runpaths=())
        return diagnostics._ElfInfo(dynamic=True, nodes=frozenset({"2.99"}), needed=(), runpaths=())

    monkeypatch.setattr(diagnostics, "_read_elf", fake)

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert f"dependency {escape(str(dependency))} at {escape(str(dependency))}" in result.message
    assert str(dependency) not in result.message
    assert "dep[/]lib" in _render(result)


@pytest.mark.unit
@requires_objdump
def test_recipe_name_reaches_message_and_fix_hint_neutralized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The recipe name is a work-tree path component, so it is artifact-derived.

    It reaches the operator twice - inside the message and inside the
    remediation hint - and ``_print_diagnosis`` renders both through markup.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc="2.0")
    cfg = _cfg(tmp_path)
    _place(_work_tree(cfg), "[bold red]zlib-native", _CLEAN)
    monkeypatch.setattr(
        diagnostics,
        "_read_elf",
        lambda reader, path: diagnostics._ElfInfo(dynamic=True, nodes=frozenset({"2.99"}), needed=(), runpaths=()),
    )

    result = diagnostics.check_uninative_leak(cfg)

    assert result.fix_hint is not None
    assert r"(recipe \[bold red]zlib-native)" in result.message
    assert r"cleansstate \[bold red]zlib-native" in result.fix_hint
    rendered = _render(result)
    assert "(recipe [bold red]zlib-native)" in rendered
    assert "cleansstate [bold red]zlib-native" in rendered


@pytest.mark.unit
def test_neutralization_does_not_reach_the_values_the_scan_compares() -> None:
    """Only what is printed is neutralized; what is reasoned about stays byte-exact.

    ``_read_elf`` returning escaped text would change the ``dep_cache`` key, the
    containment tests and the node comparisons - the scan would be checking a
    string no artifact declared.
    """
    assert "_neutralized" not in inspect.getsource(diagnostics._read_elf)
    assert "_neutralized" not in inspect.getsource(diagnostics._resolve_needed)


# --- Reading only regular files -------------------------------------------
#
# A FIFO is the one entry ``os.walk`` lists among its files that an ELF-magic
# read cannot survive: opening one for reading blocks until a writer appears,
# and ``_is_elf``'s open carries no timeout. A recipe or an upstream test suite
# leaving a named pipe under its work directory is enough - tar and pseudo both
# preserve FIFOs, so an sstate restore reproduces it. The module's pytest
# timeout is what turns a regression here into a failure rather than a hung
# suite.


@pytest.mark.unit
def test_is_elf_refuses_a_fifo(tmp_path: Path) -> None:
    """The type gate answers before the open, so the blocking read never happens."""
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    assert diagnostics._is_elf(fifo) is False


@pytest.mark.unit
def test_is_elf_refuses_a_directory(tmp_path: Path) -> None:
    """Non-regular is the condition, not FIFO specifically."""
    assert diagnostics._is_elf(tmp_path) is False


@pytest.mark.unit
@requires_objdump
def test_a_fifo_beside_an_artifact_does_not_hang_or_hide_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The pipe is skipped and the real artifact beside it still counts as scanned."""
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    artifact = _place(_work_tree(cfg), "zlib-native", _CLEAN)
    os.mkfifo(artifact.parent / "pipe")

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.PASS
    assert "in 1 dynamically linked native artifact(s)" in result.message


@pytest.mark.unit
@requires_objdump
def test_a_tree_of_only_fifos_skips_rather_than_passing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Skipping the pipe leaves no evidence, and no evidence is the zero-evidence SKIP.

    Not a PASS: the walk read nothing, so it cannot vouch for the tree.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    pipe_dir = _work_tree(cfg) / "zlib-native" / "1.0" / "image" / "usr" / "bin"
    pipe_dir.mkdir(parents=True, exist_ok=True)
    os.mkfifo(pipe_dir / "pipe")

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.SKIP
    assert result.status is not Status.PASS
    assert "holds no dynamically linked native artifact this scan could read" in result.message


# --- the narrowed unresolved warning ----------------------------------------
#
# Measured on a real 236-recipe tree: 118,549 DT_NEEDED entries, 118,361
# resolved, 188 not - so the WARN branch fired on every healthy build and the
# channel carried no information. ``_unchecked_reason`` excludes 183 of the 188
# through three named predicates, leaving 5 to raise WARN: 3 reported as
# resolving to no file, and 2 the confinement refuses and reports out of scope.
# The sweep and its classification live in the change's design, and the counts
# are restated where the predicates are defined so a later reader can re-run
# them.
#
# The predicates are what these tests pin. The two things that must NOT happen
# are that a genuinely missing library goes quiet, and that an entry the
# confinement refused is swallowed - the second is not hypothetical, because the
# `shadow-native` shape matches two predicates on its face.


@pytest.mark.unit
@requires_objdump
def test_a_dependency_matching_no_predicate_still_warns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The branch is narrowed, not suppressed: a real miss still reaches the operator."""
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    _place(_work_tree(cfg), "zlib-native", _CLEAN)
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", ())
    monkeypatch.setattr(diagnostics, "_read_elf", _crafted_reader(("libbakar-absent.so.9",)))

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert "declares libbakar-absent.so.9, which resolves to no file" in result.message


@pytest.mark.unit
@requires_objdump
def test_a_soname_the_build_provides_elsewhere_is_not_unchecked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The RPATH names a staging location; the walk reads the provider anyway.

    The provider is a real ELF, and that is the whole justification: the index
    holds only what the walk READ, so a name carried by a script, a stamp or a
    zero-byte fixture must NOT suppress anything - see
    ``test_a_name_carried_by_no_elf_does_not_suppress_the_warning``. Both
    artifacts here declare the soname and both are excluded, which is why the
    count is two.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    work = _work_tree(cfg)
    _place(work, "readline-native", _CLEAN)
    provider = work / "ncurses-native" / "1.0" / "recipe-sysroot-native" / "usr" / "lib"
    provider.mkdir(parents=True)
    shutil.copy2(_CLEAN, provider / "libncurses.so.5")
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", ())
    monkeypatch.setattr(diagnostics, "_read_elf", _crafted_reader(("libncurses.so.5",)))

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.PASS
    assert "libncurses.so.5" not in result.message
    assert "2 provided elsewhere under the work tree" in result.message


@pytest.mark.unit
@requires_objdump
def test_a_foreign_platform_artifact_is_not_unchecked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A NetBSD fixture in an upstream source tree cannot load under any uninative loader."""
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    fixture_dir = _work_tree(cfg) / "rust-native" / "1.0" / "sources" / "test" / "Inputs"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "hello-netbsd").write_bytes(_foreign_elf_header())
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", ())
    monkeypatch.setattr(diagnostics, "_read_elf", _crafted_reader(("libc.so.12",)))

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.PASS
    assert "libc.so.12" not in result.message
    assert "1 declared by a non-host-platform artifact" in result.message


@pytest.mark.unit
@requires_objdump
def test_an_unreadable_header_reports_rather_than_suppresses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The fail-open guard must fail towards REPORTING, and the polarity is easy to read backwards.

    `_host_platform_elf` returns True when it cannot read a header, and the
    caller suppresses on `not _host_platform_elf(...)` - so missing evidence
    declines to suppress and the dependency still reaches the operator. A
    reviewer read this the other way and reported it as silently swallowing a
    warning; nothing at the caller level pinned which way round it goes, which
    is what this asserts. Getting it backwards would make an unreadable artifact
    silence its own dependency inside a BLOCK-severity gate.

    The header is short rather than permission-denied on purpose: chmod 000
    would make `_is_elf` skip the artifact during the walk, so the scan would
    never reach the predicate under test, and the test would pass while proving
    nothing.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    stub = _work_tree(cfg) / "zlib-native" / "1.0" / "image" / "usr" / "lib"
    stub.mkdir(parents=True)
    # Passes _is_elf's four-byte magic test, too short for _host_platform_elf's
    # twenty-byte header read.
    (stub / "libtruncated.so").write_bytes(b"\x7fELF")
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", ())
    monkeypatch.setattr(diagnostics, "_read_elf", _crafted_reader(("libbakar-absent.so.9",)))

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert "libbakar-absent.so.9" in result.message
    assert "non-host-platform" not in result.message


@pytest.mark.unit
@requires_objdump
def test_an_artifact_requiring_no_glibc_node_is_not_unchecked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Nothing it declares can bear on a glibc ceiling it states no requirement against."""
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    _place(_work_tree(cfg), "zlib-native", _CLEAN)
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", ())
    monkeypatch.setattr(diagnostics, "_read_elf", _crafted_reader(("libbakar-absent.so.9",), nodes=frozenset()))

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.PASS
    assert "1 declared by an artifact stating no glibc requirement" in result.message


@pytest.mark.unit
@requires_objdump
def test_an_out_of_scope_dependency_survives_every_predicate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The measured `shadow-native` shape: refused by confinement, and both predicates fit.

    ``libsubid.so.5`` is staged under ``sysroot-destdir/`` and BOTH sonames its
    absolute foreign RUNPATH refuses exist inside shadow-native's own work
    directory - so a location rule and the provided-elsewhere rule each swallow
    exactly the four edges that are the confinement's only operator-visible
    output. They must not: the rule only ever sees an entry the scan looked for
    and did not find.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    staged = _work_tree(cfg) / "shadow-native" / "4.18" / "sysroot-destdir" / "usr" / "lib"
    staged.mkdir(parents=True)
    shutil.copy2(_CLEAN, staged / "libsubid.so.5")
    own = _work_tree(cfg) / "shadow-native" / "4.18" / "image" / "usr" / "lib"
    own.mkdir(parents=True)
    for soname in ("libattr.so.1", "libbsd.so.0"):
        # Real ELFs, or the provided-elsewhere predicate would not fit them at
        # all and the test would stop covering the trap it is named for.
        shutil.copy2(_CLEAN, own / soname)
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", ())
    monkeypatch.setattr(
        diagnostics,
        "_read_elf",
        lambda reader, path: diagnostics._ElfInfo(
            dynamic=True,
            nodes=frozenset({"2.2.5"}),
            needed=("libattr.so.1", "libbsd.so.0"),
            runpaths=("/etc",),
        ),
    )

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert "declares libattr.so.1, which lands outside the scanned roots and is out of scope" in result.message
    assert "declares libbsd.so.0, which lands outside the scanned roots and is out of scope" in result.message
    assert "not counted as unchecked" not in result.message


@pytest.mark.unit
@requires_objdump
def test_an_excluded_dependency_is_still_recorded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Narrowing the warning must not hide the fact that an edge went unfollowed."""
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    _place(_work_tree(cfg), "zlib-native", _CLEAN)
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", ())
    monkeypatch.setattr(
        diagnostics,
        "_read_elf",
        _crafted_reader(("libbakar-absent.so.9", "libbakar-other.so.1"), nodes=frozenset()),
    )

    result = diagnostics.check_uninative_leak(cfg)

    assert "2 further declared dependenc(y/ies) resolved to no file and are not counted as unchecked" in result.message


@pytest.mark.unit
def test_the_predicates_never_reach_the_filesystem_with_a_crafted_soname(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, is_file_spy: list[Path]
) -> None:
    """The provided-elsewhere test is a name lookup, so it joins and stats nothing.

    A "search the recipe's tree for this soname" helper would have been a second
    unbounded join on an artifact-controlled string, reopening the door the
    confinement closes. The spy is what proves this one is not.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    _place(_work_tree(cfg), "zlib-native", _CLEAN)
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", ())
    monkeypatch.setattr(diagnostics, "_read_elf", _crafted_reader(("/etc/shadow", "../../../etc/passwd")))

    diagnostics.check_uninative_leak(cfg)

    escapees = [path for path in is_file_spy if not Path(os.path.normpath(path)).is_relative_to(tmp_path)]
    assert escapees == [], escapees


@pytest.mark.unit
def test_host_platform_elf_reads_the_machine_and_abi_fields(tmp_path: Path) -> None:
    """A crafted header is legitimate here: this helper reads two header fields.

    The module's no-synthesised-ELF rule is about proving a reader mock agrees
    with itself, which needs a whole dynamic section. ``_host_platform_elf``
    reads ``e_ident[EI_OSABI]`` and ``e_machine`` and nothing else, and no
    toolchain on this host can be asked to emit a NetBSD SPARC binary.
    """
    foreign = tmp_path / "foreign"
    foreign.write_bytes(_foreign_elf_header())

    assert diagnostics._host_platform_elf(foreign) is False
    assert diagnostics._host_platform_elf(_CLEAN) is True


@pytest.mark.unit
def test_host_platform_elf_fails_open_on_an_unreadable_header(tmp_path: Path) -> None:
    """Missing evidence must never be grounds to stay silent about a dependency."""
    truncated = tmp_path / "truncated"
    truncated.write_bytes(b"\x7fELF")

    assert diagnostics._host_platform_elf(truncated) is True
    assert diagnostics._host_platform_elf(tmp_path / "absent") is True


@pytest.mark.unit
@requires_objdump
def test_the_block_verdict_records_the_exclusion_too(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A leak verdict is the one the operator acts on, so it carries the record too."""
    _patch_host(monkeypatch, tmp_path, max_glibc="2.0")
    cfg = _cfg(tmp_path)
    work = _work_tree(cfg)
    _place(work, "zlib-native", _CLEAN)
    provider = work / "ncurses-native" / "1.0" / "recipe-sysroot-native" / "usr" / "lib"
    provider.mkdir(parents=True)
    shutil.copy2(_CLEAN, provider / "libncurses.so.5")
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", ())
    monkeypatch.setattr(
        diagnostics,
        "_read_elf",
        _crafted_reader(("libncurses.so.5",), nodes=frozenset({"2.34"})),
    )

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "2 provided elsewhere under the work tree" in result.message


# --- what neutralization has to survive on the way OUT of the process --------
#
# Everything below fails on an encode or on a rendered layout, not on a
# substring of ``r.message``. ``_render`` writes into an ``io.StringIO``, which
# holds ``str`` and never encodes, so it CANNOT catch a character that is only
# fatal at encode time - and the doctor gate encodes first, in
# ``diag_path.write_text``, before anything is rendered at all.


@pytest.mark.unit
def test_neutralized_strips_every_format_character() -> None:
    """Walks the whole code space against ``unicodedata`` rather than a sample.

    ``_CONTROL_RE`` spells the ``Cf`` ranges out because ``re`` has no category
    escape, so the class is a snapshot of one Unicode version. This is what
    turns the next assignment into a failure here instead of a bidi override
    reaching the operator's terminal.
    """
    import unicodedata

    missed = [
        code
        for code in range(0x110000)
        if unicodedata.category(chr(code)) == "Cf" and diagnostics._CONTROL_RE.sub("", chr(code)) != ""
    ]

    assert missed == [], [hex(code) for code in missed]


@pytest.mark.unit
@pytest.mark.parametrize(
    "char",
    ["\u202e", "\u202d", "\u202b", "\u2066", "\u2069", "\u200b", "\u200e", "\u200f", "\u00ad", "\ufeff"],
    ids=["rlo", "lro", "rle", "lri", "pdi", "zwsp", "lrm", "rlm", "shy", "bom"],
)
def test_neutralized_strips_the_bidi_and_zero_width_controls(char: str) -> None:
    """A name that renders as a different path is a forged finding, not a garbled one."""
    assert diagnostics._neutralized(f"lib{char}z.so") == "libz.so"


@pytest.mark.unit
@pytest.mark.parametrize("char", ["\u2028", "\u2029"], ids=["ls", "ps"])
def test_a_line_separator_does_not_split_a_rendered_row(monkeypatch: pytest.MonkeyPatch, char: str) -> None:
    """Rich breaks the line on these, so one crafted name would render as two rows."""
    assert diagnostics._neutralized(f"lib{char}z.so") == "libz.so"


@pytest.mark.unit
@requires_objdump
def test_an_undecodable_filename_does_not_kill_the_doctor_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A filesystem byte that is not UTF-8 must not take the gate down with it.

    ``os.walk`` decodes such a byte with ``surrogateescape``, yielding a lone
    surrogate that no UTF-8 encoder accepts. ``_run_doctor_gate`` writes the
    report to ``diagnosis.txt`` BEFORE rendering it, so an unstripped surrogate
    raises ``UnicodeEncodeError`` there and aborts the gate and the build.

    Rendering through ``io.StringIO`` cannot catch this: a ``StringIO`` holds
    ``str`` and never encodes. Both halves below therefore encode for real - a
    ``write_text`` to disk, and a console over a UTF-8 byte stream.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    target_dir = _work_tree(cfg) / os.fsdecode(b"zlib\xff-native") / "1.0"
    target_dir.mkdir(parents=True)
    shutil.copy2(_CLEAN, target_dir / _CLEAN.name)
    monkeypatch.setattr(diagnostics, "_read_elf", _crafted_reader(("libbakar-absent.so.9",)))

    result = diagnostics.check_uninative_leak(cfg)

    (tmp_path / "diagnosis.txt").write_text(result.message)
    assert result.fix_hint is not None
    (tmp_path / "hint.txt").write_text(result.fix_hint)

    from rich.console import Console

    from bakar.commands._helpers import _print_diagnosis

    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", write_through=True)
    console = Console(file=stream, width=400, force_terminal=False)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("bakar.commands.console", console)
        _print_diagnosis([result])


@pytest.mark.unit
def test_a_surrogate_is_stripped_rather_than_replaced() -> None:
    """The whole surrogate block goes, not just the ``surrogateescape`` sub-range."""
    assert diagnostics._neutralized("lib\udcffz.so") == "libz.so"
    assert diagnostics._neutralized("lib\ud800z.so") == "libz.so"
    diagnostics._neutralized("lib\udcffz.so").encode("utf-8")


# --- the report's entry boundary --------------------------------------------


@pytest.mark.unit
@requires_objdump
def test_a_crafted_artifact_path_cannot_forge_a_second_finding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """One leaked artifact must render as one finding, whatever it is named.

    Measured: a directory named ``a) reaches GLIBC_2.99 via the artifact
    itself; `` (trailing space) turned one leak into two report entries under a
    ``"; "`` join, the fabricated one naming ``/usr/lib/libc.so.6``, with only
    the header count as a tell. The soname is not a vector - ``_ELF_NEEDED_RE``
    drops any name containing whitespace - but a path component carries it.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc="2.0")
    cfg = _cfg(tmp_path)
    forged = "a) reaches GLIBC_2.99 via the artifact itself; "
    target_dir = _work_tree(cfg) / forged / "1.0"
    target_dir.mkdir(parents=True)
    shutil.copy2(_CLEAN, target_dir / _CLEAN.name)
    monkeypatch.setattr(
        diagnostics,
        "_read_elf",
        lambda reader, path: diagnostics._ElfInfo(dynamic=True, nodes=frozenset({"2.99"}), needed=(), runpaths=()),
    )

    result = diagnostics.check_uninative_leak(cfg)

    assert result.message.count(diagnostics._ENTRY_SEPARATOR) == 0
    assert "1 glibc version node(s)" in result.message


@pytest.mark.unit
def test_the_entry_separator_cannot_survive_neutralization() -> None:
    """The invariant the boundary rests on, stated as one assertion."""
    separator = diagnostics._ENTRY_SEPARATOR.strip()

    assert separator
    assert separator not in diagnostics._neutralized(f"lib{separator}z.so")


@pytest.mark.unit
def test_leak_report_separates_on_the_unforgeable_character() -> None:
    """Two entries, two boundaries; the tail summary uses the same separator."""
    joined = diagnostics._leak_report(["one", "two"])

    assert joined == f"one{diagnostics._ENTRY_SEPARATOR}two"

    truncated = diagnostics._leak_report([str(index) for index in range(diagnostics._LEAK_REPORT_LIMIT + 3)])

    assert truncated.endswith(f"{diagnostics._ENTRY_SEPARATOR}and 3 more")


# --- resolution is done ONCE, and the resolved value is what is used ---------


@pytest.mark.unit
def test_resolve_needed_returns_the_resolved_path(tmp_path: Path) -> None:
    """The caller must not have to resolve again - see the TOCTOU note below."""
    libdir = tmp_path / "usr" / "lib"
    libdir.mkdir(parents=True)
    real = tmp_path / "usr" / "lib" / "libz.so.1.2.13"
    real.write_bytes(b"\x7fELF")
    (libdir / "libz.so.1").symlink_to(real)

    resolved, refused = diagnostics._resolve_needed("libz.so.1", [str(libdir)], diagnostics._resolve_roots([tmp_path]))

    assert refused is False
    assert resolved == Path(os.path.realpath(real))


@pytest.mark.unit
def test_the_scan_never_resolves_a_dependency_a_second_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second ``realpath`` at the call site is unconfined and lands after the check.

    Reproduced before the fix: with the un-resolved candidate returned and
    re-resolved by ``_scan_native_tree``, a symlink swapped between the two
    steps put ``objdump`` on ``/etc/shadow``. The reader still opens by path
    afterwards, so a swap of a path COMPONENT can still redirect it; closing
    that needs an fd handed to the reader.

    Spied rather than read out of ``inspect.getsource``: a source-text
    assertion only sees a literal in one function body, so it cannot observe a
    re-resolution reached through a helper and would keep passing after a
    refactor moved one there.
    """
    seen: list[str] = []
    real = os.path.realpath

    def recording(path: object, *args: object, **kwargs: object) -> str:
        seen.append(str(path))
        return real(path, *args, **kwargs)  # type: ignore[arg-type]

    libdir = Path("/usr/lib")
    monkeypatch.setattr(os.path, "realpath", recording)
    resolved, refused = diagnostics._resolve_needed("libc.so.6", [str(libdir)], diagnostics._resolve_roots([libdir]))

    assert refused is False
    assert resolved is not None
    settled = str(resolved)
    assert seen.count(settled) <= 1, f"the settled dependency path was resolved again: {seen}"


@pytest.mark.unit
@requires_objdump
def test_a_swap_after_confinement_never_reaches_the_reader(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The reader must be handed the path confinement tested, not a later reading of it.

    The isolated test above pins ``_resolve_needed``; this one pins the CALLER,
    which is where the defect actually was. A second ``realpath`` in
    ``_scan_native_tree`` cannot be caught by counting resolutions, because
    without a swap it returns the same path - so simulate the swap: every
    resolution of the settled dependency after the first returns a poisoned
    path. If the caller re-resolves, that poisoned path is what it reads.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    work = _work_tree(cfg)
    _place(work, "zlib-native", _CLEAN)
    hostlib = tmp_path / "hostlib"
    hostlib.mkdir()
    shutil.copy2(_CLEAN, hostlib / "libz.so.1")
    poisoned = str(tmp_path / "poisoned")

    settled = os.path.realpath(hostlib / "libz.so.1")
    real = os.path.realpath
    resolutions: list[str] = []

    def swapping(path: object, *args: object, **kwargs: object) -> str:
        out = real(path, *args, **kwargs)  # type: ignore[arg-type]
        if out == settled:
            resolutions.append(out)
            if len(resolutions) > 1:
                return poisoned
        return out

    read: list[str] = []
    crafted = _crafted_reader(("libz.so.1",))

    def recording_reader(reader: str, path: Path) -> diagnostics._ElfInfo | None:
        read.append(str(path))
        return crafted(reader, path)

    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", (str(hostlib),))
    monkeypatch.setattr(diagnostics, "_read_elf", recording_reader)
    monkeypatch.setattr(os.path, "realpath", swapping)

    diagnostics.check_uninative_leak(cfg)

    assert settled in read, "the dependency was never read, so the test proves nothing"
    assert poisoned not in read, "the reader was handed a path resolved after the confinement check"


# --- what the provider index may and may not contain ------------------------


@pytest.mark.unit
@requires_objdump
def test_a_name_carried_by_no_elf_does_not_suppress_the_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The index holds what the walk READ, so a stamp of that name proves nothing.

    Measured against a name-only index over one real tree: of 133 suppressions,
    105 named nothing the walk ever read and 15 named no ELF at all - 68
    zero-byte ``libc++.so`` fixtures and a WebAssembly ``libdl.so`` stub among
    them. The claim in the suppression's own wording, that the walk reads the
    provider on its own, was false for those.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    work = _work_tree(cfg)
    _place(work, "readline-native", _CLEAN)
    decoy = work / "ncurses-native" / "1.0" / "image" / "usr" / "lib"
    decoy.mkdir(parents=True)
    (decoy / "libncurses.so.5").write_bytes(b"")
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", ())
    monkeypatch.setattr(diagnostics, "_read_elf", _crafted_reader(("libncurses.so.5",)))

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert "declares libncurses.so.5, which resolves to no file" in result.message


@pytest.mark.unit
@requires_objdump
def test_a_symlink_to_an_elf_in_the_tree_counts_as_provided(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A soname is usually spelled by the versioned link, not by the real file.

    ``libmicrohttpd.so.12`` -> ``libmicrohttpd.so.12.0.2`` is the shape, and the
    walk does read the target - so the name is covered even though the link
    itself is never opened.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    work = _work_tree(cfg)
    _place(work, "consumer-native", _CLEAN)
    libdir = work / "libmicrohttpd-native" / "1.0" / "image" / "usr" / "lib"
    libdir.mkdir(parents=True)
    shutil.copy2(_CLEAN, libdir / "libmicrohttpd.so.12.0.2")
    (libdir / "libmicrohttpd.so.12").symlink_to(libdir / "libmicrohttpd.so.12.0.2")
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", ())
    monkeypatch.setattr(diagnostics, "_read_elf", _crafted_reader(("libmicrohttpd.so.12",)))

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.PASS
    assert "provided elsewhere under the work tree" in result.message


@pytest.mark.unit
@requires_objdump
def test_a_symlink_out_of_the_tree_does_not_count_as_provided(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The walk does not read a target outside the work tree, so it covers nothing."""
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    work = _work_tree(cfg)
    _place(work, "consumer-native", _CLEAN)
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.copy2(_CLEAN, outside / "libbakar-elsewhere.so.1")
    libdir = work / "other-native" / "1.0" / "image" / "usr" / "lib"
    libdir.mkdir(parents=True)
    (libdir / "libbakar-elsewhere.so.1").symlink_to(outside / "libbakar-elsewhere.so.1")
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", ())
    monkeypatch.setattr(diagnostics, "_read_elf", _crafted_reader(("libbakar-elsewhere.so.1",)))

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.FAIL
    assert "declares libbakar-elsewhere.so.1, which resolves to no file" in result.message


@pytest.mark.unit
@requires_objdump
def test_a_provider_sorting_after_its_consumer_still_counts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Classification is deferred to after the walk, which is what buys this.

    Under a pre-pass the property came free; accumulating the index during the
    walk only preserves it because the unresolved entries are held back.
    ``aaa-native`` sorts first, so its consumer is read before the provider in
    ``zzz-native`` exists in the index at all.
    """
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    work = _work_tree(cfg)
    _place(work, "aaa-native", _CLEAN)
    later = work / "zzz-native" / "1.0" / "image" / "usr" / "lib"
    later.mkdir(parents=True)
    shutil.copy2(_CLEAN, later / "libbakar-late.so.1")
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", ())
    monkeypatch.setattr(diagnostics, "_read_elf", _crafted_reader(("libbakar-late.so.1",)))

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.PASS
    assert "provided elsewhere under the work tree" in result.message


@pytest.mark.unit
def test_the_scan_walks_the_work_tree_once() -> None:
    """The pre-pass cost a second full traversal - 2,329,148 entries on one tree."""
    source = inspect.getsource(diagnostics._scan_native_tree)

    assert source.count("os.walk(") == 1
    assert not hasattr(diagnostics, "_provided_file_names")


# --- a relative RUNPATH is a miss, not a refusal ----------------------------


@pytest.mark.unit
def test_a_relative_search_directory_is_skipped_not_refused(tmp_path: Path) -> None:
    """``../../sqlite3-native/usr/lib`` is CWD-relative, not an escape attempt.

    A relative candidate can never be ``is_relative_to`` an absolute root, so
    reporting it as refused labels a linker artefact as a security refusal AND
    routes it past ``_unchecked_reason`` entirely.
    """
    resolved, refused = diagnostics._resolve_needed(
        "libz.so.1", ["../../sqlite3-native/usr/lib"], diagnostics._resolve_roots([tmp_path])
    )

    assert resolved is None
    assert refused is False


@pytest.mark.unit
def test_a_path_qualified_soname_under_a_relative_runpath_is_still_judged_on_where_it_lands() -> None:
    """The absoluteness test is on the candidate, not on the search directory."""
    resolved, refused = diagnostics._resolve_needed(
        "/etc/shadow", ["../relative"], diagnostics._resolve_roots([Path("/usr/lib")])
    )

    assert resolved is None
    assert refused is True


@pytest.mark.unit
@requires_objdump
def test_a_relative_runpath_miss_still_consults_the_predicates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """It reaches the normal not-found path, so a foreign artifact is still excluded."""
    _patch_host(monkeypatch, tmp_path, max_glibc=_UNREACHABLE_CEILING)
    cfg = _cfg(tmp_path)
    fixture_dir = _work_tree(cfg) / "rust-native" / "1.0" / "sources" / "test" / "Inputs"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "hello-netbsd").write_bytes(_foreign_elf_header())
    monkeypatch.setattr(diagnostics, "_HOST_LIB_DIRS", ())
    monkeypatch.setattr(
        diagnostics,
        "_read_elf",
        lambda reader, path: diagnostics._ElfInfo(
            dynamic=True, nodes=frozenset({"2.2.5"}), needed=("libc.so.12",), runpaths=("../lib",)
        ),
    )

    result = diagnostics.check_uninative_leak(cfg)

    assert result.status is Status.PASS
    assert "out of scope" not in result.message
    assert "1 declared by a non-host-platform artifact" in result.message


# --- the sanctioned/permitted roots are canonicalised once -------------------


@pytest.mark.unit
def test_a_root_reached_through_a_symlink_is_permitted_either_way(tmp_path: Path) -> None:
    """Both spellings are roots, or a real hit falls through to the host libraries.

    The lexical stage compares text and runs first with ``continue``, so it can
    never be rescued later. Keeping only one spelling silently rebinds an edge
    from the uninative libc to the HOST libc and reports a false BLOCK with no
    out-of-scope line to explain it.
    """
    real = tmp_path / "real-sysroot" / "usr" / "lib"
    real.mkdir(parents=True)
    (real / "libz.so.1").write_bytes(b"\x7fELF")
    link = tmp_path / "linked-sysroot"
    link.symlink_to(tmp_path / "real-sysroot")

    roots = diagnostics._resolve_roots([link])

    assert diagnostics._lexically_within(link / "usr" / "lib" / "libz.so.1", roots)
    assert diagnostics._lexically_within(real / "libz.so.1", roots)


@pytest.mark.unit
def test_resolve_roots_does_not_resolve_per_candidate() -> None:
    """The helpers take pre-resolved roots; resolving inside them was ~1.4M calls."""
    assert "realpath(root)" not in inspect.getsource(diagnostics._within_any)
    assert "realpath" not in inspect.getsource(diagnostics._lexically_within)


# --- _host_platform_elf must not block, and must not invert its polarity -----


@pytest.mark.unit
@pytest.mark.timeout(10)
def test_host_platform_elf_refuses_to_open_a_fifo(tmp_path: Path) -> None:
    """Opening a FIFO for reading blocks until a writer appears, with no timeout.

    ``_is_elf`` guards its own open, but a whole ``_read_elf`` subprocess runs
    between the two - a window roughly five hundred times wider than the one
    ``_is_elf`` closes.
    """
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    assert diagnostics._host_platform_elf(fifo) is True


@pytest.mark.unit
def test_host_platform_elf_fails_open_on_a_non_regular_file(tmp_path: Path) -> None:
    """The polarity is the opposite of ``_is_elf``'s, and getting it wrong goes quiet.

    The caller reads ``if not _host_platform_elf(...)`` to SUPPRESS a warning,
    so copying ``_is_elf``'s ``return False`` for a non-regular file would drop
    dependencies from the report instead of reporting them.
    """
    assert diagnostics._host_platform_elf(tmp_path) is True
    assert diagnostics._is_elf(tmp_path) is False
