"""Read rpm-md repository metadata for ``bakar feed mirror``, and hash what it names.

No bakar imports. XML parsing goes through ``defusedxml`` rather than the
stdlib ``xml.etree`` directly, since every byte parsed here comes from a
remote source. This module reports what a source's metadata SAYS - including
a package location's ``xml:base`` - and never judges whether a location is
safe to write; that policy lives in ``feed_mirror_paths``.

A primary listing is a compressed file served by a remote host, so its expanded
size is whatever that host chooses. Every read of one goes through a counting
reader that asks the decompressor for at most one byte past the declared
``open-size`` (a hard cap when the index declares none) and refuses the moment
that byte arrives. An oversized or bomb-shaped listing therefore costs at most
the bound, never its full expansion.

Locations and checksums are paired structurally: both come from the same
``<package>`` element of an ``iterparse`` walk. Pairing them by pattern over the
text would let one package's checksum vouch for another's location.
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import lzma
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, BinaryIO

from defusedxml import ElementTree as ET  # noqa: N817 - ET for ElementTree is the established convention

try:
    from compression import zstd
except ImportError:  # pragma: no cover - Python 3.14 ships it; a build without _zstd may not
    zstd = None

if TYPE_CHECKING:
    from _hashlib import HASH
    from collections.abc import Callable, Iterator
    from pathlib import Path

    # Type-only, never executed at runtime: defusedxml.ElementTree does not
    # re-export the Element class itself, but the elements it parses are the
    # same xml.etree.ElementTree.Element instances underneath.
    # nosemgrep: python.lang.security.use-defused-xml.use-defused-xml
    from xml.etree.ElementTree import Element

_REPO_NS = "http://linux.duke.edu/metadata/repo"
_COMMON_NS = "http://linux.duke.edu/metadata/common"
_XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"

_PACKAGE_TAG = f"{{{_COMMON_NS}}}package"
_METADATA_TAG = f"{{{_COMMON_NS}}}metadata"

# Bound on a primary's decompressed size when its index declares no open-size.
# The largest live primary measured on the published feed is a few MiB; this is
# a ceiling against a hostile listing, not an expected size.
DEFAULT_OPEN_SIZE_CAP = 1 << 30

_READ_CHUNK = 64 * 1024

_HASHERS: dict[str, Callable[[], HASH]] = {
    "sha": hashlib.sha1,
    "sha1": hashlib.sha1,
    "sha224": hashlib.sha224,
    "sha256": hashlib.sha256,
    "sha384": hashlib.sha384,
    "sha512": hashlib.sha512,
}

_DECOMPRESS_ERRORS: tuple[type[BaseException], ...] = (
    ET.ParseError,
    OSError,
    EOFError,
    zlib.error,
    lzma.LZMAError,
) + ((zstd.ZstdError,) if zstd is not None else ())


class MetadataError(ValueError):
    """Repository metadata that cannot be trusted or read, naming what failed."""


@dataclass(frozen=True, slots=True)
class MetadataFile:
    """One ``<data>`` entry of ``repomd.xml``."""

    type: str
    href: str
    checksum_type: str
    checksum: str
    size: int | None
    open_size: int | None


@dataclass(frozen=True, slots=True)
class RepoIndex:
    """A parsed ``repomd.xml``."""

    revision: str
    files: tuple[MetadataFile, ...]

    @property
    def primary(self) -> MetadataFile:
        for entry in self.files:
            if entry.type == "primary":
                return entry
        raise MetadataError("repomd.xml names no primary metadata file")


@dataclass(frozen=True, slots=True)
class PackageEntry:
    """One ``<package>`` of a primary listing, as the listing declares it."""

    href: str
    base: str | None
    checksum_type: str
    checksum: str
    size: int


def _int_attr(value: str | None, *, what: str) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except ValueError:
        raise MetadataError(f"{what} is not an integer: {value!r}") from None
    if number < 0:
        raise MetadataError(f"{what} is negative: {number}")
    return number


def _parse_data(data: Element) -> MetadataFile:
    kind = data.get("type")
    if not kind:
        raise MetadataError("repomd.xml <data> element has no type attribute")
    location = data.find(f"{{{_REPO_NS}}}location")
    href = location.get("href") if location is not None else None
    if not href:
        raise MetadataError(f"repomd.xml <data type={kind!r}> has no <location href>")
    checksum = data.find(f"{{{_REPO_NS}}}checksum")
    checksum_type = checksum.get("type") if checksum is not None else None
    digest = (checksum.text or "").strip() if checksum is not None else ""
    if not checksum_type or not digest:
        raise MetadataError(f"repomd.xml <data type={kind!r}> has no <checksum type> with a value")
    return MetadataFile(
        type=kind,
        href=href,
        checksum_type=checksum_type,
        checksum=digest,
        size=_int_attr(data.findtext(f"{{{_REPO_NS}}}size"), what=f"repomd.xml <data type={kind!r}> <size>"),
        open_size=_int_attr(
            data.findtext(f"{{{_REPO_NS}}}open-size"), what=f"repomd.xml <data type={kind!r}> <open-size>"
        ),
    )


def parse_repomd(body: bytes) -> RepoIndex:
    """Parse a ``repomd.xml`` body; every ``<data>`` must carry a location and checksum."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise MetadataError(f"repomd.xml is not well-formed XML: {exc}") from None
    if root.tag != f"{{{_REPO_NS}}}repomd":
        raise MetadataError(f"repomd.xml root element is {root.tag!r}, expected repomd in {_REPO_NS}")
    revision = (root.findtext(f"{{{_REPO_NS}}}revision") or "").strip()
    files = tuple(_parse_data(data) for data in root.iter(f"{{{_REPO_NS}}}data"))
    return RepoIndex(revision=revision, files=files)


class _BoundedReader:
    """Read a decompressed stream, refusing the first byte past ``limit``."""

    def __init__(self, inner: BinaryIO, *, limit: int, href: str) -> None:
        self._inner = inner
        self._limit = limit
        self._href = href
        self._total = 0

    def read(self, size: int | None = -1) -> bytes:
        want = _READ_CHUNK if size is None or size < 0 else size
        # One byte past the bound is enough to prove it was exceeded; asking for
        # more would make the decompressor expand data this reader then discards.
        chunk = self._inner.read(min(want, self._limit - self._total + 1))
        self._total += len(chunk)
        if self._total > self._limit:
            raise MetadataError(
                f"{self._href}: decompressed listing exceeds its declared open size of {self._limit} bytes"
            )
        return chunk


def _decompressor(stream: BinaryIO, href: str) -> BinaryIO:
    suffix = PurePosixPath(href).suffix
    if suffix == ".gz":
        return gzip.GzipFile(fileobj=stream, mode="rb")
    if suffix == ".xz":
        return lzma.LZMAFile(stream)
    if suffix == ".bz2":
        return bz2.BZ2File(stream)
    if suffix == ".zst":
        if zstd is None:
            raise MetadataError(f"{href}: zstd compression is unsupported by this Python (no compression.zstd)")
        return zstd.ZstdFile(stream)
    if suffix == ".xml":
        return stream
    raise MetadataError(f"{href}: unsupported compression format {suffix or '(no suffix)'!r}")


def _package_entry(package: Element, *, href: str, index: int) -> PackageEntry:
    name = (package.findtext(f"{{{_COMMON_NS}}}name") or "").strip() or f"#{index}"
    label = f"{href}: package {name!r}"
    checksum = package.find(f"{{{_COMMON_NS}}}checksum")
    checksum_type = checksum.get("type") if checksum is not None else None
    digest = (checksum.text or "").strip() if checksum is not None else ""
    if not checksum_type or not digest:
        raise MetadataError(f"{label} has no <checksum type> with a value")
    size_element = package.find(f"{{{_COMMON_NS}}}size")
    size = _int_attr(size_element.get("package") if size_element is not None else None, what=f"{label} <size package>")
    if size is None:
        raise MetadataError(f"{label} has no <size package>")
    location = package.find(f"{{{_COMMON_NS}}}location")
    if location is None or not location.get("href"):
        raise MetadataError(f"{label} has no <location href>")
    location_href = location.get("href", "")
    return PackageEntry(
        href=location_href,
        base=location.get(_XML_BASE),
        checksum_type=checksum_type,
        checksum=digest,
        size=size,
    )


def iter_primary(stream: BinaryIO, *, href: str, open_size: int | None) -> Iterator[PackageEntry]:
    """Yield every package of a primary listing, decompressing at most ``open_size`` bytes.

    ``href`` selects the decompressor by suffix and names the listing in every
    error. A listing whose root is not ``metadata`` in the common namespace is
    refused rather than read as an empty repository.
    """
    # A declared open-size is a ceiling on what THIS listing is allowed to
    # decompress to, never a licence to raise the ceiling: a hostile index
    # could declare an arbitrarily large open-size to make its own excessive
    # value the effective bound, so the declared value is only ever narrowed
    # against the default cap, never widened past it.
    limit = DEFAULT_OPEN_SIZE_CAP if open_size is None else min(open_size, DEFAULT_OPEN_SIZE_CAP)
    if limit < 0:
        raise MetadataError(f"{href}: declared open size is negative: {limit}")
    decompressed = _decompressor(stream, href)
    last: Element | None = None
    count = 0
    try:
        reader = _BoundedReader(decompressed, limit=limit, href=href)
        for _event, element in ET.iterparse(reader, events=("end",)):
            last = element
            if element.tag == _PACKAGE_TAG:
                count += 1
                entry = _package_entry(element, href=href, index=count)
                element.clear()
                yield entry
    except _DECOMPRESS_ERRORS as exc:
        raise MetadataError(f"{href}: cannot read listing: {exc}") from None
    finally:
        if decompressed is not stream:
            decompressed.close()
    if last is None or last.tag != _METADATA_TAG:
        found = "nothing" if last is None else repr(last.tag)
        raise MetadataError(f"{href}: root element is {found}, expected metadata in {_COMMON_NS}")


def new_hasher(checksum_type: str) -> HASH:
    """Return a fresh hash object for an rpm-md checksum type; md5 and unknowns refuse."""
    factory = _HASHERS.get(checksum_type)
    if factory is None:
        supported = ", ".join(sorted(_HASHERS))
        raise MetadataError(f"unsupported checksum algorithm {checksum_type!r} (supported: {supported})")
    return factory()


def file_digest(path: Path, checksum_type: str) -> str:
    """Hex digest of ``path`` under ``checksum_type``, read in a stream."""
    hasher = new_hasher(checksum_type)
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, lambda: hasher).hexdigest()
