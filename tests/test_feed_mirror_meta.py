"""Tests for rpm-md metadata parsing and digests used by ``bakar feed mirror``."""

from __future__ import annotations

import bz2
import gzip
import hashlib
import io
import lzma

import pytest

from bakar import feed_mirror_meta as meta
from bakar.feed_mirror_meta import MetadataError, PackageEntry, file_digest, iter_primary, new_hasher, parse_repomd

_COMMON = "http://linux.duke.edu/metadata/common"
_REPO = "http://linux.duke.edu/metadata/repo"


def _package(index: int, *, checksum: bool = True, size: bool = True, location: str | None = "default") -> str:
    sha = hashlib.sha256(str(index).encode()).hexdigest()
    parts = [f"<package type='rpm'><name>pkg{index}</name>"]
    if checksum:
        parts.append(f"<checksum type='sha256' pkgid='YES'>{sha}</checksum>")
    if size:
        parts.append(f"<size package='{100 + index}' installed='1' archive='1'/>")
    if location == "default":
        parts.append(f"<location href='../../_pkgs/{sha[:2]}/{sha}.rpm'/>")
    elif location is not None:
        parts.append(location)
    parts.append("</package>")
    return "".join(parts)


def _primary(packages: list[str]) -> bytes:
    body = "".join(packages)
    return (
        f"<?xml version='1.0' encoding='UTF-8'?>"
        f"<metadata xmlns='{_COMMON}' xmlns:rpm='http://linux.duke.edu/metadata/rpm' packages='{len(packages)}'>"
        f"{body}</metadata>"
    ).encode()


def _entries(raw: bytes, *, href: str = "repodata/x-primary.xml", open_size: int | None = None) -> list[PackageEntry]:
    return list(iter_primary(io.BytesIO(raw), href=href, open_size=open_size))


def _repomd(data: str, *, revision: str = "1700000000") -> bytes:
    return f"<repomd xmlns='{_REPO}'><revision>{revision}</revision>{data}</repomd>".encode()


_PRIMARY_DATA = (
    "<data type='primary'>"
    "<checksum type='sha256'>abc</checksum><open-checksum type='sha256'>def</open-checksum>"
    "<location href='repodata/abc-primary.xml.gz'/><timestamp>1</timestamp>"
    "<size>123</size><open-size>456</open-size></data>"
)


# --- parse_repomd ---------------------------------------------------------------------------


def test_parse_repomd_reads_every_data_entry() -> None:
    other = "<data type='other'><checksum type='sha512'>fff</checksum><location href='repodata/o.xml.gz'/></data>"
    index = parse_repomd(_repomd(_PRIMARY_DATA + other, revision="42"))

    assert index.revision == "42"
    assert index.files == (
        meta.MetadataFile("primary", "repodata/abc-primary.xml.gz", "sha256", "abc", 123, 456),
        meta.MetadataFile("other", "repodata/o.xml.gz", "sha512", "fff", None, None),
    )
    assert index.primary is index.files[0]


def test_parse_repomd_without_primary_refuses_on_access() -> None:
    other = "<data type='other'><checksum type='sha256'>f</checksum><location href='repodata/o.xml.gz'/></data>"
    index = parse_repomd(_repomd(other))

    with pytest.raises(MetadataError, match="no primary"):
        _ = index.primary


def test_parse_repomd_malformed_xml_refused() -> None:
    with pytest.raises(MetadataError, match=r"repomd\.xml is not well-formed"):
        parse_repomd(b"<repomd><data")


def test_parse_repomd_wrong_root_refused() -> None:
    with pytest.raises(MetadataError, match="root element"):
        parse_repomd(b"<repomd><data type='primary'/></repomd>")


def test_parse_repomd_data_without_location_names_type() -> None:
    data = "<data type='filelists'><checksum type='sha256'>a</checksum></data>"
    with pytest.raises(MetadataError, match=r"filelists.*location"):
        parse_repomd(_repomd(data))


def test_parse_repomd_data_without_checksum_type_names_type() -> None:
    data = "<data type='primary'><checksum>a</checksum><location href='repodata/p.xml.gz'/></data>"
    with pytest.raises(MetadataError, match=r"primary.*checksum"):
        parse_repomd(_repomd(data))


def test_parse_repomd_non_integer_open_size_refused() -> None:
    data = _PRIMARY_DATA.replace("<open-size>456", "<open-size>lots")
    with pytest.raises(MetadataError, match="open-size"):
        parse_repomd(_repomd(data))


# --- iter_primary: open_size bound ----------------------------------------------------------


def test_open_size_listing_larger_than_declared_refused() -> None:
    raw = _primary([_package(i) for i in range(5)])

    with pytest.raises(MetadataError, match=r"x-primary\.xml\.gz.*open size of 10 bytes"):
        _entries(gzip.compress(raw), href="repodata/x-primary.xml.gz", open_size=10)


def test_open_size_exact_size_accepted() -> None:
    raw = _primary([_package(i) for i in range(5)])

    entries = _entries(gzip.compress(raw), href="repodata/x-primary.xml.gz", open_size=len(raw))

    assert len(entries) == 5


def test_open_size_one_byte_short_refused() -> None:
    raw = _primary([_package(i) for i in range(5)])

    with pytest.raises(MetadataError, match="open size"):
        _entries(raw, open_size=len(raw) - 1)


def test_open_size_none_uses_hard_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _primary([_package(i) for i in range(5)])
    monkeypatch.setattr(meta, "DEFAULT_OPEN_SIZE_CAP", len(raw) - 1)

    with pytest.raises(MetadataError, match=f"open size of {len(raw) - 1} bytes"):
        _entries(raw, open_size=None)


def test_open_size_declared_larger_than_cap_is_still_bound_by_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared open-size only ever narrows the bound, never widens it.

    A hostile index could declare an arbitrarily large open-size to make its
    own value the effective bound instead of the hard cap; the cap must win
    regardless of what the index claims.
    """
    raw = _primary([_package(i) for i in range(5)])
    monkeypatch.setattr(meta, "DEFAULT_OPEN_SIZE_CAP", len(raw) - 1)

    with pytest.raises(MetadataError, match=f"open size of {len(raw) - 1} bytes"):
        _entries(raw, open_size=len(raw) * 1000)


def test_open_size_bound_stops_decompression_early() -> None:
    # 64 MiB of padding after the packages; the reader must refuse without expanding it all.
    raw = _primary([_package(0)]).replace(b"</metadata>", b"<!--" + b" " * (64 << 20) + b"--></metadata>")
    compressed = lzma.compress(raw)

    with lzma.LZMAFile(io.BytesIO(compressed)) as inner:
        reader = meta._BoundedReader(inner, limit=4096, href="p.xml.xz")
        with pytest.raises(MetadataError):
            while reader.read(1 << 20):
                pass

        assert inner.tell() <= 4097


# --- iter_primary: compression --------------------------------------------------------------


def test_compression_unknown_suffix_refused() -> None:
    raw = _primary([_package(0)])

    with pytest.raises(MetadataError, match=r"unsupported compression format '\.lz4'"):
        _entries(raw, href="repodata/x-primary.xml.lz4")


def test_compression_missing_suffix_refused() -> None:
    with pytest.raises(MetadataError, match="no suffix"):
        _entries(_primary([]), href="repodata/primary")


def test_compression_gz_parses() -> None:
    raw = _primary([_package(i) for i in range(3)])

    entries = _entries(gzip.compress(raw), href="repodata/x-primary.xml.gz")

    sha = hashlib.sha256(b"1").hexdigest()
    assert entries[1] == PackageEntry(
        href=f"../../_pkgs/{sha[:2]}/{sha}.rpm", base=None, checksum_type="sha256", checksum=sha, size=101
    )
    assert len(entries) == 3


@pytest.mark.parametrize(
    ("suffix", "compress"),
    [(".xz", lzma.compress), (".bz2", bz2.compress), (".xml", lambda data: data)],
)
def test_compression_other_formats_parse(suffix: str, compress) -> None:
    raw = _primary([_package(i) for i in range(4)])

    entries = _entries(compress(raw), href=f"repodata/x-primary{suffix}")

    assert [entry.size for entry in entries] == [100, 101, 102, 103]


def test_compression_zst_parses() -> None:
    zstd = pytest.importorskip("compression.zstd")
    raw = _primary([_package(i) for i in range(4)])

    entries = _entries(zstd.compress(raw), href="repodata/x-primary.xml.zst")

    assert len(entries) == 4


def test_compression_zst_without_module_names_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meta, "zstd", None)

    with pytest.raises(MetadataError, match="zstd"):
        _entries(b"irrelevant", href="repodata/x-primary.xml.zst")


def test_compression_corrupt_gz_refused() -> None:
    with pytest.raises(MetadataError, match=r"x-primary\.xml\.gz: cannot read"):
        _entries(b"\x1f\x8bnot really gzip", href="repodata/x-primary.xml.gz")


def test_compression_mismatched_format_refused() -> None:
    raw = _primary([_package(0)])

    with pytest.raises(MetadataError, match="cannot read"):
        _entries(lzma.compress(raw), href="repodata/x-primary.xml.gz")


# --- iter_primary: package validation -------------------------------------------------------


def test_package_missing_checksum_refused_naming_it() -> None:
    raw = _primary([_package(0), _package(1, checksum=False)])

    with pytest.raises(MetadataError, match=r"'pkg1'.*checksum"):
        _entries(raw)


def test_package_missing_size_refused_naming_it() -> None:
    raw = _primary([_package(7, size=False)])

    with pytest.raises(MetadataError, match=r"'pkg7'.*size package"):
        _entries(raw)


def test_package_missing_location_refused_naming_it() -> None:
    raw = _primary([_package(3, location=None)])

    with pytest.raises(MetadataError, match=r"'pkg3'.*location href"):
        _entries(raw)


def test_package_location_xml_base_is_surfaced() -> None:
    location = "<location xml:base='http://elsewhere.example/' href='Packages/a.rpm'/>"
    raw = _primary([_package(0, location=location)])

    (entry,) = _entries(raw)

    assert entry.base == "http://elsewhere.example/"
    assert entry.href == "Packages/a.rpm"


def test_primary_malformed_xml_refused() -> None:
    raw = _primary([_package(0)])[:-20]

    with pytest.raises(MetadataError, match="cannot read listing"):
        _entries(raw)


def test_primary_wrong_namespace_refused_rather_than_empty() -> None:
    raw = b"<metadata xmlns='http://example.invalid/'><package/></metadata>"

    with pytest.raises(MetadataError, match="root element"):
        _entries(raw)


def test_primary_empty_listing_yields_nothing() -> None:
    assert _entries(_primary([])) == []


def test_primary_ten_thousand_entries_all_yielded() -> None:
    count = 10_000
    raw = _primary([_package(i) for i in range(count)])

    entries = list(iter_primary(io.BytesIO(gzip.compress(raw)), href="p.xml.gz", open_size=len(raw)))

    assert len(entries) == count
    assert {entry.checksum for entry in entries} == {hashlib.sha256(str(i).encode()).hexdigest() for i in range(count)}


# --- hashing --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("sha", "sha1"),
        ("sha1", "sha1"),
        ("sha224", "sha224"),
        ("sha256", "sha256"),
        ("sha384", "sha384"),
        ("sha512", "sha512"),
    ],
)
def test_new_hasher_supported_algorithms(name: str, expected: str) -> None:
    hasher = new_hasher(name)
    hasher.update(b"payload")

    assert hasher.hexdigest() == hashlib.new(expected, b"payload").hexdigest()


def test_new_hasher_returns_fresh_object_each_call() -> None:
    first = new_hasher("sha256")
    first.update(b"x")

    assert new_hasher("sha256").hexdigest() == hashlib.sha256().hexdigest()


@pytest.mark.parametrize("name", ["md5", "SHA256", "crc32", ""])
def test_unsupported_checksum_refused(name: str) -> None:
    with pytest.raises(MetadataError, match=f"unsupported checksum algorithm {name!r}"):
        new_hasher(name)


def test_file_digest_matches_hashlib(tmp_path) -> None:
    path = tmp_path / "blob.rpm"
    payload = bytes(range(256)) * 1000
    path.write_bytes(payload)

    assert file_digest(path, "sha256") == hashlib.sha256(payload).hexdigest()
    # "sha" is rpm-md's own legacy name for SHA1 (see _HASHERS); this proves
    # file_digest maps it correctly, not a security use of the algorithm.
    # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
    assert file_digest(path, "sha") == hashlib.sha1(payload).hexdigest()


def test_unsupported_checksum_file_digest_refused_before_open(tmp_path) -> None:
    with pytest.raises(MetadataError, match="md5"):
        file_digest(tmp_path / "missing.rpm", "md5")
