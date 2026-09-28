"""A synthetic published package feed, served over loopback HTTP, for mirror tests.

This is a test helper module, not a test module: it holds no test functions.
Import the builder and the context manager directly::

    from tests._feed_upstream import FeedSpec, build_feed, serve_feed

A test that wants the ``feed_server`` fixture declares, at module scope::

    pytest_plugins = ["tests._feed_upstream"]

rather than importing the fixture by name. An imported fixture is an unused
import that the test parameter then shadows (F401 + F811), and the pre-commit
hook runs ``ruff-check --fix``, which deletes that import at commit time and
leaves "fixture not found" behind it.

The layout matches what the production feed publishes and what bakar's own
renderer writes: a content-addressed pool at
``<root>/<release>/<channel>/_pkgs/<aa>/<sha256>.rpm``, and per repository a
``repodata/`` holding gzip primary/filelists/other plus ``repomd.xml``. A
package's ``<location href>`` is relative to the REPOSITORY directory - one
``../`` per repository path segment, then the pool path.

Metadata stays self-consistent: every rewrite of a primary regenerates the
repomd.xml checksums and bumps ``<revision>``. The only inconsistencies are the
ones a test asks for (a corrupt served body, an overridden open-size, a hostile
href, an unsupported checksum type). Serving faults and metadata overrides can be
added and cleared while the server runs, so a test can mirror cleanly first and
then flip a fault for a second run.
"""

from __future__ import annotations

import contextlib
import dataclasses
import gzip
import hashlib
import json
import os
import threading
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

# escape() only encodes text for output; it never parses input, so it has no
# defusedxml equivalent and no XXE surface regardless of what it is given.
# nosemgrep: python.lang.security.use-defused-xml.use-defused-xml
from xml.sax.saxutils import escape

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from pathlib import Path

COMMON_NS = "http://linux.duke.edu/metadata/common"
REPO_NS = "http://linux.duke.edu/metadata/repo"
FILELISTS_NS = "http://linux.duke.edu/metadata/filelists"
OTHER_NS = "http://linux.duke.edu/metadata/other"
RPM_NS = "http://linux.duke.edu/metadata/rpm"

POOL_DIR = "_pkgs"
SNAPSHOTS_DIR = "snapshots"
POINTER_NAME = "snapshots-latest.json"
TARGETS_NAME = "targets.json"
SIGNATURE_NAME = "repomd.xml.asc"

_LOOPBACK = "127.0.0.1"
_BASE_REVISION = 1000
_BASE_TIMESTAMP = 1_700_000_000
_SHUTDOWN_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True, kw_only=True)
class FeedSpec:
    """What :func:`build_feed` publishes.

    ``repos`` maps a repository path (``sdk/all``, ``target/m1``) to how many
    unique packages it lists. ``shared`` names repositories that additionally
    list ONE common payload, stored once in the pool with one checksum.
    ``targets`` writes a channel-root ``targets.json`` verbatim (entries are not
    validated, so a test can publish a hostile one). ``snapshot`` names a
    snapshot id: every repository is also rendered under
    ``snapshots/<id>/<repo>/`` and each ``target/<m>`` repository gets a
    ``snapshots-latest.json`` pointer. ``signed`` repositories carry a detached
    ``repodata/repomd.xml.asc``.
    """

    repos: Mapping[str, int]
    release: str = "2024"
    channel: str = "edge"
    shared: tuple[str, ...] = ()
    targets: Mapping[str, Sequence[str]] | None = None
    snapshot: str | None = None
    signed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Package:
    """One package as a repository's primary lists it.

    ``pool`` is where the payload really lives, relative to the channel
    directory. ``href`` is what the primary declares, relative to the repository
    directory - normally the path to ``pool``, unless a test replaced it.
    """

    name: str
    sha256: str
    size: int
    pool: str
    href: str
    checksum_type: str = "sha256"


@dataclass(frozen=True, slots=True)
class _MetaFile:
    kind: str
    name: str
    compressed: bytes
    body: bytes


_UNSET = object()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _attr(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def _norm(path: str) -> str:
    return "/" + path.lstrip("/")


def _depth(repo: str) -> int:
    return len(repo.split("/"))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _primary_xml(packages: Sequence[Package]) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<metadata xmlns="{COMMON_NS}" xmlns:rpm="{RPM_NS}" packages="{len(packages)}">',
    ]
    for pkg in packages:
        lines += [
            '<package type="rpm">',
            f"  <name>{escape(pkg.name)}</name>",
            "  <arch>noarch</arch>",
            '  <version epoch="0" ver="1.0" rel="r0"/>',
            f'  <checksum type="{_attr(pkg.checksum_type)}" pkgid="YES">{pkg.sha256}</checksum>',
            f"  <summary>{escape(pkg.name)}</summary>",
            f'  <size package="{pkg.size}" installed="{pkg.size}" archive="{pkg.size}"/>',
            f'  <location href="{_attr(pkg.href)}"/>',
            "</package>",
        ]
    lines.append("</metadata>")
    return ("\n".join(lines) + "\n").encode()


def _companion_xml(packages: Sequence[Package], *, root: str, namespace: str) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<{root} xmlns="{namespace}" packages="{len(packages)}">',
    ]
    for pkg in packages:
        lines += [
            f'<package pkgid="{pkg.sha256}" name="{_attr(pkg.name)}" arch="noarch">',
            '  <version epoch="0" ver="1.0" rel="r0"/>',
            "</package>",
        ]
    lines.append(f"</{root}>")
    return ("\n".join(lines) + "\n").encode()


def _repomd_xml(files: Sequence[_MetaFile], *, revision: int, open_size: int | None) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<repomd xmlns="{REPO_NS}" xmlns:rpm="{RPM_NS}">',
        f"  <revision>{revision}</revision>",
    ]
    for meta in files:
        declared_open = open_size if (meta.kind == "primary" and open_size is not None) else len(meta.body)
        lines += [
            f'  <data type="{meta.kind}">',
            f'    <checksum type="sha256">{_sha256(meta.compressed)}</checksum>',
            f'    <open-checksum type="sha256">{_sha256(meta.body)}</open-checksum>',
            f'    <location href="repodata/{meta.name}"/>',
            f"    <timestamp>{_BASE_TIMESTAMP + revision}</timestamp>",
            f"    <size>{len(meta.compressed)}</size>",
            f"    <open-size>{declared_open}</open-size>",
            "  </data>",
        ]
    lines.append("</repomd>")
    return ("\n".join(lines) + "\n").encode()


def _write_repodata(repodata: Path, packages: Sequence[Package], *, revision: int, open_size: int | None) -> bytes:
    """Render one repository's metadata in place and return the repomd.xml bytes.

    Metadata files are written before the index, and the previous generation is
    removed only after the new index names its replacement.
    """
    previous = set(repodata.glob("*.xml.gz")) if repodata.is_dir() else set()
    bodies = (
        ("primary", _primary_xml(packages)),
        ("filelists", _companion_xml(packages, root="filelists", namespace=FILELISTS_NS)),
        ("other", _companion_xml(packages, root="otherdata", namespace=OTHER_NS)),
    )
    files = []
    for kind, body in bodies:
        compressed = gzip.compress(body, mtime=0)
        name = f"{_sha256(compressed)}-{kind}.xml.gz"
        _atomic_write(repodata / name, compressed)
        files.append(_MetaFile(kind, name, compressed, body))
    index = _repomd_xml(files, revision=revision, open_size=open_size)
    _atomic_write(repodata / "repomd.xml", index)
    for stale in previous - {repodata / meta.name for meta in files}:
        stale.unlink()
    return index


class UpstreamFeed:
    """A published feed on disk under ``root``, plus the faults to serve it with.

    Built by :func:`build_feed`. Mutators rewrite files in place, so a running
    server picks them up on the next request.
    """

    def __init__(self, root: Path, spec: FeedSpec) -> None:
        self.root = root
        self.spec = spec
        self._packages: dict[str, list[Package]] = {}
        self._revisions: dict[str, int] = {}
        self._open_size: dict[str, int] = {}
        self._signed: set[str] = set(spec.signed)
        self._serial = 0
        self._lock = threading.Lock()
        self._withheld: set[str] = set()
        self._fail_once: set[str] = set()
        self._fail_always: set[str] = set()
        self._corrupt: dict[str, bytes | None] = {}

        unknown = [repo for repo in (*spec.shared, *spec.signed) if repo not in spec.repos]
        if unknown:
            raise ValueError(f"shared/signed name repositories not in repos: {unknown}")
        for repo, count in spec.repos.items():
            self._packages[repo] = [self._new_package(repo) for _ in range(count)]
            self._revisions[repo] = _BASE_REVISION
        if spec.shared:
            shared = self._write_payload("shared")
            for repo in spec.shared:
                self._packages[repo].append(dataclasses.replace(shared, href=self._href(repo, shared.pool)))
        for repo in spec.repos:
            self._publish(repo)
        if spec.targets is not None:
            self.set_targets(spec.targets)
        if spec.snapshot is not None:
            self._write_snapshot(spec.snapshot)

    # -- layout -----------------------------------------------------------

    @property
    def channel_dir(self) -> Path:
        return self.root / self.spec.release / self.spec.channel

    def url_path(self, relative: str) -> str:
        """Return the served path of a channel-relative file: ``/<release>/<channel>/<relative>``."""
        return f"/{self.spec.release}/{self.spec.channel}/{relative.lstrip('/')}"

    def repomd_path(self, repo: str) -> str:
        return self.url_path(f"{repo}/repodata/repomd.xml")

    def signature_path(self, repo: str) -> str:
        return self.url_path(f"{repo}/repodata/{SIGNATURE_NAME}")

    def package_path(self, package: Package) -> str:
        """Return the served path of a package's real payload in the pool."""
        return self.url_path(package.pool)

    def packages(self, repo: str) -> tuple[Package, ...]:
        return tuple(self._packages[repo])

    def revision(self, repo: str) -> str:
        return str(self._revisions[repo])

    # -- publishing -------------------------------------------------------

    @staticmethod
    def _href(repo: str, pool: str) -> str:
        return "../" * _depth(repo) + pool

    def _write_payload(self, name: str) -> Package:
        self._serial += 1
        payload = os.urandom(200 + (self._serial * 37) % 300)
        digest = _sha256(payload)
        pool = f"{POOL_DIR}/{digest[:2]}/{digest}.rpm"
        _atomic_write(self.channel_dir / pool, payload)
        return Package(name=name, sha256=digest, size=len(payload), pool=pool, href=pool)

    def _new_package(self, repo: str) -> Package:
        slug = repo.replace("/", "-")
        package = self._write_payload(f"{slug}-pkg{self._serial + 1}")
        return dataclasses.replace(package, href=self._href(repo, package.pool))

    def _publish(self, repo: str) -> None:
        repodata = self.channel_dir / repo / "repodata"
        index = _write_repodata(
            repodata, self._packages[repo], revision=self._revisions[repo], open_size=self._open_size.get(repo)
        )
        signature = repodata / SIGNATURE_NAME
        if repo in self._signed:
            armor = f"-----BEGIN PGP SIGNATURE-----\n\n{_sha256(index)}\n-----END PGP SIGNATURE-----\n"
            _atomic_write(signature, armor.encode())
        else:
            signature.unlink(missing_ok=True)

    def _republish(self, repo: str) -> None:
        self._revisions[repo] += 1
        self._publish(repo)

    def _write_snapshot(self, snapshot: str) -> None:
        for repo, packages in self._packages.items():
            deeper = [dataclasses.replace(p, href="../../" + p.href) for p in packages]
            repodata = self.channel_dir / SNAPSHOTS_DIR / snapshot / repo / "repodata"
            _write_repodata(repodata, deeper, revision=self._revisions[repo], open_size=None)
            parts = repo.split("/")
            if len(parts) == 2 and parts[0] == "target":
                pointer = {"id": snapshot, "created": "2024-01-01T00:00:00Z"}
                _atomic_write(self.channel_dir / repo / POINTER_NAME, (json.dumps(pointer) + "\n").encode())

    def set_targets(self, targets: Mapping[str, Sequence[str]] | None) -> None:
        """Write (or, with None, remove) the channel-root ``targets.json``."""
        path = self.channel_dir / TARGETS_NAME
        if targets is None:
            path.unlink(missing_ok=True)
            return
        body = {name: list(repos) for name, repos in targets.items()}
        _atomic_write(path, (json.dumps(body, indent=2, sort_keys=True) + "\n").encode())

    def set_signed(self, repo: str, *, signed: bool) -> None:
        """Publish or withdraw ``repo``'s detached signature, re-signing the current index."""
        if signed:
            self._signed.add(repo)
        else:
            self._signed.discard(repo)
        self._publish(repo)

    def update(self, repo: str, *, add: int = 0, replace: str | None = None) -> tuple[Package, ...]:
        """Change ``repo``'s packages in place and republish it with a new revision.

        ``add`` appends that many new packages. ``replace`` names a listed
        package by its location href and swaps in a new payload under the same
        name, which lands at a new pool path because the pool is
        content-addressed. Returns the packages that are new.
        """
        if add <= 0 and replace is None:
            raise ValueError("update needs add > 0 or replace")
        packages = self._packages[repo]
        fresh: list[Package] = []
        if replace is not None:
            index = next((i for i, p in enumerate(packages) if p.href == replace), None)
            if index is None:
                raise ValueError(f"{repo} lists no package at {replace}")
            new = self._write_payload(packages[index].name)
            packages[index] = dataclasses.replace(new, href=self._href(repo, new.pool))
            fresh.append(packages[index])
        for _ in range(add):
            packages.append(self._new_package(repo))
            fresh.append(packages[-1])
        self._republish(repo)
        return tuple(fresh)

    # -- metadata faults --------------------------------------------------

    def override_open_size(self, repo: str, open_size: int | None) -> None:
        """Declare ``open_size`` for ``repo``'s primary instead of its real size; None restores it."""
        if open_size is None:
            self._open_size.pop(repo, None)
        else:
            self._open_size[repo] = open_size
        self._republish(repo)

    def replace_href(self, repo: str, index: int, href: str) -> None:
        """List package ``index`` of ``repo`` at ``href`` (e.g. a hostile traversal)."""
        packages = self._packages[repo]
        packages[index] = dataclasses.replace(packages[index], href=href)
        self._republish(repo)

    def set_checksum_type(self, repo: str, index: int, checksum_type: str) -> None:
        """Declare package ``index`` of ``repo`` with ``checksum_type`` (e.g. ``md5``)."""
        packages = self._packages[repo]
        packages[index] = dataclasses.replace(packages[index], checksum_type=checksum_type)
        self._republish(repo)

    # -- serving faults ---------------------------------------------------

    def withhold(self, path: str) -> None:
        """Answer 404 for ``path`` although the file exists."""
        with self._lock:
            self._withheld.add(_norm(path))

    def fail_once(self, path: str) -> None:
        """Answer 503 to the next request for ``path``, then serve it normally."""
        with self._lock:
            self._fail_once.add(_norm(path))

    def fail_always(self, path: str) -> None:
        """Answer 503 to every request for ``path``."""
        with self._lock:
            self._fail_always.add(_norm(path))

    def corrupt(self, path: str, data: bytes | None = None) -> None:
        """Serve ``data`` for ``path`` - or, by default, its real bytes inverted."""
        with self._lock:
            self._corrupt[_norm(path)] = data

    def clear_faults(self, path: str | None = None) -> None:
        """Drop every serving fault, or only those on ``path``."""
        with self._lock:
            if path is None:
                self._withheld.clear()
                self._fail_once.clear()
                self._fail_always.clear()
                self._corrupt.clear()
                return
            key = _norm(path)
            self._withheld.discard(key)
            self._fail_once.discard(key)
            self._fail_always.discard(key)
            self._corrupt.pop(key, None)

    def respond(self, path: str) -> tuple[HTTPStatus, bytes]:
        """Return the status and body the server sends for ``path``, faults applied."""
        key = _norm(path)
        with self._lock:
            if key in self._withheld:
                return HTTPStatus.NOT_FOUND, b""
            if key in self._fail_always:
                return HTTPStatus.SERVICE_UNAVAILABLE, b""
            if key in self._fail_once:
                self._fail_once.discard(key)
                return HTTPStatus.SERVICE_UNAVAILABLE, b""
            corrupt = self._corrupt.get(key, _UNSET)
        root = self.root.resolve()
        target = (root / key.lstrip("/")).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return HTTPStatus.NOT_FOUND, b""
        body = target.read_bytes()
        if corrupt is None:
            body = bytes(byte ^ 0xFF for byte in body)
        elif isinstance(corrupt, bytes):
            body = corrupt
        return HTTPStatus.OK, body


def build_feed(root: Path, spec: FeedSpec | Mapping[str, int]) -> UpstreamFeed:
    """Publish a synthetic feed under ``root``.

    ``spec`` is a :class:`FeedSpec`, or just the repo -> package-count mapping
    for the default release ``2024`` and channel ``edge``.
    """
    if not isinstance(spec, FeedSpec):
        spec = FeedSpec(repos=dict(spec))
    return UpstreamFeed(root, spec)


class _Httpd(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, feed: UpstreamFeed) -> None:
        super().__init__((_LOOPBACK, 0), _Handler)
        self.feed = feed
        self.log_lock = threading.Lock()
        self.log: list[str] = []


class _Handler(BaseHTTPRequestHandler):
    server: _Httpd

    def do_GET(self) -> None:
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        with self.server.log_lock:
            self.server.log.append(path)
        status, body = self.server.feed.respond(path)
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep request lines out of the test output."""


class FeedServer:
    """A running loopback server over an :class:`UpstreamFeed`."""

    def __init__(self, feed: UpstreamFeed, httpd: _Httpd) -> None:
        self.feed = feed
        self._httpd = httpd

    @property
    def url(self) -> str:
        """``http://127.0.0.1:PORT`` - the root a published feed URL is built from."""
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def requests(self) -> list[str]:
        """Every requested path, in arrival order (a copy)."""
        with self._httpd.log_lock:
            return list(self._httpd.log)

    @property
    def package_requests(self) -> list[str]:
        """The requested paths that name a package (``*.rpm``), in order."""
        return [path for path in self.requests if path.endswith(".rpm")]

    def clear_requests(self) -> None:
        with self._httpd.log_lock:
            self._httpd.log.clear()


@contextlib.contextmanager
def serve_feed(feed: UpstreamFeed) -> Iterator[FeedServer]:
    """Serve ``feed.root`` on ``127.0.0.1`` at an ephemeral port until exit."""
    httpd = _Httpd(feed)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield FeedServer(feed, httpd)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=_SHUTDOWN_TIMEOUT_S)


@pytest.fixture
def feed_server() -> Iterator[Callable[[UpstreamFeed], FeedServer]]:
    """Return a starter: ``feed_server(feed)`` serves ``feed`` until the test ends."""
    with contextlib.ExitStack() as stack:
        yield lambda feed: stack.enter_context(serve_feed(feed))
