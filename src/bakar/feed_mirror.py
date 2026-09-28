"""Copy repositories of a published package feed into the local feed.

The library half of ``bakar feed mirror``. Everything a mirror writes is named
by someone else - the operator names the source and the repositories, and the
source's own metadata names every file beneath them - so validation and write
confinement (:mod:`bakar.feed_mirror_paths`) run over every name before a
single request is made, and rpm-md parsing and hashing
(:mod:`bakar.feed_mirror_meta`) turn what the source declares into a plan this
module can verify against.

Four phases, run across every selected repository before moving to the next:

1. **metadata** - fetch and verify every repository's ``repomd.xml`` and the
   metadata files it names, then parse the primary and confine every package
   location it lists. Any violation anywhere fails the whole run here, before
   a single package has been requested.
2. **packages** - download each planned package, verify it against the
   checksum its listing declared, and place it atomically.
3. **publish** - once every package of every selected repository has
   verified, write each repository's ``repomd.xml`` with the exact source
   bytes, atomically.

This module downloads single-threaded with no retries, resume, disk
preflight, target-index selection, ownership guard or signature handling -
those are later tasks. It never requests or writes ``snapshots-latest.json``,
anything under ``snapshots/``, or ``targets.json``.
"""

from __future__ import annotations

import io
import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import bakar
from bakar import feed as feed_mod
from bakar import feed_mirror_meta as meta
from bakar import feed_mirror_paths as paths

if TYPE_CHECKING:
    from pathlib import Path, PurePosixPath

_TIMEOUT_S = 60.0
_USER_AGENT = f"bakar/{bakar.__version__}"
_PART_SUFFIX = ".part"
_REPODATA = "repodata"
_REPOMD = "repomd.xml"


class MirrorError(Exception):
    """A mirror run cannot proceed; the message is the one-line reason."""


@dataclass(frozen=True, kw_only=True)
class MirrorRequest:
    """What to mirror, and where to put it."""

    source_url: str
    release: str
    channel: str
    feed_root: Path
    targets: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class RepoOutcome:
    """What happened to one mirrored repository."""

    repo: str
    packages: int
    downloaded: int
    reused: int
    bytes_downloaded: int
    signed: bool


@dataclass(frozen=True, kw_only=True)
class MirrorResult:
    """What a mirror run did, across every selected repository."""

    feed_root: Path
    channel_dir: Path
    source_channel: str
    repos: tuple[RepoOutcome, ...]
    unpublished: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class _PlannedPackage:
    """One package this run intends to download, already confined.

    ``relative`` is the destination's path relative to the CHANNEL directory,
    already normalized by :func:`bakar.feed_mirror_paths.confine_package_href` -
    used to build the request URL instead of the source's own possibly-dotted
    href, so a package shared across repositories via ``../../_pkgs/...`` is
    requested at one canonical path rather than once per repository's literal
    relative spelling of it.
    """

    relative: PurePosixPath
    dest: Path
    checksum_type: str
    checksum: str
    size: int


@dataclass(kw_only=True)
class _RepoPlan:
    """Everything phase 1 learned about one repository, ready for phase 3/4."""

    repo: str
    local_repo: Path
    repomd_body: bytes
    packages: list[_PlannedPackage] = field(default_factory=list)


def _fetch(url: str) -> bytes:
    """Return the body at ``url``, or raise :class:`MirrorError` naming it."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MirrorError(f"cannot fetch {url}: {exc}") from exc


def _download_to(url: str, dest: Path) -> Path:
    """Stream ``url`` into ``dest``'s ``.part`` sibling and return its path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + _PART_SUFFIX)
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with (
            urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response,
            part.open("wb") as fh,
        ):
            shutil.copyfileobj(response, fh)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        part.unlink(missing_ok=True)
        raise MirrorError(f"cannot fetch {url}: {exc}") from exc
    return part


def _fetch_verified(url: str, dest: Path, *, checksum_type: str, checksum: str, what: str) -> int:
    """Download ``url`` to ``dest``, verify it, and replace it into place.

    A mismatch deletes the ``.part`` file and raises before ``dest`` is
    touched. Returns the verified size in bytes.
    """
    part = _download_to(url, dest)
    try:
        digest = meta.file_digest(part, checksum_type)
    except meta.MetadataError as exc:
        part.unlink(missing_ok=True)
        raise MirrorError(f"{what}: {exc}") from exc
    if digest != checksum:
        part.unlink(missing_ok=True)
        raise MirrorError(f"{what}: checksum mismatch (expected {checksum_type}:{checksum}, got {digest})")
    size = part.stat().st_size
    os.replace(part, dest)
    return size


def _plan_repo(repo: str, *, source_channel: str, channel_dir: Path) -> _RepoPlan:
    """Phase 1 for one repository: fetch, verify and parse its metadata.

    Every package location it lists is confined and resolved before this
    returns, so a violating listing fails here - before any package of any
    repository has been requested.
    """
    repo_url = f"{source_channel}/{repo}"
    local_repo = channel_dir / repo
    repomd_url = f"{repo_url}/{_REPODATA}/{_REPOMD}"
    try:
        body = _fetch(repomd_url)
    except MirrorError as exc:
        raise MirrorError(f"repository {repo!r}: {exc}") from exc

    try:
        index = meta.parse_repomd(body)
    except meta.MetadataError as exc:
        raise MirrorError(f"repository {repo!r}: {exc}") from exc

    primary_bytes: bytes | None = None
    primary_name: str | None = None
    primary_open_size: int | None = None
    for entry in index.files:
        try:
            name = paths.confine_metadata_href(entry.href)
        except paths.UnsafePathError as exc:
            raise MirrorError(f"repository {repo!r}: {exc}") from exc
        dest = local_repo / _REPODATA / name
        url = f"{repo_url}/{_REPODATA}/{name}"
        _fetch_verified(url, dest, checksum_type=entry.checksum_type, checksum=entry.checksum, what=url)
        if entry.type == "primary":
            primary_bytes = dest.read_bytes()
            primary_name = name
            primary_open_size = entry.open_size

    if primary_bytes is None or primary_name is None:
        raise MirrorError(f"repository {repo!r}: repomd.xml names no primary metadata file")

    try:
        entries = list(meta.iter_primary(io.BytesIO(primary_bytes), href=primary_name, open_size=primary_open_size))
    except meta.MetadataError as exc:
        raise MirrorError(f"repository {repo!r}: {exc}") from exc

    plan = _RepoPlan(repo=repo, local_repo=local_repo, repomd_body=body)
    for package in entries:
        try:
            relative = paths.confine_package_href(package.href, repo=repo, base=package.base)
            dest = paths.resolve_destination(channel_dir, relative)
        except paths.UnsafePathError as exc:
            raise MirrorError(f"repository {repo!r}: {exc}") from exc
        try:
            # Validated here, not only at download time: an unsupported checksum
            # algorithm is a fact about the listing, not the transfer, so it
            # belongs with confinement in the "before any package request" phase.
            meta.new_hasher(package.checksum_type)
        except meta.MetadataError as exc:
            raise MirrorError(f"repository {repo!r}: package {package.href!r}: {exc}") from exc
        plan.packages.append(
            _PlannedPackage(
                relative=relative,
                dest=dest,
                checksum_type=package.checksum_type,
                checksum=package.checksum,
                size=package.size,
            )
        )
    return plan


def _download_package(source_channel: str, package: _PlannedPackage) -> int:
    """Phase 3 for one package: download, verify, and place it. Returns its size.

    Requested at its channel-relative path rather than the source's own
    (possibly dotted, e.g. ``../../_pkgs/...``) href, so it resolves to one
    canonical URL regardless of which repository listed it.
    """
    url = f"{source_channel}/{package.relative}"
    what = str(package.relative)
    return _fetch_verified(url, package.dest, checksum_type=package.checksum_type, checksum=package.checksum, what=what)


def _publish(plan: _RepoPlan) -> None:
    """Phase 4 for one repository: write its ``repomd.xml`` atomically, last."""
    dest = plan.local_repo / _REPODATA / _REPOMD
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + _PART_SUFFIX)
    part.write_bytes(plan.repomd_body)
    os.replace(part, dest)


def mirror(request: MirrorRequest) -> MirrorResult:
    """Copy every selected repository of a published feed into the local feed."""
    source_url = paths.validate_source_url(request.source_url)

    if request.targets:
        raise MirrorError("target selection is not yet supported; select repositories with --repo")

    selected: list[str] = []
    for repo in request.repos:
        validated = paths.validate_repo_path(repo, origin="operator")
        if validated not in selected:
            selected.append(validated)
    if not selected:
        raise MirrorError("no repository selected")

    channel_dir = feed_mod.channel_root(request.feed_root, release=request.release, channel=request.channel)
    channel_dir.mkdir(parents=True, exist_ok=True)
    source_channel = f"{source_url}/{request.release}/{request.channel}"

    # Phase 1: every repository's metadata, fully validated, before any package
    # of any repository is requested.
    plans = [_plan_repo(repo, source_channel=source_channel, channel_dir=channel_dir) for repo in selected]

    # Phase 3: every planned package, across every repository.
    outcomes: list[RepoOutcome] = []
    for plan in plans:
        downloaded_bytes = 0
        for package in plan.packages:
            downloaded_bytes += _download_package(source_channel, package)
        outcomes.append(
            RepoOutcome(
                repo=plan.repo,
                packages=len(plan.packages),
                downloaded=len(plan.packages),
                reused=0,
                bytes_downloaded=downloaded_bytes,
                signed=False,
            )
        )

    # Phase 4: publish only after every package of every repository verified.
    for plan in plans:
        _publish(plan)

    return MirrorResult(
        feed_root=request.feed_root,
        channel_dir=channel_dir,
        source_channel=source_channel,
        repos=tuple(outcomes),
    )
