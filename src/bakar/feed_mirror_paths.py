"""Input validation and write confinement for ``bakar feed mirror``.

Everything a mirror writes is named by someone else: the operator names the
source and the repositories, and the source's own metadata names every file
beneath them. None of those names may choose where a byte lands. This module
turns each one into a destination relative to the local channel directory, or
refuses it, before any request is made.

Pure functions over strings and paths. Nothing here opens a socket or writes a
file; ``resolve_destination`` reads the filesystem only to follow symlinks.
"""

from __future__ import annotations

import posixpath
from pathlib import Path, PurePosixPath
from urllib.parse import SplitResult, urlsplit

_SCHEMES = ("http", "https")
_POOL = "_pkgs"
_SNAPSHOTS = "snapshots"
_REPODATA = "repodata"
# The first segment of a repository path may not be a channel-level directory:
# the pool and the snapshot tree are shared by every repository in the channel.
_RESERVED_ROOTS = frozenset({_POOL, _SNAPSHOTS})
# Files the mirror itself publishes into repodata/, last and atomically. A source
# index naming one of them as an ordinary metadata entry would overwrite the
# pointer before the data it points at has been verified.
_PUBLISH_TARGETS = frozenset({"repomd.xml", "repomd.xml.asc", "repomd.xml.key"})
_PARTIAL_SUFFIX = ".part"
_PACKAGE_SUFFIX = ".rpm"


class UnsafePathError(ValueError):
    """A URL, repository path or metadata href that the mirror refuses to follow."""


def _has_control(value: str) -> bool:
    return any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in value)


def _redacted(parts: SplitResult) -> str:
    # The message must name the URL, but not repeat the secret it was refused for.
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return parts._replace(netloc=f"***@{host}").geturl()


def validate_source_url(url: str) -> str:
    """Return ``url`` without a trailing ``/`` when it is a plain http(s) feed root.

    Credentials are refused rather than stripped: the source URL is recorded in a
    file inside the served tree, and a mirror host serves that tree to anyone.
    """
    if not url or _has_control(url):
        raise UnsafePathError(f"source URL {url!r} is empty or contains whitespace or control characters")
    parts = urlsplit(url)
    if parts.scheme.lower() not in _SCHEMES:
        accepted = ", ".join(f"{s}://" for s in _SCHEMES)
        raise UnsafePathError(f"source URL {url!r} has scheme {parts.scheme!r}; accepted schemes: {accepted}")
    try:
        _ = parts.port
    except ValueError as exc:
        raise UnsafePathError(f"source URL {url!r} has an invalid port") from exc
    if parts.username is not None or parts.password is not None:
        raise UnsafePathError(
            f"source URL {_redacted(parts)!r} carries credentials; the source is recorded in the served tree"
        )
    if not parts.hostname:
        raise UnsafePathError(f"source URL {url!r} has no host")
    if "?" in url or "#" in url:
        raise UnsafePathError(f"source URL {url!r} has a query string or fragment")
    return url.rstrip("/")


def validate_repo_path(path: str, *, origin: str) -> str:
    """Return ``path`` when it is a relative repository path such as ``sdk/all``.

    ``origin`` names who supplied it (``"operator"`` or ``"source index"``), so a
    refusal says whether to fix the command line or distrust the source.
    """
    if not path:
        raise UnsafePathError(f"repository path {path!r} from {origin} is empty")
    if "\x00" in path or "\\" in path:
        raise UnsafePathError(f"repository path {path!r} from {origin} contains a NUL or backslash")
    if path.startswith("/"):
        raise UnsafePathError(f"repository path {path!r} from {origin} is absolute")
    segments = path.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise UnsafePathError(f"repository path {path!r} from {origin} has an empty, '.' or '..' segment")
    if segments[0] in _RESERVED_ROOTS:
        raise UnsafePathError(f"repository path {path!r} from {origin} starts with reserved {segments[0]!r}")
    return path


def confine_metadata_href(href: str) -> str:
    """Return the file name of a ``repodata/<name>`` metadata href, or raise."""
    if "\x00" in href or "\\" in href:
        raise UnsafePathError(f"metadata href {href!r} contains a NUL or backslash")
    head, sep, name = href.partition("/")
    if head != _REPODATA or not sep:
        raise UnsafePathError(f"metadata href {href!r} is not of the form repodata/<name>")
    if name in ("", ".", "..") or "/" in name:
        raise UnsafePathError(f"metadata href {href!r} does not name one file directly under repodata/")
    if name in _PUBLISH_TARGETS or name.endswith(_PARTIAL_SUFFIX):
        raise UnsafePathError(f"metadata href {href!r} names a file the mirror publishes itself")
    return name


def confine_package_href(href: str, *, repo: str, base: str | None = None) -> PurePosixPath:
    """Return a package's destination relative to the channel directory, or raise.

    A package may land in the channel's shared pool (``_pkgs/...``) or inside its
    own repository, never in another repository, a snapshot tree, or any
    ``repodata/`` - the last is where the mirror's own index is published.
    """
    if base is not None:
        raise UnsafePathError(f"package location {href!r} carries xml:base {base!r}; only relative hrefs are mirrored")
    if not href or "\x00" in href or "\\" in href:
        raise UnsafePathError(f"package location {href!r} is empty or contains a NUL or backslash")
    if href.startswith("/"):
        raise UnsafePathError(f"package location {href!r} is absolute")
    if urlsplit(href).scheme:
        raise UnsafePathError(f"package location {href!r} carries a URL scheme")
    if "" in href.split("/"):
        raise UnsafePathError(f"package location {href!r} has an empty path segment")
    if not href.endswith(_PACKAGE_SUFFIX):
        raise UnsafePathError(f"package location {href!r} does not name an {_PACKAGE_SUFFIX} file")
    repo_parts = PurePosixPath(validate_repo_path(repo, origin="package listing")).parts

    normalized = posixpath.normpath(f"{repo}/{href}")
    if normalized == ".." or normalized.startswith("../"):
        raise UnsafePathError(f"package location {href!r} in {repo!r} escapes the channel")
    parts = PurePosixPath(normalized).parts
    if _REPODATA in parts:
        raise UnsafePathError(f"package location {href!r} in {repo!r} lands in a repodata/ directory")
    in_pool = len(parts) > 1 and parts[0] == _POOL
    in_repo = (
        len(parts) > len(repo_parts) and parts[: len(repo_parts)] == repo_parts and parts[len(repo_parts)] != _SNAPSHOTS
    )
    if not (in_pool or in_repo):
        raise UnsafePathError(f"package location {href!r} in {repo!r} resolves to {normalized!r}, outside {repo!r}")
    return PurePosixPath(normalized)


def resolve_destination(channel_dir: Path, relative: PurePosixPath) -> Path:
    """Join ``relative`` onto ``channel_dir`` and return the resolved path.

    Symlinks are followed (non-strict, so the file need not exist yet) and the
    result must stay inside the resolved channel directory. The channel itself
    may sit behind a symlink - a feed root on another disk is common - which is
    why both sides are resolved before comparing.
    """
    if relative.is_absolute():
        raise UnsafePathError(f"destination {str(relative)!r} is absolute")
    root = channel_dir.resolve()
    dest = (channel_dir / relative).resolve()
    if dest == root or not dest.is_relative_to(root):
        raise UnsafePathError(f"destination {str(relative)!r} resolves to {str(dest)!r}, outside {str(root)!r}")
    return dest
