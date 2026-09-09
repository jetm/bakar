"""Host-glibc leak scanning of post-build native artifacts.

The ELF reader, the path-confinement machinery and the report builder behind
``check_uninative_leak``. The check itself stays in :mod:`bakar.diagnostics`
because it belongs to the check framework; everything it walks lives here.

Three invariants are recorded from the archived ``leak-scan-path-confinement``
change. Each is voided silently - no test fails, the scan simply stops being a
scan - so do not relax one without re-reading that change:

1. :func:`_resolve_roots` is the only sanctioned producer of the ``permitted``
   allowlist :func:`_resolve_needed` consumes, and the two must stay in one
   module. Split them and a third caller can supply an allowlist nothing
   canonicalised.
2. :func:`_resolve_needed` tests containment with :func:`_lexically_within`
   BEFORE it calls ``os.path.realpath`` and again afterwards. Resolving first
   would stat intermediate components of a path that is about to be refused.
   Do not reorder, and do not collapse its tri-state ``(path, refused)``
   return.
3. :func:`_neutralized` runs at the message boundary only - in
   :meth:`_NativeLeak.describe` and :func:`_scan_native_tree`, never inside
   :func:`_read_elf`. Neutralizing earlier corrupts the dependency-cache key,
   the containment tests and the node comparisons; it is the explicitly
   rejected alternative. :func:`_leak_report` does NOT call it: by the time a
   string reaches that joiner its caller has already neutralized it.

   One boundary site sits OUTSIDE this module, in ``check_uninative_leak``'s
   ``fix_hint`` (``diagnostics.py``, the ``" ".join(_neutralized(recipe) ...)``).
   It is guarded - dropping it fails
   ``test_recipe_name_reaches_message_and_fix_hint_neutralized`` - but this
   docstring cannot reach it, so an audit of invariant 3 has to look there too.
   The hint renders through a markup-enabled ``console.print``, so a recipe
   directory named ``foo[/]bar`` raises ``MarkupError`` and takes the whole
   doctor report with it.

:func:`_read_elf` and ``_HOST_LIB_DIRS`` are deliberately NOT re-exported from
:mod:`bakar.diagnostics`. A leak-scan test whose stub is left pointing at the
old path must raise, because the alternative is the real reader walking an
empty temporary tree and satisfying its own "no leak" assertion.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass

# Bound as a name rather than importing the module, matching the spelling this
# code carried in ``bakar.diagnostics``.
from glob import iglob
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape

if TYPE_CHECKING:
    from collections.abc import Iterable


def _version_tuple(value: str) -> tuple[int, ...] | None:
    """Split a dotted version into integer components, or None when non-numeric.

    Integer-tuple comparison rather than string comparison because string order
    ranks ``2.9`` above ``2.44``, which is the exact comparison the glibc
    invariant makes. Deliberately no ``packaging`` dependency for a single
    two-component comparison, matching the Docker version check's precedent.
    """
    parts = value.strip().split(".")
    if not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


# Requirements come from ``objdump -p``'s ``Version References:`` block, which
# renders ``DT_VERNEED`` and therefore holds requirements and nothing else.
#
# The tempting shortcut is ``objdump -T``'s parenthesisation, and it is wrong.
# ``objdump`` parenthesises a version node when the symbol's version binding is
# non-default (``VERSYM_HIDDEN``), which binutils sets for undefined symbols AND
# for compat definitions - so a definition at a real ``.text`` address prints
# parenthesised too. Measured on ``libc.so.6``: 537 parenthesised symbols are
# not ``*UND*``, and the parenthesised maximum runs several releases above what
# ``DT_VERNEED`` says libc actually needs. Anything derived from the parenthesis
# is an upper bound on requirements, not a measure of them.
#
# Section is no discriminator either: a copy-relocated libc data object lives in
# the executable's own ``.bss`` rather than ``*UND*`` and is still a requirement
# (``/usr/bin/ls`` carries eight, ``optarg`` and friends). ``DT_VERNEED`` names
# all eight, which is the point of reading it instead.
_VERNEED_HEADER = "Version References:"
_DYNAMIC_HEADER = "Dynamic Section:"
# Every string this module matches in objdump's output - both block headers
# above and the "not a dynamic object" stderr - is a gettext msgid in bfd, and
# bfd ships translations (fr, es, da, fi and a dozen more under
# /usr/share/locale/*/LC_MESSAGES/bfd.mo). Unpinned, a French desktop reads
# zero version nodes out of every artifact and this BLOCK-severity gate returns
# an all-clear over a tree it never understood.
#
# LANGUAGE is pinned as well as LC_ALL because gettext consults it first, and
# the documented condition for ignoring it is a locale of exactly "C" or
# "POSIX" - which C.UTF-8 is not. glibc 2.42 does ignore LANGUAGE under
# LC_ALL=C.UTF-8 (measured against binutils 2.46 for LANGUAGE=fr, fr_FR and
# fr_FR:fr), so this entry is redundant there; it is kept because that
# behaviour is an implementation detail of one libc and the documented rule
# does not promise it. Blanking is the portable disarm: gettext treats an empty
# LANGUAGE as unset. C.UTF-8 rather than C to match steps/qcom_common.py's
# existing pin and keep a UTF-8-capable child.
_READER_ENV: dict[str, str] = {"LC_ALL": "C.UTF-8", "LANGUAGE": ""}
# ``GLIBC_PRIVATE`` carries no version digits and so is excluded by construction:
# an unversioned node cannot be compared against a dotted ceiling.
_GLIBC_NODE_RE = re.compile(r"\bGLIBC_(\d+(?:\.\d+)+)\b")
_ELF_NEEDED_RE = re.compile(r"^\s*NEEDED\s+(\S+)\s*$", re.MULTILINE)
_ELF_RUNPATH_RE = re.compile(r"^\s*(?:RUNPATH|RPATH)\s+(\S+)\s*$", re.MULTILINE)


def _required_glibc_nodes(dump: str) -> frozenset[str]:
    """Glibc version nodes named in ``dump``'s ``Version References:`` block.

    The block runs to the first line that is non-empty and not indented. Parsed
    on that indentation rather than on a column layout, because the entry lines
    carry a hash, flags and an index whose widths ``objdump`` is free to change.
    """
    block: list[str] = []
    inside = False
    for line in dump.splitlines():
        if line.startswith(_VERNEED_HEADER):
            inside = True
            continue
        if inside:
            if line and not line.startswith((" ", "\t")):
                break
            block.append(line)
    return frozenset(_GLIBC_NODE_RE.findall("\n".join(block)))


# Bound on `include` recursion in ld.so.conf. The format allows an include to
# pull in a glob that includes further files; a cycle would otherwise hang the
# doctor on a malformed host config.
_LD_CONF_MAX_DEPTH = 4
_LD_SO_CONF = Path("/etc/ld.so.conf")


def _ld_so_conf_dirs(conf: Path = _LD_SO_CONF, *, depth: int = 0, seen: set[Path] | None = None) -> list[str]:
    """Library directories this host's own dynamic loader searches.

    Read rather than guessed. A fixed tuple is simply wrong on a multiarch
    distribution: Debian and Ubuntu put libc in ``/usr/lib/<gnu-triplet>``, and
    no hardcoded list can name that directory for every architecture. Asking the
    loader's own configuration answers the question the scan is actually posing
    - "where would THIS host resolve this DT_NEEDED" - and keeps answering it
    when a distribution moves its libraries.

    Deliberately not derived from ``sysconfig``'s ``MULTIARCH``: that reflects
    how the running Python was built, and a relocatable interpreter (uv's
    python-build-standalone, which is what CI runs) does not set it on a host
    that is nonetheless multiarch.

    Never raises. An absent or unreadable config yields nothing and leaves the
    static floor below in place, which is the right answer on a host that has no
    glibc loader config to begin with.
    """
    if depth > _LD_CONF_MAX_DEPTH:
        return []
    seen = set() if seen is None else seen
    marker = conf.resolve(strict=False)
    if marker in seen:
        return []
    seen.add(marker)
    try:
        raw = conf.read_text(errors="replace")
    except OSError:
        return []
    dirs: list[str] = []
    for line in raw.splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        head, _, rest = entry.partition(" ")
        if head == "include":
            pattern = rest.strip()
            if not pattern:
                continue
            # A relative include is relative to the including file, not to the
            # doctor's working directory.
            if not pattern.startswith("/"):
                pattern = str(conf.parent / pattern)
            for included in sorted(iglob(pattern)):
                dirs += _ld_so_conf_dirs(Path(included), depth=depth + 1, seen=seen)
            continue
        dirs.append(entry)
    return dirs


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    """Order-preserving dedup, so the search order stays the declared one."""
    return tuple(dict.fromkeys(values))


# Where a DT_NEEDED soname is looked for when no RUNPATH/RPATH names it. Not a
# full loader emulation: enough to tell "resolves to a host library" from
# "resolves to nothing", which is the only distinction the scan makes.
#
# The static entries are a floor for hosts with no loader config; the rest comes
# from the host's ld.so.conf. Without the latter, every artifact on a Debian or
# Ubuntu host reports its libc as unresolved, because libc.so.6 lives in
# /usr/lib/<triplet> and nothing here would name it.
#
# Also the allowlist of permitted roots for dependency resolution (see
# _resolve_needed): widening it from root-owned loader config does not reopen
# the file-existence oracle that confinement closed, since an attacker who can
# write /etc/ld.so.conf.d has already won.
_HOST_LIB_DIRS: tuple[str, ...] = _unique(
    ("/usr/lib", "/usr/lib64", "/lib", "/lib64", "/usr/local/lib", *_ld_so_conf_dirs())
)

# Findings are enumerated in the message; past this many the tail is summarized
# so a systemically broken tree reports a readable verdict instead of megabytes.
_LEAK_REPORT_LIMIT = 10

# Everything stripped out of artifact-derived text, in one pass:
#
# * C0 (including ESC), DEL and C1. A soname is matched with ``\S+``, which
#   admits ESC, so an artifact can carry a full OSC sequence through the reader
#   intact.
# * The surrogate block. ``os.walk`` and ``Path`` decode an undecodable
#   filesystem byte with ``surrogateescape``, yielding a lone U+DC80-U+DCFF that
#   no UTF-8 encoder will accept - and the doctor gate writes the report to
#   ``diagnosis.txt`` BEFORE rendering it, so one such name raises
#   ``UnicodeEncodeError`` there and takes the whole gate, and the build, with
#   it rather than garbling a row.
# * The Unicode ``Cf`` format characters: the bidi overrides (U+202A-U+202E,
#   U+2066-U+2069) let a crafted name render as a different path, and the
#   zero-width ones hide a difference entirely. ``re`` has no category escape,
#   so the ranges are spelled out; a test walks the whole code space against
#   ``unicodedata`` so a future assignment fails here instead of leaking.
# * U+2028 and U+2029, which Rich renders as a line break - one crafted name
#   splits a table cell into two rendered rows, which is how a forged finding
#   gets its own line.
# * The report's own entry separator (see ``_ENTRY_SEPARATOR``), so that a
#   boundary between findings can only be produced by ``_leak_report``.
_CONTROL_RE = re.compile(
    "["
    "\x00-\x1f\x7f-\x9f"
    "\ud800-\udfff"
    "\u00ad\u0600-\u0605\u061c\u06dd\u070f\u0890-\u0891\u08e2\u180e"
    "\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb"
    "\U000110bd\U000110cd\U00013430-\U0001343f\U0001bca0-\U0001bca3\U0001d173-\U0001d17a"
    "\U000e0001\U000e0020-\U000e007f"
    "\u2028\u2029"
    "\u2022"
    "]"
)

# Bounds one artifact-derived string so a crafted name cannot flood a report the
# operator has to read. Measured on a real native work tree: 3,520 of its ELF
# artifacts exceed 240 characters and the longest path is 445, because
# ``sysroot-destdir/`` embeds a second absolute copy of the work path and so
# roughly doubles it. The bound sits well clear of that, and when it does fire
# the ELISION takes the MIDDLE - a path truncated at its tail names a directory
# rather than a file and matches nothing the operator can look up.
_ARTIFACT_TEXT_LIMIT = 1024
_ELISION = "..."

# What separates one finding from the next in a report. ``_neutralized`` strips
# this character from every artifact-derived string, so an entry boundary can
# only be produced by ``_leak_report`` - a directory named
# ``a) reaches GLIBC_2.99 via the artifact itself; `` otherwise renders one
# leaked artifact as two findings, the second naming a library that was never
# read.
_ENTRY_SEPARATOR = " • "


def _neutralized(value: object) -> str:
    """Render an artifact-derived value safe to put in a ``CheckResult`` message.

    Everything the leak scan reports - the artifact path, the recipe name, the
    declared soname, the path a dependency resolved to - is read out of a file in
    the work tree, and ``_print_diagnosis`` hands the message to a markup-enabled
    Rich table. So a directory named ``foo[/]bar`` raises ``MarkupError`` and
    destroys the whole doctor report rather than one row, ``[on red blink]``
    forges report formatting, an ESC in a soname rewrites the operator's terminal
    title, and an undecodable byte in a filename kills the gate at the point it
    writes the report to disk. Three defences, in this order: strip the
    characters ``_CONTROL_RE`` names, bound the length, then escape markup -
    escaping last so the backslashes it inserts are neither stripped nor counted
    against the bound.

    Applied where a message is BUILT, never inside ``_read_elf``: containment
    tests, the ``dep_cache`` key and node comparisons all have to keep comparing
    the bytes the artifact actually declared.
    """
    text = _CONTROL_RE.sub("", str(value))
    if len(text) > _ARTIFACT_TEXT_LIMIT:
        keep = _ARTIFACT_TEXT_LIMIT - len(_ELISION)
        head = keep // 2
        text = text[:head] + _ELISION + text[len(text) - (keep - head) :]
    return escape(text)


@dataclass(frozen=True)
class _NativeLeak:
    """One version node above the ceiling, and where it was reached from."""

    artifact: Path
    recipe: str
    node: str
    # "the artifact itself", or "dependency <path>" - the operator's first
    # question is whether the recipe emitted this or merely linked it. Already
    # neutralized component-wise where it is built, so describe() must not
    # neutralize it again and double-escape.
    source: str

    def describe(self) -> str:
        return (
            f"{_neutralized(self.artifact)} (recipe {_neutralized(self.recipe)}) "
            f"reaches GLIBC_{_neutralized(self.node)} via {self.source}"
        )


@dataclass(frozen=True)
class _ElfInfo:
    # False when the dump carried no dynamic section: a relocatable .o or a
    # static binary. Such a file states no requirement at all, so it is not
    # evidence about the tree either way - see _scan_native_tree.
    dynamic: bool
    nodes: frozenset[str]
    needed: tuple[str, ...]
    runpaths: tuple[str, ...]


def _elf_reader() -> str | None:
    """Resolve the ELF reader the scan needs, or None when none is installed.

    Its own function so the check can report a SKIP naming the missing tool
    rather than PASSing a tree it never read.
    """
    return shutil.which("objdump")


def _is_elf(path: Path) -> bool:
    """True when ``path`` starts with the ELF magic.

    Reading four bytes beats shelling out to ``file`` per path: a native work
    tree holds thousands of scripts, headers and stamps, and only the ELF ones
    are worth an ``objdump`` process.

    The regular-file gate is not an optimisation. ``os.walk`` lists a FIFO
    among its files, opening one for reading blocks until a writer appears,
    and this open carries no timeout - a named pipe left under a recipe's work
    directory, or restored from an sstate tarball, would hang the whole check.
    ``S_ISREG`` on an ``lstat`` excludes FIFOs, sockets and device nodes in one
    condition, without a signal or a thread.
    """
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return False
        with path.open("rb") as handle:
            return handle.read(4) == b"\x7fELF"
    except OSError:
        return False


def _read_elf(reader: str, path: Path) -> _ElfInfo | None:
    """Read ``path``'s required glibc version nodes, DT_NEEDED entries and RUNPATH.

    All three come out of ``-p`` alone: ``DT_VERNEED``, ``DT_NEEDED`` and
    ``DT_RUNPATH`` are all in the dynamic section. ``-T`` used to be passed for
    the version nodes and is not, now that requirements are read from
    ``Version References:`` - dropping it also drops the dynamic symbol table,
    which for ``libc.so.6`` is 240 KB of stdout per invocation against 6 KB for
    ``-p``. None when the reader failed, which the caller treats as unread
    rather than clean.

    The reader runs under a pinned locale (``_READER_ENV``) because every string
    matched below is a translated bfd message.
    """
    try:
        out = subprocess.run(
            # "--" so the guarantee is local: every path reaching here is
            # absolute today, but that invariant is established two call sites
            # away and a name beginning with "-" would otherwise read as a flag.
            [reader, "-p", "--", str(path)],
            capture_output=True,
            text=True,
            # Every string parsed out of this dump is an English bfd msgid; see
            # _READER_ENV. text=True decodes with the PARENT interpreter's
            # encoding, so overriding the child's locale does not touch the
            # errors="replace" contract below.
            env={**os.environ, **_READER_ENV},
            # An artifact's .dynstr can hold bytes that are not valid UTF-8, and
            # a decode failure here raises UnicodeDecodeError - a ValueError,
            # which the except below does not catch - so one odd vendor blob
            # would crash the whole check instead of costing one unread file.
            # Every pattern applied to the output is byte-agnostic, so replacing
            # the undecodable bytes loses nothing.
            errors="replace",
            timeout=30,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:
        # A relocatable object or a static binary has no dynamic section. Under
        # ``-p`` alone this binutils exits 0 with an empty dump, which already
        # yields the non-dynamic _ElfInfo below; the branch stays for a reader
        # that still errors, because without it a work tree's thousands of .o
        # files under <pn>/<pv>/build/ would all count as unread and return an
        # unconditional WARN on every healthy tree. The stderr string is a bfd
        # msgid and so is translated on a localised host, which _READER_ENV
        # above pins away along with the block headers.
        if "not a dynamic object" in out.stderr:
            return _ElfInfo(dynamic=False, nodes=frozenset(), needed=(), runpaths=())
        return None
    return _ElfInfo(
        dynamic=_DYNAMIC_HEADER in out.stdout,
        nodes=_required_glibc_nodes(out.stdout),
        needed=tuple(dict.fromkeys(_ELF_NEEDED_RE.findall(out.stdout))),
        runpaths=tuple(_ELF_RUNPATH_RE.findall(out.stdout)),
    )


def _runpath_dirs(info: _ElfInfo, artifact: Path) -> list[str]:
    """Expand an artifact's RUNPATH/RPATH into candidate directories.

    ``$ORIGIN`` is expanded because uninative-relocated native binaries carry
    origin-relative RPATHs into their recipe sysroot; leaving it literal would
    make every such dependency look unresolvable.
    """
    dirs: list[str] = []
    for entry in info.runpaths:
        for part in entry.split(":"):
            if not part:
                continue
            dirs.append(part.replace("$ORIGIN", str(artifact.parent)).replace("${ORIGIN}", str(artifact.parent)))
    return dirs


def _normalized(path: Path) -> Path:
    """``path`` with ``.``/``..`` folded away, without touching the filesystem.

    ``os.path.normpath`` preserves exactly two leading slashes (POSIX leaves
    ``//foo`` implementation-defined), so ``//usr/lib/libz.so.1`` would compare
    unequal to the ``/usr/lib`` root and a legitimate host library would be
    refused. Collapse the doubled root before comparing.
    """
    text = os.path.normpath(str(path))
    while text.startswith("//"):
        text = text[1:]
    return Path(text)


def _lexically_within(path: Path, roots: tuple[Path, ...]) -> bool:
    """True when ``path`` names a location under one of ``roots``, on text alone.

    No filesystem access at all: this is what decides whether a candidate is
    ever stat'd, so a refused candidate must be indistinguishable from a name
    the scan looked for and did not find. Containment is per path component -
    a string prefix test would admit ``/usr/libexec/...`` against ``/usr/lib``.

    ``roots`` must already be normalized and symlink-resolved. Resolving them
    here would be both a per-candidate cost on constants and, worse, a lie: this
    stage runs before any filesystem access by design, so a root spelled through
    a symlink has to have been canonicalised by the caller or a legitimate
    in-root path is refused and silently falls through to the host libraries.
    ``_resolve_roots`` is what does it, once, before the walk.
    """
    normalized = _normalized(path)
    return any(normalized.is_relative_to(root) for root in roots)


def _resolve_roots(roots: Iterable[Path]) -> tuple[Path, ...]:
    """Canonicalise containment roots once, for ``_lexically_within``/``_within_any``.

    Every root the scan uses is spelled by whoever configured it - ``work`` comes
    from ``abspath`` and the sanctioned trees from ``OECORE_NATIVE_SYSROOT`` or a
    release directory - so none of them is guaranteed symlink-free. Resolving
    them per candidate was roughly 1.4M redundant ``realpath`` calls on
    constants over one real tree; resolving them here is once per scan.

    BOTH spellings are kept, the one given and the resolved one, because the
    lexical stage compares text and a candidate may legitimately arrive in
    either. Dropping the given spelling refuses a real hit under
    ``/usr/lib64`` on a host where that is a link to ``/usr/lib``, and the
    candidate then falls through to whatever the next search directory holds -
    silently rebinding the edge to the host libc and reporting a false leak with
    no out-of-scope line to explain it. Dropping the resolved spelling is the
    mirror failure for a sanctioned tree reached through a link. Admitting both
    is not a widening: a root is trusted by construction, and the second stage
    still re-tests the RESOLVED candidate against this same set.
    """
    both: list[Path] = []
    for root in roots:
        for spelling in (_normalized(root), _normalized(Path(os.path.realpath(root)))):
            if spelling not in both:
                both.append(spelling)
    return tuple(both)


def _resolve_needed(soname: str, search_dirs: list[str], permitted: tuple[Path, ...]) -> tuple[Path | None, bool]:
    """Resolve ``soname`` under ``search_dirs``, confined to ``permitted``.

    ``permitted`` must come from ``_resolve_roots``. Returns ``(path, refused)``,
    a tri-state: the resolved path when the soname resolved, ``(None, False)``
    when every candidate was looked for and not found, and ``(None, True)`` when
    a candidate was refused for landing outside the permitted roots and nothing
    else resolved. The caller reports the last case as out of scope, which is a
    different fact from a missing library.

    Both operands of the join come from the artifact's own ``.dynstr`` - the
    soname, and the run paths ``_runpath_dirs`` expands - so an unconfined join
    lets a file in the work tree steer the scan onto any readable path and get
    that path echoed into the operator's report. The guard is an allowlist on
    where the join LANDED rather than a refusal of either input: a
    path-qualified ``DT_NEEDED`` is legal ELF that GNU ld emits for a library
    linked by absolute path with no ``DT_SONAME``, and refusing it outright
    demotes a genuine leak to a warning.

    A refused candidate directory only skips that candidate; the loop continues,
    because a real artifact carries a foreign RPATH ahead of the host directories
    that resolve it fine.

    The path returned is the RESOLVED one, and it is the value containment was
    tested on. Returning the pre-``realpath`` spelling for the caller to resolve
    again reopens the hole this closes: the second resolution is unconfined, and
    it happens after the check, so swapping a symlink in between redirects the
    reader onto an arbitrary path - measured, ``objdump`` ran on ``/etc/shadow``.
    Resolving once is not a complete answer either, because ``objdump`` opens by
    path afterwards and a swap of a path COMPONENT after the check can still
    redirect it; closing that needs an fd handed to the reader and is out of
    proportion to a check that reads a build's own work tree.
    """
    refused = False
    for directory in search_dirs:
        candidate = Path(directory) / soname
        # A CWD-relative search directory can never be inside an absolute root,
        # so it is skipped rather than refused: "the linker recorded a relative
        # RPATH" is not the same fact as "this landed outside the scanned
        # roots", and reporting it as the latter bypasses _unchecked_reason.
        # Tested on the candidate rather than on the directory, so a
        # path-qualified soname under a relative RPATH is still judged on where
        # it actually lands.
        if not candidate.is_absolute():
            continue
        # Lexically first, and the order is load-bearing: resolving symlinks
        # first would stat the intermediate components of an attacker-named
        # path, a weaker oracle but still one.
        if not _lexically_within(candidate, permitted):
            refused = True
            continue
        # Then again with symlinks resolved, which catches a link inside a
        # permitted root pointing out of one.
        final = Path(os.path.realpath(candidate))
        if not _lexically_within(final, permitted):
            refused = True
            continue
        if final.is_file():
            return final, False
    return None, refused


def _within_any(path: Path, roots: tuple[Path, ...]) -> bool:
    """True when ``path`` lies under one of ``roots``, symlinks resolved.

    ``roots`` must come from ``_resolve_roots``; see ``_lexically_within``.
    """
    resolved = _normalized(Path(os.path.realpath(path)))
    return any(resolved.is_relative_to(root) for root in roots)


def _nodes_above(nodes: frozenset[str], ceiling: tuple[int, ...]) -> list[str]:
    """The version nodes in ``nodes`` that exceed ``ceiling``."""
    above: list[str] = []
    for node in nodes:
        parsed = _version_tuple(node)
        if parsed is not None and parsed > ceiling:
            above.append(node)
    return sorted(above, key=lambda n: _version_tuple(n) or ())


def _producing_recipe(work: Path, artifact: Path) -> str:
    """Name the recipe that built ``artifact`` from its work-tree path.

    ``<work>/x86_64-linux/<pn>/<pv>/...`` (``bitbake.conf``'s ``WORKDIR``), so
    the first component under the native work tree is the recipe name - which is
    what the cache-discard remediation has to name.
    """
    try:
        relative = artifact.relative_to(work)
    except ValueError:
        return "unknown"
    return relative.parts[0] if relative.parts else "unknown"


# The native work tree is ``<tmpdir>/work/x86_64-linux`` by construction (see
# check_uninative_leak), so anything this build produced there is an x86-64 ELF
# under the SysV or Linux ABI. EM_X86_64 and ELFOSABI_NONE/ELFOSABI_LINUX out of
# the ELF header, read directly rather than through objdump because the whole
# question is one field and the dump is already parsed for other reasons.
_HOST_ELF_MACHINE = 62
_HOST_ELF_OSABI = frozenset({0, 3})

# Why a declared dependency that resolved to nothing is not reported as
# unchecked. Stable keys: they are counted per scan and named in the verdict.
_UNCHECKED_PROVIDED = "provided elsewhere under the work tree"
_UNCHECKED_FOREIGN = "declared by a non-host-platform artifact"
_UNCHECKED_NO_GLIBC = "declared by an artifact stating no glibc requirement"


def _host_platform_elf(path: Path) -> bool:
    """True when ``path``'s ELF header names the platform this build runs on.

    Fails open - an unreadable, truncated or non-regular file returns True -
    because this only ever decides whether to stay silent about a dependency,
    and silence must never be the default when the evidence is missing. Note the
    polarity: the ``S_ISREG`` gate below returns the OPPOSITE of the one in
    ``_is_elf``. There it excludes a FIFO from being read at all; here it means
    "no evidence", and the caller reads ``if not _host_platform_elf(...)`` to
    suppress a warning, so returning False for a pipe would silently drop
    dependencies instead of reporting them.

    Its own type gate rather than leaning on ``_is_elf``'s: the two are separated
    by a whole ``_read_elf`` subprocess, so the window in which a regular file
    could be replaced by a pipe is roughly five hundred times wider than the one
    ``_is_elf`` closes, and the open below blocks until a writer appears.
    """
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return True
        with path.open("rb") as handle:
            head = handle.read(20)
    except OSError:
        return True
    if len(head) < 20:
        return True
    machine = int.from_bytes(head[18:20], "little" if head[5] == 1 else "big")
    return head[7] in _HOST_ELF_OSABI and machine == _HOST_ELF_MACHINE


@dataclass(frozen=True)
class _Unclassified:
    """An unresolved dependency held back until the walk has finished.

    ``_unchecked_reason``'s first predicate asks what the walk read elsewhere
    under the work tree, and a provider that sorts after its consumer is only
    known once the walk is over. Deferring the classification is what buys that,
    and it costs nothing: these are held in ``unresolved``'s own list, in walk
    order, and turned into their message in place.
    """

    artifact: Path
    recipe: str
    soname: str
    info: _ElfInfo


def _unchecked_reason(
    artifact: Path,
    soname: str,
    info: _ElfInfo,
    provided: frozenset[str],
) -> str | None:
    """Why a dependency that resolved to no file need not alarm the operator.

    None means it must, and the caller reports it. This narrows the WARN
    trigger; it never lowers the severity, and it never applies to a dependency
    the confinement refused - an out-of-scope entry is the only operator-visible
    output that confinement has, and the `shadow-native` shape would be
    swallowed by two of the predicates below if they were ever allowed near it:
    it is staged under `sysroot-destdir/`, and both sonames its foreign RUNPATH
    names exist inside its own work directory.

    Every predicate is justified by a counted class from a sweep of a real
    236-recipe tree (`build-qemuarm64`, 118,549 DT_NEEDED entries, 118,361
    resolved, 188 not). Attributed to the first predicate that matches:

    * ``_UNCHECKED_PROVIDED`` - 118. The walk itself read an ELF of that name
      somewhere else under the work tree; the RPATH just names a staging
      location it is not at (`image/`, `sysroot-destdir/`, `.libs/`, a cleaned
      recipe sysroot). Nothing is lost by staying quiet: that provider's own
      nodes were compared against the ceiling on its own turn, so the edge is
      covered - just not through this join.
    * ``_UNCHECKED_FOREIGN`` - 51. The artifact is not an x86-64 Linux ELF
      (NetBSD/FreeBSD/Solaris libc, Android liblog and friends, all fixtures
      inside an upstream source tarball). It cannot load under the uninative
      loader on any host, so what it declares says nothing about the ceiling.
    * ``_UNCHECKED_NO_GLIBC`` - 14. The artifact states no glibc version
      requirement of its own, so it is not the product of a native compile
      through the buildtools toolchain - every such compile emits at least one
      glibc node - and an edge it fails to resolve is not evidence about output
      this ceiling governs. NOTE what this does NOT say: it does not follow from
      the artifact's own empty node set that the dependency has none. That
      inference is the exact non-implication `docs/doctor.md` cites as the
      reason the DT_NEEDED walk cannot be deleted, and a counterexample sits in
      this very tree - an lldb minidump fixture with no nodes of its own
      declaring `libstdc++.so.6`, which on this host carries nodes up to 2.38.
      The predicate is a judgement about which artifacts the gate is about, not
      a deduction about their dependencies.

    Five of the 188 keep raising WARN, and should. Three reach this function and
    are reported as resolving to no file: `libselinux.so.1` and `libcallback1.so`
    are carried by neither the host nor the build, which is exactly the "looked
    for it, found nothing" fact the branch exists to state. Two never reach it -
    cmake-native's big-endian `RunCMake/file-RPATH` fixtures, whose `/sample/rpath`
    RUNPATH the confinement refuses, so they are reported out of scope instead.
    They are inside the 188 (before confinement they resolved to no file, like
    the rest), just attributed elsewhere: a classification that does not model
    the refusal will score them under ``_UNCHECKED_FOREIGN`` and read 53 there
    and no separate term, which sums to the same total.

    118 + 51 + 14 + 3 + 2 = 188.

    Distinct and NOT in that sum: the foreign-RPATH edges the confinement gives
    up. Those resolve today, so confinement adds them as new out-of-scope
    reports rather than reclassifying an existing miss. On this host they number
    ZERO - `shadow-native`'s `libsubid.so.5` does carry an absolute RUNPATH into
    a foreign build directory, but refusal is per candidate directory, so
    `libattr.so.1` and `libbsd.so.0` resolve under `/usr/lib` on the next
    candidate and never become out-of-scope reports at all. Expect that count to
    move with what the host has installed.

    A later reader can re-run the classification against a fresh tree; a large
    shift in the residue is drift or a new defect rather than noise.

    Deliberately NOT a predicate on where the artifact sits in the work tree:
    excluding `image/`, `build/` and `sysroot-destdir/` covers all 188 and looks
    justified, but essentially every artifact in a native work tree lives under
    one of those, so it would silence the channel permanently.
    """
    if os.path.basename(soname) in provided:
        return _UNCHECKED_PROVIDED
    if not _host_platform_elf(artifact):
        return _UNCHECKED_FOREIGN
    if not info.nodes:
        return _UNCHECKED_NO_GLIBC
    return None


def _unchecked_note(excluded: dict[str, int]) -> str:
    """Name what the scan chose not to report, so the narrowing stays visible."""
    if not excluded:
        return ""
    parts = ", ".join(f"{count} {reason}" for reason, count in sorted(excluded.items()))
    total = sum(excluded.values())
    return f"; {total} further declared dependenc(y/ies) resolved to no file and are not counted as unchecked: {parts}"


def _scan_native_tree(
    work: Path,
    reader: str,
    ceiling: tuple[int, ...],
    sanctioned: tuple[Path, ...],
) -> tuple[list[_NativeLeak], list[str], int, dict[str, int]]:
    """Walk ``work`` for ELF artifacts leaking a node above ``ceiling``.

    Returns ``(leaks, unresolved, scanned, excluded)``, where ``scanned`` counts
    only the artifacts that had a dynamic section to read and ``excluded`` counts,
    per reason, the dependencies that resolved to nothing but that
    ``_unchecked_reason`` accounts for. Each artifact contributes its own
    version nodes AND the nodes of every DT_NEEDED dependency that resolves
    outside ``sanctioned`` - a host library built against the host glibc carries
    the fault one edge away while the artifact's own nodes look clean, which is
    the whole reason this is not a one-line symbol grep.

    Dependency results are cached by resolved path so a libc referenced by five
    hundred artifacts is read once.

    The provider index ``_unchecked_reason`` consults is accumulated by this one
    walk rather than by a pre-pass, and holds only names the walk actually READ:
    a non-symlink ELF regular file, or a symlink to one inside the tree. A
    pre-pass over every name under ``work`` was both a second full traversal
    (2,329,148 entries, 1.3 seconds, 43 MB) and a false claim - of its 133
    suppressions, 105 named nothing the walk ever read and 15 named no ELF at
    all, among them 68 zero-byte `libc++.so` fixtures. Names only, never paths:
    the membership test is keyed on an artifact-controlled soname, and a set
    lookup joins nothing onto a directory and stats nothing, so it cannot reach
    a path the confinement in ``_resolve_needed`` refuses. A "search the tree
    for this soname" helper would be exactly that second unbounded join.
    """
    leaks: list[_NativeLeak] = []
    # Holds finished message lines and, in walk order among them, the
    # dependencies whose classification needs the finished provider index.
    pending: list[str | _Unclassified] = []
    excluded: dict[str, int] = {}
    dep_cache: dict[Path, frozenset[str] | None] = {}
    scanned = 0
    provided: set[str] = set()
    # The only places a declared dependency may resolve to. Fixed before the
    # walk and never derived from an artifact: a root read out of an artifact's
    # own RUNPATH, or matched by work-tree path shape, would be a root any local
    # user can satisfy, which is the oracle this closes. Symlinks resolved here,
    # once, because the lexical stage of the confinement cannot do it later.
    work_root = _resolve_roots([work])
    sanctioned = _resolve_roots(sanctioned)
    permitted: tuple[Path, ...] = _resolve_roots(Path(d) for d in _HOST_LIB_DIRS) + sanctioned + work_root
    for root, dirs, files in os.walk(work, followlinks=False):
        # Sorted at the source rather than on the accumulated findings: the
        # report truncates at _LEAK_REPORT_LIMIT, so readdir order would decide
        # which findings get named. Sorting here also pins `scanned` traversal
        # order and any accumulator added later. This yields a deterministic
        # order, not lexicographic full-path order - os.walk is top-down, so a
        # directory's own files precede everything in its subdirectories.
        dirs.sort()
        files.sort()
        for filename in files:
            artifact = Path(root) / filename
            # Symlinks are not read: the target is walked on its own, and
            # following would double the reader invocations. The NAME still
            # counts as provided when the target is an ELF inside the tree,
            # because that is precisely the case where the walk reads it - a
            # soname is usually spelled by the versioned symlink beside the
            # real file (`libmicrohttpd.so.12` -> `libmicrohttpd.so.12.0.2`).
            if artifact.is_symlink():
                target = Path(os.path.realpath(artifact))
                if _lexically_within(target, work_root) and _is_elf(target):
                    provided.add(filename)
                continue
            if not _is_elf(artifact):
                continue
            provided.add(filename)
            info = _read_elf(reader, artifact)
            if info is None:
                pending.append(f"{_neutralized(artifact)} could not be read by {reader}")
                continue
            if not info.dynamic:
                # A relocatable .o or a static binary. It names no version node
                # and no DT_NEEDED, so every loop below is empty for it - but
                # counting it would let a tree holding nothing but leftover .o
                # files clear the zero-evidence floor and PASS a BLOCK-severity
                # gate on files that state no requirement at all. `scanned` has
                # to mean "artifacts that could have carried a leak".
                continue
            scanned += 1
            recipe = _producing_recipe(work, artifact)
            leaks.extend(
                _NativeLeak(artifact=artifact, recipe=recipe, node=node, source="the artifact itself")
                for node in _nodes_above(info.nodes, ceiling)
            )
            search_dirs = [*_runpath_dirs(info, artifact), *_HOST_LIB_DIRS]
            for soname in info.needed:
                dependency, refused = _resolve_needed(soname, search_dirs, permitted)
                if dependency is None:
                    if refused:
                        pending.append(
                            f"{_neutralized(artifact)} (recipe {_neutralized(recipe)}) "
                            f"declares {_neutralized(soname)}, "
                            f"which lands outside the scanned roots and is out of scope"
                        )
                    else:
                        # Held back, never classified here: a provider that
                        # sorts after its consumer is not in `provided` yet.
                        # A refused entry took the branch above and is never
                        # offered to `_unchecked_reason` at all.
                        pending.append(_Unclassified(artifact=artifact, recipe=recipe, soname=soname, info=info))
                    continue
                if _within_any(dependency, sanctioned):
                    continue
                # Already the resolved path, and already the value the
                # confinement was tested on - see _resolve_needed. Resolving it
                # a second time here is what let a swapped symlink steer the
                # reader onto an arbitrary path after the check had passed.
                resolved = dependency
                if resolved not in dep_cache:
                    dep_info = _read_elf(reader, resolved)
                    dep_cache[resolved] = dep_info.nodes if dep_info is not None else None
                dep_nodes = dep_cache[resolved]
                if dep_nodes is None:
                    pending.append(
                        f"{_neutralized(artifact)} (recipe {_neutralized(recipe)}) "
                        f"declares {_neutralized(soname)} at {_neutralized(resolved)}, "
                        f"which {reader} could not read"
                    )
                    continue
                leaks.extend(
                    _NativeLeak(
                        artifact=artifact,
                        recipe=recipe,
                        node=node,
                        # Neutralized per component here rather than in
                        # describe(), so the bound applies to each artifact-derived
                        # part instead of to the sentence as a whole.
                        source=f"dependency {_neutralized(soname)} at {_neutralized(resolved)}",
                    )
                    for node in _nodes_above(dep_nodes, ceiling)
                )
    unresolved: list[str] = []
    frozen = frozenset(provided)
    for item in pending:
        if isinstance(item, str):
            unresolved.append(item)
            continue
        reason = _unchecked_reason(item.artifact, item.soname, item.info, frozen)
        if reason is not None:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        unresolved.append(
            f"{_neutralized(item.artifact)} (recipe {_neutralized(item.recipe)}) "
            f"declares {_neutralized(item.soname)}, which resolves to no file"
        )
    return leaks, unresolved, scanned, excluded


def _leak_report(items: list[str]) -> str:
    """Join finding lines, summarizing the tail past ``_LEAK_REPORT_LIMIT``.

    The join is what makes an entry boundary, so the separator has to be a
    character no entry can contain - otherwise a directory named
    ``a) reaches GLIBC_2.99 via the artifact itself; `` renders one leaked
    artifact as two findings, the second naming a library nothing ever read,
    with only the header's count as a tell. ``_neutralized`` strips
    ``_ENTRY_SEPARATOR``'s character from every artifact-derived string, so a
    boundary can only come from here.
    """
    if len(items) <= _LEAK_REPORT_LIMIT:
        return _ENTRY_SEPARATOR.join(items)
    head = _ENTRY_SEPARATOR.join(items[:_LEAK_REPORT_LIMIT])
    return f"{head}{_ENTRY_SEPARATOR}and {len(items) - _LEAK_REPORT_LIMIT} more"
