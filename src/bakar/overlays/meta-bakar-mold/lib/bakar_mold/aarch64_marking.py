"""Find AArch64 binaries whose BTI/PAC marking mold dropped.

GNU ld writes the AArch64 hardening bits into a ``.note.gnu.property`` section
so the loader can enable BTI and pointer authentication for the binary. mold
does not carry them over (rui314/mold#1725), so a program compiled with
``-mbranch-protection`` and linked by mold runs unprotected, and nothing fails
at build time.

mold also drops ``.ARM.attributes``, so the output keeps no record of the
original marking. The code does: a function compiled with
``-mbranch-protection=standard`` starts with ``bti c`` or ``paciasp``. A file
counts as having lost its marking when its code contains one of those
instructions and the matching bit is missing from the property note. A file that
was never hardened is not reported.

The linker is deliberately not part of that rule. OpenEmbedded strips ``.comment``
when it packages a recipe, so a finished rootfs no longer says which linker made
a file. ``Verdict.mold_linked`` is filled in when ``.comment`` survives (an
unstripped tree), and is informational only.

This module runs inside BitBake, whose host Python can be old, so it uses only
the standard library and syntax from Python 3.8.
"""

from __future__ import annotations

import mmap
import os
import struct
from dataclasses import dataclass, field

EM_AARCH64 = 183
ET_EXEC = 2
ET_DYN = 3
SHT_NOTE = 7

NT_GNU_PROPERTY_TYPE_0 = 5
GNU_PROPERTY_AARCH64_FEATURE_1_AND = 0xC0000000
FEATURE_BTI = 1
FEATURE_PAC = 2

# Named so the formatter, which targets Python 3.14, cannot rewrite the except
# clause into the bracketless form that older BitBake hosts cannot parse.
_UNREADABLE = (OSError, ValueError, struct.error)

# little-endian encodings of the two instructions GCC emits for
# -mbranch-protection=standard, checked against real binaries with objdump
_BTI_C = struct.pack("<I", 0xD503245F)
_PACIASP = struct.pack("<I", 0xD503233F)


@dataclass(frozen=True)
class Verdict:
    """What one AArch64 ELF file looks like with respect to mold and its marking."""

    path: str
    mold_linked: bool
    feature_bits: int
    has_bti_code: bool
    has_pac_code: bool

    @property
    def lost_marking(self) -> bool:
        lost_bti = self.has_bti_code and not self.feature_bits & FEATURE_BTI
        lost_pac = self.has_pac_code and not self.feature_bits & FEATURE_PAC
        return lost_bti or lost_pac


@dataclass
class Report:
    scanned: int = 0
    mold_linked: int = 0
    lost: list = field(default_factory=list)


def _sections(mm):
    """Return ``{name: (offset, size, type)}`` for a 64-bit little-endian ELF, or None."""
    shoff = struct.unpack_from("<Q", mm, 40)[0]
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", mm, 58)
    if not shoff or not shnum or shentsize < 64 or shstrndx >= shnum:
        return None
    if shoff + shnum * shentsize > len(mm):
        return None
    headers = []
    for i in range(shnum):
        sh_name, sh_type = struct.unpack_from("<II", mm, shoff + i * shentsize)
        sh_offset, sh_size = struct.unpack_from("<QQ", mm, shoff + i * shentsize + 24)
        headers.append((sh_name, sh_type, sh_offset, sh_size))
    str_off, str_size = headers[shstrndx][2], headers[shstrndx][3]
    if str_off + str_size > len(mm):
        return None
    names = bytes(mm[str_off : str_off + str_size])
    sections = {}
    for sh_name, sh_type, sh_offset, sh_size in headers:
        end = names.find(b"\0", sh_name)
        if end < 0:
            continue
        sections[names[sh_name:end].decode("latin-1")] = (sh_offset, sh_size, sh_type)
    return sections


def _feature_bits(mm, offset, size):
    """Read GNU_PROPERTY_AARCH64_FEATURE_1_AND out of a ``.note.gnu.property`` section."""
    if offset + size > len(mm):
        return 0
    pos, end = offset, offset + size
    while pos + 12 <= end:
        namesz, descsz, ntype = struct.unpack_from("<III", mm, pos)
        desc = pos + 12 + ((namesz + 3) & ~3)
        if ntype == NT_GNU_PROPERTY_TYPE_0 and desc + descsz <= end:
            prop = desc
            while prop + 8 <= desc + descsz:
                pr_type, pr_datasz = struct.unpack_from("<II", mm, prop)
                if pr_type == GNU_PROPERTY_AARCH64_FEATURE_1_AND and pr_datasz >= 4:
                    return struct.unpack_from("<I", mm, prop + 8)[0]
                prop += 8 + ((pr_datasz + 7) & ~7)
        pos = desc + ((descsz + 3) & ~3)
    return 0


def _contains_word(mm, pattern, start, size):
    """True when ``pattern`` occurs at a 4-byte aligned offset inside the section."""
    end = start + size
    pos = mm.find(pattern, start, end)
    while pos != -1:
        if (pos - start) % 4 == 0:
            return True
        pos = mm.find(pattern, pos + 1, end)
    return False


def inspect(path):
    """Return a :class:`Verdict` for an AArch64 executable or shared object, else None.

    Anything that is not such a file, or that cannot be read or parsed, returns
    None: the check reports what it can prove and stays quiet about the rest.
    """
    try:
        if os.path.islink(path) or os.path.getsize(path) < 64:
            return None
        with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            if mm[:4] != b"\x7fELF" or mm[4] != 2 or mm[5] != 1:
                return None
            e_type, e_machine = struct.unpack_from("<HH", mm, 16)
            if e_machine != EM_AARCH64 or e_type not in (ET_EXEC, ET_DYN):
                return None
            sections = _sections(mm)
            if sections is None:
                return None
            comment = sections.get(".comment")
            mold_linked = False
            if comment and comment[0] + comment[1] <= len(mm):
                mold_linked = b"mold " in bytes(mm[comment[0] : comment[0] + comment[1]])
            bits = 0
            note = sections.get(".note.gnu.property")
            if note and note[2] == SHT_NOTE:
                bits = _feature_bits(mm, note[0], note[1])
            has_bti = has_pac = False
            text = sections.get(".text")
            if text and text[0] + text[1] <= len(mm):
                has_bti = _contains_word(mm, _BTI_C, text[0], text[1])
                has_pac = _contains_word(mm, _PACIASP, text[0], text[1])
            return Verdict(path, mold_linked, bits, has_bti, has_pac)
    except _UNREADABLE:
        return None


def scan_tree(root):
    """Inspect every regular file under ``root`` and return a :class:`Report`."""
    report = Report()
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(filenames):
            verdict = inspect(os.path.join(dirpath, name))
            if verdict is None:
                continue
            report.scanned += 1
            if verdict.mold_linked:
                report.mold_linked += 1
            if verdict.lost_marking:
                report.lost.append(os.path.relpath(verdict.path, root))
    report.lost.sort()
    return report


def summarize(report, sample=5):
    """One-line summary naming the count and a few example paths, or None when nothing was lost."""
    if not report.lost:
        return None
    shown = ", ".join("/" + p for p in report.lost[:sample])
    more = "" if len(report.lost) <= sample else f" and {len(report.lost) - sample} more"
    return (
        f"{len(report.lost)} of {report.scanned} AArch64 binaries in this image use BTI/PAC "
        f"instructions but carry no matching BTI/PAC marking (e.g. {shown}{more}). "
        "mold drops the marking when it links and GNU ld keeps it (rui314/mold#1725)."
    )
