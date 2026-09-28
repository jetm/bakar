"""Copy repositories of a published package feed into the local feed.

The library half of ``bakar feed mirror``. Everything a mirror writes is named
by someone else - the operator names the source and the repositories, and the
source's own metadata names every file beneath them - so validation and write
confinement (:mod:`bakar.feed_mirror_paths`) run over every name before a
single request is made, and rpm-md parsing and hashing
(:mod:`bakar.feed_mirror_meta`) turn what the source declares into a plan this
module can verify against.

Four phases, run across every selected repository before moving to the next:

1. **metadata** - fetch and verify every repository's ``repomd.xml``, its
   detached signature (if the source publishes one), and the metadata files
   ``repomd.xml`` names, then parse the primary and confine every package
   location it lists. Any violation anywhere fails the whole run here, before
   a single package has been requested - including a signature fetch failure,
   so phase 3 never performs network I/O.
2. **packages** - download each planned package, verify it against the
   checksum its listing declared, and place it atomically. A destination whose
   existing bytes already hash to the declared checksum is reused rather than
   re-fetched (resume). Before any package request, every unique destination
   this run needs is sized against ``shutil.disk_usage`` (disk preflight), and
   every listing is checked for a destination two repositories name with
   conflicting checksums, or that lands inside a sibling selected repository's
   own tree. Downloads run concurrently across :data:`MIRROR_WORKERS` threads,
   de-duplicated by destination so a package shared by several repositories is
   fetched once; each download is capped at its listing's declared size, never
   trusting the transfer to stop there on its own.
3. **publish** - once every package of every selected repository has
   verified, write that repository's provenance marker
   (``.bakar-mirror.json``), then the signature phase 1 already fetched, then
   its ``repomd.xml`` with the exact source bytes - each atomically, in that
   order, and confined against the channel directory the same way a package
   destination is. This phase makes no network request.

Before a repository's metadata is fetched, an ownership guard refuses to
mirror over a repository this run did not create: a local ``repomd.xml`` or
snapshot pointer with no marker, or a marker naming a different source,
fails the run before that repository's first request. Every request this run
makes - ``repomd.xml``, its signature, each metadata file, ``targets.json``,
and every package - retries up to 3 attempts with a short exponential backoff
on a connection error, a timeout, an HTTP 5xx, or (for content this module
verifies) a checksum mismatch or a transfer exceeding its declared size; an
HTTP 4xx fails immediately, naming the URL. No request follows a redirect -
the source is a fixed, named feed root, and a redirect target could otherwise
carry a scheme (``file://``) the source URL's own validation never sees. This
module never requests or writes ``snapshots-latest.json`` or anything under
``snapshots/`` itself (though a leftover one from elsewhere is what the
ownership guard checks for). A ``--target`` selection reads the source's own
``targets.json`` to resolve a machine name into the repository paths it
locks, but this module never writes one -
:func:`bakar.feed_index.write_targets_index` owns that.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import bakar
from bakar import feed as feed_mod
from bakar import feed_mirror_meta as meta
from bakar import feed_mirror_paths as paths

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import BinaryIO

_TIMEOUT_S = 60.0
_USER_AGENT = f"bakar/{bakar.__version__}"
_PART_SUFFIX = ".part"
_READ_CHUNK = 64 * 1024
#: Sanity ceiling for a control file with no self-declared size (repomd.xml,
#: targets.json, a detached signature) - never an expected size, a ceiling
#: against a hostile response. The largest live control file measured on the
#: published feed is a few KiB.
_CONTROL_FILE_SIZE_CAP = 16 * 1024 * 1024
_REPODATA = "repodata"
_REPOMD = "repomd.xml"
_TARGETS = "targets.json"
_SIGNATURE = "repomd.xml.asc"
_POINTER = "snapshots-latest.json"
_MARKER = ".bakar-mirror.json"

#: Downloads run through a thread pool this wide, de-duplicated by destination
#: path across repositories. Not a flag - concurrency here is an
#: implementation detail of one mirror run, not something an operator tunes.
MIRROR_WORKERS = 8

#: Sleep between attempts 1->2 and 2->3, in seconds. A 4th attempt never
#: happens: 3 attempts total, 2 sleeps between them.
_RETRY_BACKOFF_S = (0.5, 1.0)


class MirrorError(Exception):
    """A mirror run cannot proceed; the message is the one-line reason."""


class _TransientError(MirrorError):
    """A retryable failure: connection error, timeout, HTTP 5xx, or (for
    content this module verifies) a checksum mismatch. Never raised past
    :func:`_retrying` - callers only ever see the plain :class:`MirrorError`
    it re-raises once retries are exhausted.
    """


class _SizeExceededError(Exception):
    """Internal: a bounded copy exceeded its limit; :func:`_download_to` alone
    catches this and converts it into a :class:`_TransientError` naming the
    URL - nothing outside this module ever sees it.
    """


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every HTTP redirect.

    A mirror's source is a fixed, named feed root - there is no legitimate
    reason for a request against it to be redirected anywhere else. Without
    this, the default opener follows a redirect regardless of the original
    request URL's own validated scheme, and its default file-scheme handler
    would open a redirect target of ``file://...`` without ever passing
    through :func:`bakar.feed_mirror_paths.validate_source_url`.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913
        raise urllib.error.HTTPError(req.full_url, code, f"redirect to {newurl!r} refused", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _copy_bounded(response: BinaryIO, fh: BinaryIO, *, limit: int) -> None:
    """Copy ``response`` into ``fh``, raising :class:`_SizeExceededError` past ``limit``.

    Reads at most one byte past ``limit`` before refusing - enough to prove
    the bound was exceeded without letting an oversized response buffer or
    write further than that.
    """
    total = 0
    while True:
        chunk = response.read(min(_READ_CHUNK, limit - total + 1))
        if not chunk:
            return
        total += len(chunk)
        if total > limit:
            raise _SizeExceededError
        fh.write(chunk)


def _read_bounded(response: BinaryIO, *, limit: int, what: str) -> bytes:
    """Read ``response`` fully, refusing past ``limit``; used where no declared size exists."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(_READ_CHUNK, limit - total + 1))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise MirrorError(f"{what}: response exceeds {limit} bytes")
        chunks.append(chunk)


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
    """What a mirror run did, across every selected repository.

    ``packages_downloaded``/``packages_reused``/``bytes_downloaded`` are
    counted once per UNIQUE destination, unlike each :class:`RepoOutcome`'s
    own fields - a package shared by several repositories (a common pooled
    layout) is one download, and summing the per-repo counts would report it
    once per repository that lists it.
    """

    feed_root: Path
    channel_dir: Path
    source_channel: str
    repos: tuple[RepoOutcome, ...]
    packages_downloaded: int
    packages_reused: int
    bytes_downloaded: int
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
    """Everything phase 1 learned about one repository, ready for phase 2/3.

    ``signature_body`` is fetched here, in phase 1, alongside every other
    metadata request - phase 3 only writes it, so a signature fetch failure
    fails the whole run before any repository's marker or repomd.xml is
    written, rather than partway through the write phase.
    """

    repo: str
    local_repo: Path
    repomd_body: bytes
    revision: str
    signature_body: bytes | None = None
    packages: list[_PlannedPackage] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class _PackageResult:
    """What happened to one unique destination during the download phase."""

    downloaded: bool
    size: int


def _retrying[T](func: Callable[[], T], *, sleep: Callable[[float], None] = time.sleep) -> T:
    """Call ``func``, retrying up to 3 attempts total on :class:`_TransientError`.

    A permanent failure (anything else, including a plain :class:`MirrorError`
    raised for an HTTP 4xx) propagates on the first attempt. A transient
    failure that is still failing after the third attempt propagates as the
    :class:`_TransientError` it always was - which callers see only as its
    superclass :class:`MirrorError`, since nothing outside this function is
    meant to distinguish the two.
    """
    attempts = len(_RETRY_BACKOFF_S) + 1
    for attempt in range(attempts):
        try:
            return func()
        except _TransientError:
            if attempt == attempts - 1:
                raise
            sleep(_RETRY_BACKOFF_S[attempt])
    raise AssertionError("unreachable")  # pragma: no cover


def _fetch(url: str, *, allow_missing: bool = False) -> bytes | None:
    """Return the body at ``url``, or raise naming it.

    When ``allow_missing`` is set, a 404 response returns ``None`` instead of
    raising, so a caller can distinguish "not published" from every other fetch
    failure. An HTTP 5xx, or a connection error or timeout, raises
    :class:`_TransientError`; anything else (including any other HTTP status)
    raises the plain :class:`MirrorError` a caller should not retry. The body
    is capped at :data:`_CONTROL_FILE_SIZE_CAP` - this fetches only small
    control files (``repomd.xml``, ``targets.json``, a detached signature)
    with no self-declared size to bound the transfer against instead.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with _OPENER.open(request, timeout=_TIMEOUT_S) as response:
            return _read_bounded(response, limit=_CONTROL_FILE_SIZE_CAP, what=url)
    except urllib.error.HTTPError as exc:
        if allow_missing and exc.code == 404:
            return None
        if exc.code >= 500:
            raise _TransientError(f"cannot fetch {url}: {exc}") from exc
        raise MirrorError(f"cannot fetch {url}: {exc}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _TransientError(f"cannot fetch {url}: {exc}") from exc


def _fetch_targets(source_channel: str) -> dict[str, list[str]]:
    """Fetch and validate the source's ``targets.json``, or raise naming it.

    Returns the raw target -> repository-path-list mapping, unvalidated beyond
    its shape - each listed path still has to pass
    :func:`bakar.feed_mirror_paths.validate_repo_path` before it is trusted.
    """
    url = f"{source_channel}/{_TARGETS}"
    body = _retrying(lambda: _fetch(url, allow_missing=True))
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


def _download_to(url: str, dest: Path, *, size: int | None = None) -> Path:
    """Stream ``url`` into ``dest``'s ``.part`` sibling and return its path.

    Raises :class:`_TransientError` for a connection error, a timeout, an HTTP
    5xx, or (when ``size`` is given) a transfer that exceeds it; anything else
    raises the plain :class:`MirrorError` a caller should not retry. ``size``
    is never trusted implicitly - the transfer is capped at it rather than
    left to stop on its own, since a compromised or malfunctioning source
    declaring a small size in its listing could otherwise still send an
    unbounded body.
    """
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MirrorError(f"cannot create directory {dest.parent}: {exc}") from exc
    part = dest.with_name(dest.name + _PART_SUFFIX)
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with (
            _OPENER.open(request, timeout=_TIMEOUT_S) as response,
            part.open("wb") as fh,
        ):
            if size is None:
                shutil.copyfileobj(response, fh)
            else:
                _copy_bounded(response, fh, limit=size)
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        if exc.code >= 500:
            raise _TransientError(f"cannot fetch {url}: {exc}") from exc
        raise MirrorError(f"cannot fetch {url}: {exc}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        part.unlink(missing_ok=True)
        raise _TransientError(f"cannot fetch {url}: {exc}") from exc
    except _SizeExceededError:
        part.unlink(missing_ok=True)
        raise _TransientError(f"{url}: transfer exceeds its declared size of {size} bytes") from None
    return part


def _fetch_verified(  # noqa: PLR0913
    url: str, dest: Path, *, checksum_type: str, checksum: str, size: int | None, what: str
) -> int:
    """Download ``url`` to ``dest``, verify it, and replace it into place.

    A mismatch deletes the ``.part`` file and raises :class:`_TransientError` before
    ``dest`` is touched, so the caller's retry wrapper re-fetches it. Returns
    the verified size in bytes. A filesystem failure while hashing the fresh
    download is raised as :class:`MirrorError`, not left as a bare
    :class:`OSError` - this runs inside :func:`_download_all`'s executor
    threads too, where an unwrapped exception would bypass cancel-on-first-failure.
    """
    part = _download_to(url, dest, size=size)
    try:
        digest = meta.file_digest(part, checksum_type)
    except (meta.MetadataError, OSError) as exc:
        part.unlink(missing_ok=True)
        raise MirrorError(f"{what}: {exc}") from exc
    if digest != checksum:
        part.unlink(missing_ok=True)
        raise _TransientError(f"{what}: checksum mismatch (expected {checksum_type}:{checksum}, got {digest})")
    size = part.stat().st_size
    os.replace(part, dest)
    return size


def _check_ownership(repo: str, *, source_channel: str, channel_dir: Path) -> None:
    """Refuse to mirror over a repository this run did not create.

    Evaluated for every selected repository before its metadata is fetched. A
    repository with neither a local ``repomd.xml`` nor a snapshot pointer is
    fresh (or was interrupted before a previous publish) and passes
    unconditionally. Otherwise the repository's own ``.bakar-mirror.json``
    marker must name this run's source, or the run is refused before any
    request for that repository.
    """
    local_repo = channel_dir / repo
    repomd_path = local_repo / _REPODATA / _REPOMD
    pointer_path = local_repo / _POINTER
    has_repomd = repomd_path.is_file()
    has_pointer = pointer_path.is_file()
    if not has_repomd and not has_pointer:
        return

    marker_path = local_repo / _MARKER
    marker: dict | None = None
    if marker_path.is_file():
        try:
            marker = json.loads(marker_path.read_text())
        except ValueError as exc:
            raise MirrorError(f"repository {repo!r}: cannot parse {marker_path}: {exc}") from exc

    expected_source = f"{source_channel}/{repo}"
    if marker is None:
        if has_pointer:
            raise MirrorError(
                f"repository {repo!r} has a local snapshot pointer {pointer_path} with no mirror marker; "
                "clients would resolve it instead of the mirrored head"
            )
        raise MirrorError(
            f"repository {repo!r} has local feed metadata this mirror did not create; "
            f"choose another feed root (-w or build.feed_dir) or remove {local_repo}"
        )

    marker_source = marker.get("source") if isinstance(marker, dict) else None
    if marker_source != expected_source:
        raise MirrorError(
            f"repository {repo!r} was mirrored from {marker_source!r}, not this run's source {expected_source!r}"
        )


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
        body = _retrying(lambda: _fetch(repomd_url, allow_missing=allow_missing))
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
            dest = paths.resolve_destination(channel_dir, PurePosixPath(repo) / _REPODATA / name)
        except paths.UnsafePathError as exc:
            raise MirrorError(f"repository {repo!r}: {exc}") from exc
        url = f"{repo_url}/{_REPODATA}/{name}"
        _retrying(
            lambda url=url, dest=dest, entry=entry: _fetch_verified(
                url, dest, checksum_type=entry.checksum_type, checksum=entry.checksum, size=entry.size, what=url
            )
        )
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

    # Fetched here, in phase 1, rather than at publish time: a signature
    # fetch failure must fail the whole run before any repository's marker or
    # repomd.xml is written, the same as every other phase-1 violation - not
    # partway through phase 3's writes.
    asc_url = f"{repo_url}/{_REPODATA}/{_SIGNATURE}"
    signature_body = _retrying(lambda: _fetch(asc_url, allow_missing=True))

    plan = _RepoPlan(
        repo=repo, local_repo=local_repo, repomd_body=body, revision=index.revision, signature_body=signature_body
    )
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


def _check_no_conflicting_checksums(plans: list[_RepoPlan]) -> None:
    """Refuse before any package request when two listings disagree.

    Two repository listings may legitimately name the same destination (a
    shared pool package) - but only when they declare the same checksum type
    and value for it. Runs before de-duplication, since de-duplication is what
    would otherwise silently keep only the first listing's declared checksum
    and never check the second.
    """
    seen: dict[Path, _PlannedPackage] = {}
    for plan in plans:
        for package in plan.packages:
            existing = seen.get(package.dest)
            if existing is None:
                seen[package.dest] = package
                continue
            if existing.checksum_type != package.checksum_type or existing.checksum != package.checksum:
                raise MirrorError(
                    f"{package.dest}: conflicting checksums declared for the same destination "
                    f"({existing.checksum_type}:{existing.checksum} vs {package.checksum_type}:{package.checksum})"
                )


def _check_no_cross_repo_landing(selected: list[str], plans: list[_RepoPlan]) -> None:
    """Refuse before any package request when one repository's listing lands inside a sibling's tree.

    :func:`bakar.feed_mirror_paths.confine_package_href` only confirms a
    package stays inside its OWN repository (or the shared pool) - it has no
    visibility into what else this run selected. A repository publishing a
    non-pooled package under a subdirectory whose name matches another
    SELECTED repository's own path (``sdk`` listing a package at
    ``all/pkg.rpm`` while ``sdk/all`` is also selected) would otherwise land
    inside that sibling's directory without the sibling's own listing or
    ownership guard ever knowing.
    """
    selected_parts = [(other, PurePosixPath(other).parts) for other in selected]
    for plan in plans:
        for package in plan.packages:
            parts = package.relative.parts
            for other, other_parts in selected_parts:
                if other == plan.repo:
                    continue
                if len(parts) > len(other_parts) and parts[: len(other_parts)] == other_parts:
                    raise MirrorError(
                        f"repository {plan.repo!r} package at {package.relative} lands inside "
                        f"sibling selected repository {other!r}"
                    )


def _dedupe_packages(plans: list[_RepoPlan]) -> list[_PlannedPackage]:
    """Return one :class:`_PlannedPackage` per unique destination, first-seen."""
    seen: dict[Path, _PlannedPackage] = {}
    for plan in plans:
        for package in plan.packages:
            seen.setdefault(package.dest, package)
    return list(seen.values())


def _check_disk_space(channel_dir: Path, packages: list[_PlannedPackage]) -> None:
    """Refuse before any package request when free space can't cover this run.

    Sums the declared size of every unique destination that is missing or
    whose on-disk size differs from what its listing declares - a destination
    that already exists at the right size never counts, even when its content
    turns out to be wrong (resume's checksum re-fetch does not add net bytes
    in the common case). Free space is read at the nearest existing ancestor
    of the channel directory, since the channel directory itself may not exist
    yet on a first run.
    """
    required = 0
    for package in packages:
        try:
            on_disk_size = package.dest.stat().st_size
        except OSError:
            on_disk_size = None
        if on_disk_size is None or on_disk_size != package.size:
            required += package.size
    if required == 0:
        return
    ancestor = channel_dir
    while not ancestor.exists():
        ancestor = ancestor.parent
    free = shutil.disk_usage(ancestor).free
    if free < required:
        required_gib = required / 2**30
        free_gib = free / 2**30
        raise MirrorError(f"not enough disk space: need {required_gib:.2f} GiB, have {free_gib:.2f} GiB free")


def _resume_or_download(source_channel: str, package: _PlannedPackage) -> _PackageResult:
    """Reuse ``package.dest`` if its bytes already match, else (re-)download it.

    Requested at its channel-relative path rather than the source's own
    (possibly dotted, e.g. ``../../_pkgs/...``) href, so it resolves to one
    canonical URL regardless of which repository listed it.

    A filesystem failure while checking or re-hashing an existing destination
    is raised as :class:`MirrorError`, not left as a bare :class:`OSError` -
    :func:`_download_all` cancels the whole phase only on the former, so an
    unwrapped exception here would silently let every other in-flight
    download keep running past a failure this run should have stopped on.
    """
    dest = package.dest
    try:
        resumable = dest.is_file() and dest.stat().st_size == package.size
    except OSError as exc:
        raise MirrorError(f"{package.relative}: cannot stat {dest}: {exc}") from exc
    if resumable:
        try:
            digest = meta.file_digest(dest, package.checksum_type)
        except OSError as exc:
            raise MirrorError(f"{package.relative}: cannot read {dest} to verify checksum: {exc}") from exc
        if digest == package.checksum:
            return _PackageResult(downloaded=False, size=package.size)
    url = f"{source_channel}/{package.relative}"
    what = str(package.relative)
    size = _retrying(
        lambda: _fetch_verified(
            url, dest, checksum_type=package.checksum_type, checksum=package.checksum, size=package.size, what=what
        )
    )
    return _PackageResult(downloaded=True, size=size)


def _download_all(source_channel: str, packages: list[_PlannedPackage]) -> dict[Path, _PackageResult]:
    """Phase 2: resume or download every unique destination, concurrently.

    On the first permanent failure, no new package is scheduled and the
    failure propagates naming the file; packages already in flight are not
    interrupted, but their results are discarded.
    """
    results: dict[Path, _PackageResult] = {}
    with ThreadPoolExecutor(max_workers=MIRROR_WORKERS) as executor:
        futures = {executor.submit(_resume_or_download, source_channel, package): package for package in packages}
        try:
            for future in as_completed(futures):
                package = futures[future]
                results[package.dest] = future.result()
        except MirrorError:
            executor.shutdown(wait=False, cancel_futures=True)
            raise
    return results


def _write_atomic(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + _PART_SUFFIX)
    part.write_bytes(data)
    os.replace(part, dest)


def _publish_destination(channel_dir: Path, repo: str, *relative: str) -> Path:
    """Resolve one of phase 3's own destinations, confined the same way a package's is."""
    try:
        return paths.resolve_destination(channel_dir, PurePosixPath(repo).joinpath(*relative))
    except paths.UnsafePathError as exc:
        raise MirrorError(f"repository {repo!r}: {exc}") from exc


def _write_marker(plan: _RepoPlan, *, source_channel: str, channel_dir: Path) -> None:
    """Write ``.bakar-mirror.json`` beside ``repodata/``, naming this run's source."""
    payload = {
        "source": f"{source_channel}/{plan.repo}",
        "revision": plan.revision,
        "mirrored": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    marker_path = _publish_destination(channel_dir, plan.repo, _MARKER)
    _write_atomic(marker_path, (json.dumps(payload, sort_keys=True) + "\n").encode())


def _publish(plan: _RepoPlan, *, source_channel: str, channel_dir: Path) -> bool:
    """Phase 3 for one repository: marker, signature, then ``repomd.xml`` - last.

    Returns whether the source publishes a detached signature for this
    repository. A stale local signature from a source that has since stopped
    signing is removed before the new (unsigned) index is written - the one
    exception to never deleting a local file the mirror did not just
    download, and it is safe only because the ownership guard already
    confirmed this repository's marker names this exact source. Makes no
    network request: ``plan.signature_body`` was already fetched in phase 1,
    so a signature-fetch failure never lands mid-way through this phase's
    writes.
    """
    _write_marker(plan, source_channel=source_channel, channel_dir=channel_dir)

    local_asc = _publish_destination(channel_dir, plan.repo, _REPODATA, _SIGNATURE)
    signed = plan.signature_body is not None
    if signed:
        _write_atomic(local_asc, plan.signature_body)
    else:
        local_asc.unlink(missing_ok=True)

    dest = _publish_destination(channel_dir, plan.repo, _REPODATA, _REPOMD)
    _write_atomic(dest, plan.repomd_body)
    return signed


def mirror(request: MirrorRequest) -> MirrorResult:
    """Copy every selected repository of a published feed into the local feed."""
    try:
        source_url = paths.validate_source_url(request.source_url)
    except paths.UnsafePathError as exc:
        raise MirrorError(str(exc)) from exc
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
        try:
            validated = paths.validate_repo_path(repo, origin="operator")
        except paths.UnsafePathError as exc:
            raise MirrorError(str(exc)) from exc
        origins[validated] = "operator"

    selected = list(origins.keys())
    if not selected:
        raise MirrorError("no repository selected")

    channel_dir = feed_mod.channel_root(request.feed_root, release=request.release, channel=request.channel)
    channel_dir.mkdir(parents=True, exist_ok=True)

    # Ownership guard: for every selected repository, before its metadata is
    # requested, refuse to mirror over local feed content this run did not
    # create. Cheap and purely local, so it runs ahead of every repository's
    # first request rather than only ahead of the first repository's.
    for repo in selected:
        _check_ownership(repo, source_channel=source_channel, channel_dir=channel_dir)

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

    # Before any package request: two listings naming the same destination
    # with conflicting checksums fail the run, one repository's listing
    # landing inside a sibling selected repository's own tree fails the run,
    # then every unique destination this run needs is sized against the free
    # space at the channel directory.
    _check_no_conflicting_checksums(plans)
    _check_no_cross_repo_landing(selected, plans)
    unique_packages = _dedupe_packages(plans)
    _check_disk_space(channel_dir, unique_packages)

    # Phase 2: every unique package, across every repository, resumed or
    # downloaded concurrently.
    results = _download_all(source_channel, unique_packages)

    # Phase 3: publish only after every package of every repository verified -
    # marker, then signature, then repomd.xml, per repository.
    outcomes: list[RepoOutcome] = []
    for plan in plans:
        downloaded = 0
        reused = 0
        downloaded_bytes = 0
        for package in plan.packages:
            result = results[package.dest]
            if result.downloaded:
                downloaded += 1
                downloaded_bytes += result.size
            else:
                reused += 1
        signed = _publish(plan, source_channel=source_channel, channel_dir=channel_dir)
        outcomes.append(
            RepoOutcome(
                repo=plan.repo,
                packages=len(plan.packages),
                downloaded=downloaded,
                reused=reused,
                bytes_downloaded=downloaded_bytes,
                signed=signed,
            )
        )

    return MirrorResult(
        feed_root=request.feed_root,
        channel_dir=channel_dir,
        source_channel=source_channel,
        repos=tuple(outcomes),
        packages_downloaded=sum(1 for r in results.values() if r.downloaded),
        packages_reused=sum(1 for r in results.values() if not r.downloaded),
        bytes_downloaded=sum(r.size for r in results.values() if r.downloaded),
        unpublished=tuple(unpublished),
    )
