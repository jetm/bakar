#!/usr/bin/env python3
"""Smoke for the dl-mirror-link-restore premise: a bare link in ``DL_DIR`` is enough.

Drives BitBake's own fetcher library (``bb.fetch2``) in a scratch tree against a
``DL_DIR`` that holds only a symlink into a read-only mirror directory, with no
``.done`` stamp, and measures what the fetcher does with it.

Nothing leaves the machine: the upstream is ``example.invalid`` and is refused by
``BB_ALLOWED_NETWORKS`` before any DNS lookup, and the one fallback mirror is an
``http.server`` thread on 127.0.0.1.

Hermetic scenarios, each a fresh ``Fetch``:

- S1: link to a matching mirror file, no stamp, no premirror, no network.
  ``download()`` raises nothing, the entry is still a link, BitBake wrote a
  regular local stamp, and the mirror directory gained no entry.
- S2: link to a mirror file whose bytes differ from the recipe's checksum, with
  an http fallback mirror. No exception, the mirror file and directory are
  unchanged, ``DL_DIR`` ends with a regular file carrying the recipe's checksum
  and one ``_bad-checksum_`` entry renamed aside.
- S3: link to a real small gzip tarball. ``unpack()`` with no fetch, no stamp
  and no network extracts the file.

Usage:
    uv run python scripts/smoke_dl_link_restore.py --bitbake-dir DIR
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import http.server
import importlib.util
import io
import os
import pathlib
import shutil
import sys
import tarfile
import tempfile
import threading

# A copy of smoke_lib.py beside this script wins, so a host without the Claude
# skills tree can ship the two files together.
_LIB_CANDIDATES = (
    pathlib.Path(__file__).resolve().parent / "smoke_lib.py",
    pathlib.Path("~/.claude/skills/ds-archive/scripts/smoke_lib.py").expanduser(),
)
SMOKE_LIB_PATH = next((p for p in _LIB_CANDIDATES if p.is_file()), _LIB_CANDIDATES[-1])
_spec = importlib.util.spec_from_file_location("smoke_lib", SMOKE_LIB_PATH)
if _spec is None or _spec.loader is None:
    raise SystemExit(f"cannot load {SMOKE_LIB_PATH}")
smoke_lib = importlib.util.module_from_spec(_spec)
sys.modules["smoke_lib"] = smoke_lib
_spec.loader.exec_module(smoke_lib)

SCRATCH_PARENT = pathlib.Path("/var/tmp/claude-code/bakar")
# 2 preconditions + S1 (4) + S2 (5) + S3 (3).
EXPECTED_CHECKS = 14

PAYLOAD_NAME = "pkg-1.0.tar.gz"
PAYLOAD = bytes((i * 31 + 7) % 256 for i in range(4096))
BAD_PAYLOAD = bytes((i * 17 + 3) % 256 for i in range(4096))
UPSTREAM_HOST = "example.invalid"
README = b"hello\n"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@contextlib.contextmanager
def http_server(directory: pathlib.Path):
    """Serve ``directory`` on 127.0.0.1 from a daemon thread; yield the port."""
    handler = functools.partial(QuietHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def make_datastore(
    bb,
    dl_dir: pathlib.Path,
    mirror_dir: pathlib.Path | None = None,
    port: int | None = None,
    *,
    no_network: bool = False,
):
    d = bb.data.init()
    d.setVar("DL_DIR", str(dl_dir))
    # Fetcher caches and persistent data must land in the scratch tree too.
    state = dl_dir.parent / (dl_dir.name + "-state")
    state.mkdir(exist_ok=True)
    for var in ("BB_CACHEDIR", "PERSISTENT_DIR", "CACHE"):
        d.setVar(var, str(state))
    d.setVar("FETCHCMD_wget", "/usr/bin/env wget -t 2 -T 10 --passive-ftp")
    d.setVar("MIRRORS", "")
    d.setVar("BB_ALLOWED_NETWORKS", "127.0.0.1")
    if no_network:
        d.setVar("BB_NO_NETWORK", "1")
    premirrors = ""
    if mirror_dir is not None:
        premirrors += "(https?|ftp|crate|gomod)://.*/.* file://" + str(mirror_dir) + "/ \n"
    if port is not None:
        premirrors += "(https?|ftp|crate|gomod)://.*/.* http://127.0.0.1:" + str(port) + "/ \n"
    d.setVar("PREMIRRORS", premirrors)
    bb.fetch2.fetcher_init(d)
    return d


def recipe_uri(name: str, sha: str) -> str:
    return f"https://{UPSTREAM_HOST}/{name};sha256sum={sha}"


def fetch(bb, d, name: str, sha: str) -> Exception | None:
    """One fresh ``Fetch`` + ``download()``; return the exception that escaped, if any."""
    try:
        bb.fetch2.Fetch([recipe_uri(name, sha)], d).download()
    except Exception as exc:  # noqa: BLE001 - any escape is the measurement
        return exc
    return None


def entries(path: pathlib.Path) -> list[str]:
    return sorted(p.name for p in path.iterdir())


def sha256_of(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("pkg-1.0/README")
        info.size = len(README)
        tar.addfile(info, io.BytesIO(README))
    return buf.getvalue()


def hermetic(run, bb, root: pathlib.Path) -> None:
    sha = hashlib.sha256(PAYLOAD).hexdigest()
    run.check("wget is on PATH", shutil.which("wget") is not None, "wget not found")
    run.check("not running as root (read-only mirror dir must bind)", os.geteuid() != 0, "euid 0 ignores chmod")

    mirrors = []
    try:
        # S1: matching link, no stamp, nothing to reach.
        mirror1 = root / "mirror1"
        mirror1.mkdir()
        (mirror1 / PAYLOAD_NAME).write_bytes(PAYLOAD)
        os.chmod(mirror1, 0o555)
        mirrors.append(mirror1)
        dl1 = root / "dl1"
        dl1.mkdir()
        (dl1 / PAYLOAD_NAME).symlink_to(mirror1 / PAYLOAD_NAME)
        exc = fetch(bb, make_datastore(bb, dl1), PAYLOAD_NAME, sha)
        stamp = dl1 / (PAYLOAD_NAME + ".done")
        run.check("S1 download raised nothing", exc is None, repr(exc))
        run.check("S1 DL_DIR entry is still the link", (dl1 / PAYLOAD_NAME).is_symlink(), "entry is not a link")
        run.check(
            "S1 BitBake wrote a regular local stamp",
            stamp.is_file() and not stamp.is_symlink(),
            "no regular stamp in DL_DIR",
        )
        run.check("S1 mirror directory gained no entry", entries(mirror1) == [PAYLOAD_NAME], f"{entries(mirror1)}")

        # S2: link whose bytes differ from the recipe checksum, http fallback.
        mirror2 = root / "mirror2"
        mirror2.mkdir()
        (mirror2 / PAYLOAD_NAME).write_bytes(BAD_PAYLOAD)
        os.chmod(mirror2, 0o555)
        mirrors.append(mirror2)
        served = root / "http"
        served.mkdir()
        (served / PAYLOAD_NAME).write_bytes(PAYLOAD)
        dl2 = root / "dl2"
        dl2.mkdir()
        (dl2 / PAYLOAD_NAME).symlink_to(mirror2 / PAYLOAD_NAME)
        with http_server(served) as port:
            exc = fetch(bb, make_datastore(bb, dl2, mirror2, port), PAYLOAD_NAME, sha)
        entry = dl2 / PAYLOAD_NAME
        run.check("S2 download raised nothing", exc is None, repr(exc))
        run.check(
            "S2 mirror file bytes unchanged",
            (mirror2 / PAYLOAD_NAME).read_bytes() == BAD_PAYLOAD,
            "mirror bytes differ",
        )
        run.check("S2 mirror directory unchanged", entries(mirror2) == [PAYLOAD_NAME], f"{entries(mirror2)}")
        run.check(
            "S2 DL_DIR entry is a regular file with the recipe's checksum",
            entry.is_file() and not entry.is_symlink() and sha256_of(entry) == sha,
            f"is_symlink={entry.is_symlink()}",
        )
        bad = [name for name in entries(dl2) if "_bad-checksum_" in name]
        run.check("S2 one rejected entry renamed aside in DL_DIR", len(bad) == 1, f"{bad}")

        # S3: unpack through a bare link, no fetch, no stamp, no network.
        tarball = make_tarball()
        mirror3 = root / "mirror3"
        mirror3.mkdir()
        (mirror3 / PAYLOAD_NAME).write_bytes(tarball)
        dl3 = root / "dl3"
        dl3.mkdir()
        (dl3 / PAYLOAD_NAME).symlink_to(mirror3 / PAYLOAD_NAME)
        out = root / "work3"
        out.mkdir()
        d = make_datastore(bb, dl3, no_network=True)
        unpack_exc: Exception | None = None
        try:
            bb.fetch2.Fetch([recipe_uri(PAYLOAD_NAME, hashlib.sha256(tarball).hexdigest())], d).unpack(str(out))
        except Exception as exc:  # noqa: BLE001 - any escape is the measurement
            unpack_exc = exc
        extracted = out / "pkg-1.0" / "README"
        run.check("S3 unpack raised nothing", unpack_exc is None, repr(unpack_exc))
        run.check(
            "S3 unpack extracted the file",
            extracted.is_file() and extracted.read_bytes() == README,
            "README missing or wrong",
        )
        run.check("S3 DL_DIR entry is still the link", (dl3 / PAYLOAD_NAME).is_symlink(), "entry is not a link")
    finally:
        for mirror in mirrors:
            os.chmod(mirror, 0o755)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bitbake-dir", required=True, type=pathlib.Path, help="BitBake checkout (uses DIR/lib)")
    args = parser.parse_args()

    sys.path.insert(0, str(args.bitbake_dir.expanduser().resolve() / "lib"))
    import bb
    import bb.data
    import bb.fetch2

    print(f"bitbake {bb.__version__} from {pathlib.Path(bb.__file__).parent}")
    SCRATCH_PARENT.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(SCRATCH_PARENT)
    with (
        smoke_lib.SmokeRun(EXPECTED_CHECKS) as run,
        smoke_lib.isolated_env("dl-link-restore-smoke-") as (root, _env),
    ):
        hermetic(run, bb, root)
    return smoke_lib.finish(run)


if __name__ == "__main__":
    sys.exit(main())
