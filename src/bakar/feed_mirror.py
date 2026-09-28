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
preflight, ownership guard or signature handling - those are later tasks. It
never requests or writes ``snapshots-latest.json`` or anything under
``snapshots/``. A ``--target`` selection reads the source's own
``targets.json`` to resolve a machine name into the repository paths it
locks, but this module never writes one - :func:`bakar.feed_index.write_targets_index`
owns that.
"""

from __future__ import annotations

import io
import json
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
_TARGETS = "targets.json"


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


def _fetch(url: str, *, allow_missing: bool = False) -> bytes | None:
    """Return the body at ``url``, or raise :class:`MirrorError` naming it.

    When ``allow_missing`` is set, a 404 response returns ``None`` instead of
    raising, so a caller can distinguish "not published" from every other fetch
    failure.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if allow_missing and exc.code == 404:
            return None
        raise MirrorError(f"cannot fetch {url}: {exc}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MirrorError(f"cannot fetch {url}: {exc}") from exc


def _fetch_targets(source_channel: str) -> dict[str, list[str]]:
    """Fetch and validate the source's ``targets.json``, or raise naming it.

    Returns the raw target -> repository-path-list mapping, unvalidated beyond
    its shape - each listed path still has to pass
    :func:`bakar.feed_mirror_paths.validate_repo_path` before it is trusted.
    """
    url = f"{source_channel}/{_TARGETS}"
    body = _fetch(url, allow_missing=True)
    if body is None:
        raise MirrorError(f"no {_TARGETS} at {url}; name repositories with --repo instead")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise MirrorError(f"{url}: not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not all(
        isinstance(name, str) and isinstance(repos, list) and all(isinstance(item, str) for item in repos)
        for name, repos in data.items()
    ):
        raise MirrorError(f"{url}: expected a JSON object mapping target name to a list of repository paths")
    return data


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


def _plan_repo(repo: str, *, source_channel: str, channel_dir: Path, allow_missing: bool = False) -> _RepoPlan | None:
    """Phase 1 for one repository: fetch, verify and parse its metadata.

    Every package location it lists is confined and resolved before this
    returns, so a violating listing fails here - before any package of any
    repository has been requested. When ``allow_missing`` is set and the
    repository's own ``repomd.xml`` 404s, returns ``None`` instead of raising -
    the caller records it as unpublished rather than failing the run.
    """
    repo_url = f"{source_channel}/{repo}"
    local_repo = channel_dir / repo
    repomd_url = f"{repo_url}/{_REPODATA}/{_REPOMD}"
    try:
        body = _fetch(repomd_url, allow_missing=allow_missing)
    except MirrorError as exc:
        raise MirrorError(f"repository {repo!r}: {exc}") from exc
    if body is None:
        return None

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
    source_channel = f"{source_url}/{request.release}/{request.channel}"

    # `origins` is the union of target-derived and explicit repositories, in
    # first-seen order (dict insertion order), and remembers which source named
    # each one - a repository the operator names explicitly must still fail hard
    # on a 404, even when a target the run also selected happens to declare it
    # too, so an explicit --repo always overwrites an index-derived origin.
    origins: dict[str, str] = {}
    if request.targets:
        target_index = _fetch_targets(source_channel)
        for target in request.targets:
            if target not in target_index:
                raise MirrorError(f"target {target!r} not found in {source_channel}/{_TARGETS}")
            for repo in target_index[target]:
                try:
                    validated = paths.validate_repo_path(repo, origin="source index")
                except paths.UnsafePathError as exc:
                    raise MirrorError(str(exc)) from exc
                origins.setdefault(validated, "index")

    for repo in request.repos:
        validated = paths.validate_repo_path(repo, origin="operator")
        origins[validated] = "operator"

    selected = list(origins.keys())
    if not selected:
        raise MirrorError("no repository selected")

    channel_dir = feed_mod.channel_root(request.feed_root, release=request.release, channel=request.channel)
    channel_dir.mkdir(parents=True, exist_ok=True)

    # Phase 1: every repository's metadata, fully validated, before any package
    # of any repository is requested. An index-derived repository whose own
    # repomd.xml 404s is not a failure here - the target map declares what a
    # machine COULD publish, and one never built for this release is a normal
    # outcome - so it is recorded in `unpublished` instead of failing the run.
    plans: list[_RepoPlan] = []
    unpublished: list[str] = []
    for repo in selected:
        plan = _plan_repo(
            repo, source_channel=source_channel, channel_dir=channel_dir, allow_missing=(origins[repo] == "index")
        )
        if plan is None:
            unpublished.append(repo)
            continue
        plans.append(plan)

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
        unpublished=tuple(unpublished),
    )
