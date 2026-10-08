"""The AArch64 marking check that mold.bbclass runs over an image rootfs.

mold drops the BTI/PAC property note that GNU ld writes. The check recognises a
file that lost it from two facts: its code has a BTI or PAC instruction, and the
property note lacks the matching bit. It does not ask which linker made the file,
because packaging strips ``.comment``. These tests build tiny ELF files to pin
each decision, because the real inputs only exist inside a Yocto build.
"""

from __future__ import annotations

import ast
import importlib.util
import struct
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import bakar

if TYPE_CHECKING:
    from types import ModuleType

_LAYER = Path(bakar.__file__).parent / "overlays/meta-bakar-mold"
_MODULE = _LAYER / "lib/bakar_mold/aarch64_marking.py"

_BTI_C = struct.pack("<I", 0xD503245F)
_PACIASP = struct.pack("<I", 0xD503233F)
_NOP = struct.pack("<I", 0xD503201F)
_MOLD_COMMENT = b"mold 3.0.0 (8de38c35a2df16a25f7ff87ac3ad07156a925beb; compatible with GNU ld)\0"
_GCC_COMMENT = b"GCC: (GNU) 16.2.0\0"

_SHT_PROGBITS = 1
_SHT_STRTAB = 3
_SHT_NOTE = 7


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("aarch64_marking", _MODULE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules[cls.__module__]
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


marking = _load()


def _property_note(bits: int, *, leading_other_property: bool = False) -> bytes:
    """A .note.gnu.property section carrying GNU_PROPERTY_AARCH64_FEATURE_1_AND."""
    props = b""
    if leading_other_property:
        props += struct.pack("<II", 0xC0008002, 4) + struct.pack("<I", 1) + b"\0" * 4
    props += struct.pack("<II", 0xC0000000, 4) + struct.pack("<I", bits) + b"\0" * 4
    return struct.pack("<III", 4, len(props), 5) + b"GNU\0" + props


def _elf(
    *,
    text: bytes = _NOP,
    comment: bytes | None = _GCC_COMMENT,
    note_bits: int | None = None,
    leading_other_property: bool = False,
    **header: int,
) -> bytes:
    """Build an ELF64 file; ``header`` overrides machine, etype, ei_class or ei_data."""
    machine = header.get("machine", 183)
    etype = header.get("etype", 3)
    ei_class = header.get("ei_class", 2)
    ei_data = header.get("ei_data", 1)
    sections: list[tuple[str, int, bytes]] = [(".text", _SHT_PROGBITS, text)]
    if comment is not None:
        sections.append((".comment", _SHT_PROGBITS, comment))
    if note_bits is not None:
        note = _property_note(note_bits, leading_other_property=leading_other_property)
        sections.append((".note.gnu.property", _SHT_NOTE, note))

    body = bytearray(64)
    shstr = bytearray(b"\0")
    entries = [(0, 0, 0, 0)]
    for name, sh_type, data in sections:
        name_off = len(shstr)
        shstr += name.encode() + b"\0"
        while len(body) % 8:
            body.append(0)
        entries.append((name_off, sh_type, len(body), len(data)))
        body += data
    name_off = len(shstr)
    shstr += b".shstrtab\0"
    while len(body) % 8:
        body.append(0)
    entries.append((name_off, _SHT_STRTAB, len(body), len(shstr)))
    body += shstr
    while len(body) % 8:
        body.append(0)
    shoff = len(body)
    for name_off, sh_type, offset, size in entries:
        body += struct.pack("<IIQQQQIIQQ", name_off, sh_type, 0, 0, offset, size, 0, 0, 1, 0)

    ident = b"\x7fELF" + bytes([ei_class, ei_data, 1]) + b"\0" * 9
    body[:16] = ident
    fields = (etype, machine, 1, 0, 0, shoff, 0, 64, 0, 0, 64, len(entries), len(entries) - 1)
    struct.pack_into("<HHIQQQIHHHHHH", body, 16, *fields)
    return bytes(body)


def _inspect(tmp_path: Path, data: bytes, name: str = "bin"):
    path = tmp_path / name
    path.write_bytes(data)
    return marking.inspect(str(path))


@pytest.mark.unit
def test_mold_linked_hardened_code_without_a_note_lost_its_marking(tmp_path: Path) -> None:
    verdict = _inspect(tmp_path, _elf(text=_BTI_C, comment=_MOLD_COMMENT))
    assert verdict is not None
    assert verdict.mold_linked
    assert verdict.lost_marking


@pytest.mark.unit
def test_a_note_with_the_matching_bit_keeps_the_marking(tmp_path: Path) -> None:
    verdict = _inspect(tmp_path, _elf(text=_BTI_C, comment=_MOLD_COMMENT, note_bits=marking.FEATURE_BTI))
    assert verdict is not None
    assert verdict.feature_bits == marking.FEATURE_BTI
    assert not verdict.lost_marking


@pytest.mark.unit
def test_pac_code_with_only_the_bti_bit_set_lost_the_pac_marking(tmp_path: Path) -> None:
    verdict = _inspect(tmp_path, _elf(text=_PACIASP, comment=_MOLD_COMMENT, note_bits=marking.FEATURE_BTI))
    assert verdict is not None
    assert verdict.lost_marking


@pytest.mark.unit
def test_the_aarch64_property_is_found_after_another_property(tmp_path: Path) -> None:
    data = _elf(text=_BTI_C, comment=_MOLD_COMMENT, note_bits=marking.FEATURE_BTI, leading_other_property=True)
    verdict = _inspect(tmp_path, data)
    assert verdict is not None
    assert verdict.feature_bits == marking.FEATURE_BTI


@pytest.mark.unit
def test_a_mold_linked_file_that_was_never_hardened_is_not_reported(tmp_path: Path) -> None:
    """No bti or paciasp in the code: there was no marking to lose."""
    verdict = _inspect(tmp_path, _elf(text=_NOP * 8, comment=_MOLD_COMMENT))
    assert verdict is not None
    assert verdict.mold_linked
    assert not verdict.lost_marking


@pytest.mark.unit
def test_a_stripped_file_is_judged_by_its_code_and_note_alone(tmp_path: Path) -> None:
    """Packaging removes .comment, so a rootfs file cannot say which linker made it."""
    verdict = _inspect(tmp_path, _elf(text=_BTI_C, comment=None))
    assert verdict is not None
    assert not verdict.mold_linked
    assert verdict.lost_marking


@pytest.mark.unit
def test_hardened_code_with_its_note_is_fine_whichever_linker_made_it(tmp_path: Path) -> None:
    verdict = _inspect(tmp_path, _elf(text=_BTI_C + _PACIASP, comment=_GCC_COMMENT, note_bits=3))
    assert verdict is not None
    assert not verdict.mold_linked
    assert not verdict.lost_marking


@pytest.mark.unit
def test_hardened_code_without_a_note_is_reported_even_when_gnu_ld_made_it(tmp_path: Path) -> None:
    """The cause cannot be told apart from the file, so the report states the fact and not the blame."""
    verdict = _inspect(tmp_path, _elf(text=_BTI_C, comment=_GCC_COMMENT))
    assert verdict is not None
    assert not verdict.mold_linked
    assert verdict.lost_marking


@pytest.mark.unit
def test_an_instruction_pattern_at_an_unaligned_offset_is_not_code(tmp_path: Path) -> None:
    verdict = _inspect(tmp_path, _elf(text=b"\x01" + _BTI_C + b"\0\0\0", comment=_MOLD_COMMENT))
    assert verdict is not None
    assert not verdict.has_bti_code


@pytest.mark.unit
@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("x86-64 file", {"machine": 62}),
        ("relocatable object", {"etype": 1}),
        ("32-bit ELF", {"ei_class": 1}),
        ("big-endian ELF", {"ei_data": 2}),
    ],
)
def test_files_that_are_not_little_endian_aarch64_executables_are_skipped(
    tmp_path: Path, label: str, kwargs: dict[str, int]
) -> None:
    assert _inspect(tmp_path, _elf(text=_BTI_C, comment=_MOLD_COMMENT, **kwargs)) is None, label


@pytest.mark.unit
def test_unreadable_or_damaged_input_is_skipped_without_raising(tmp_path: Path) -> None:
    good = _elf(text=_BTI_C, comment=_MOLD_COMMENT)
    assert _inspect(tmp_path, b"not an elf file at all" * 8, "text") is None
    assert _inspect(tmp_path, b"", "empty") is None
    assert _inspect(tmp_path, good[: len(good) // 2], "truncated") is None
    assert marking.inspect(str(tmp_path / "missing")) is None
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "text")
    assert marking.inspect(str(link)) is None


@pytest.mark.unit
def test_scan_tree_counts_and_lists_what_lost_its_marking(tmp_path: Path) -> None:
    (tmp_path / "usr/bin").mkdir(parents=True)
    (tmp_path / "usr/bin/lost").write_bytes(_elf(text=_BTI_C, comment=_MOLD_COMMENT))
    (tmp_path / "usr/bin/kept").write_bytes(_elf(text=_BTI_C, comment=_MOLD_COMMENT, note_bits=3))
    (tmp_path / "usr/bin/stripped").write_bytes(_elf(text=_PACIASP, comment=None))
    (tmp_path / "usr/bin/plain").write_bytes(_elf(text=_NOP * 4, comment=None))
    (tmp_path / "usr/bin/script").write_text("#!/bin/sh\n")
    report = marking.scan_tree(str(tmp_path))
    assert report.scanned == 4
    assert report.mold_linked == 2
    assert report.lost == ["usr/bin/lost", "usr/bin/stripped"]


@pytest.mark.unit
def test_summary_is_empty_when_nothing_was_lost_and_names_a_sample_otherwise() -> None:
    assert marking.summarize(marking.Report(scanned=10, mold_linked=4)) is None
    report = marking.Report(scanned=10, mold_linked=8, lost=[f"usr/bin/f{i}" for i in range(7)])
    text = marking.summarize(report)
    assert text is not None
    assert text.startswith("7 of 10 AArch64 binaries")
    assert "/usr/bin/f0" in text
    assert "/usr/bin/f6" not in text
    assert "and 2 more" in text
    assert "rui314/mold#1725" in text


@pytest.mark.unit
def test_module_uses_only_syntax_a_python_38_bitbake_host_can_parse() -> None:
    """BitBake runs this on the build host's Python, which can be older than bakar's."""
    ast.parse(_MODULE.read_text(), feature_version=(3, 8))
