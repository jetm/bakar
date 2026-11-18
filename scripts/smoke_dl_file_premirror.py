#!/usr/bin/env python3
"""Smoke for design assumption A1 of dl-dir-space-sharing: a ``file://`` premirror.

Drives BitBake's own fetcher library (``bb.fetch2``) against a local ``file://``
premirror and measures what the fetcher leaves in ``DL_DIR``. The premise is
that a premirror hit becomes a symlink in ``DL_DIR`` (no copy), a mirror file
with the wrong checksum is rejected in favour of the next mirror, and a link
whose target vanished is replaced by a real download.

Nothing leaves the machine: the upstream is ``example.invalid`` and is refused
by ``BB_ALLOWED_NETWORKS`` before any DNS lookup, and the fallback mirror is an
``http.server`` thread on 127.0.0.1.

Hermetic scenarios (default), each a fresh ``Fetch`` + ``download()``:

- (a) hit: ``DL_DIR`` holds a link to the mirror file and a ``.done`` stamp.
- (b) wrong checksum, mirror directory read-only (as the real mount is): the
  mirror file is untouched, nothing is renamed, ``DL_DIR`` holds a real file
  fetched over http.
- (c) vanished target: after (a) the mirror file is deleted; a fresh fetch
  leaves a real file, not a dangling link.

Live mode (``--mirror-dir DIR``) fetches one small real file from DIR through
the same machinery under a stopwatch, for the mount-up / mount-down timing
runs.

Usage:
    uv run python scripts/smoke_dl_file_premirror.py --bitbake-dir DIR
    uv run python scripts/smoke_dl_file_premirror.py --bitbake-dir DIR --mirror-dir DIR
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import http.server
import importlib.util
import os
import pathlib
import shutil
import stat
import sys
import tempfile
import threading
import time

# A copy of smoke_lib.py beside this script wins, so the live-gate run on a host
# without the Claude skills tree (the builder) can ship the two files together.
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
EXPECTED_CHECKS = 16
LIVE_EXPECTED_CHECKS = 2
LIVE_MAX_BYTES = 5 * 1024 * 1024

PAYLOAD_NAME = "pkg-1.0.tar.gz"
PAYLOAD = bytes((i * 31 + 7) % 256 for i in range(4096))
BAD_PAYLOAD = bytes((i * 17 + 3) % 256 for i in range(4096))
UPSTREAM_HOST = "example.invalid"

MANUAL_STEPS = (
    smoke_lib.ManualStep(
        "human",
        "Task 13.1: run live mode (`--mirror-dir <real mirror>`) twice through scripts/measure_dl_mirror_mount.sh, "
        "with the mount up and with it down, and record fetch-seconds and final-kind for each.",
    ),
)


def sha256_of(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def make_datastore(bb, dl_dir: pathlib.Path, mirror_dir: pathlib.Path, port: int):
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
    d.setVar(
        "PREMIRRORS",
        "(https?|ftp|crate|gomod)://.*/.* file://" + str(mirror_dir) + "/ \n"
        "(https?|ftp|crate|gomod)://.*/.* http://127.0.0.1:" + str(port) + "/ \n",
    )
    bb.fetch2.fetcher_init(d)
    return d


def fetch(bb, d, name: str, sha: str) -> Exception | None:
    """One fresh ``Fetch`` + ``download()``; return the exception that escaped, if any."""
    uri = f"https://{UPSTREAM_HOST}/{name};sha256sum={sha}"
    try:
        bb.fetch2.Fetch([uri], d).download()
    except Exception as exc:  # noqa: BLE001 - any escape is the measurement
        return exc
    return None


def kind(path: pathlib.Path) -> str:
    if path.is_symlink():
        return "dangling-link" if not path.exists() else "link"
    if path.is_file():
        return "file"
    return "absent"


def set_mode(path: pathlib.Path, mode: int) -> None:
    os.chmod(path, mode)


def hermetic(run, bb, root: pathlib.Path) -> None:
    sha = hashlib.sha256(PAYLOAD).hexdigest()
    served = root / "http"
    served.mkdir()
    (served / PAYLOAD_NAME).write_bytes(PAYLOAD)

    run.check("wget is on PATH", shutil.which("wget") is not None, "wget not found")
    run.check("not running as root (read-only mirror dir must bind)", os.geteuid() != 0, "euid 0 ignores chmod")

    mirror_a = root / "mirror-a"
    mirror_a.mkdir()
    (mirror_a / PAYLOAD_NAME).write_bytes(PAYLOAD)
    mirror_b = root / "mirror-b"
    mirror_b.mkdir()
    bad_file = mirror_b / PAYLOAD_NAME
    bad_file.write_bytes(BAD_PAYLOAD)
    set_mode(mirror_a, 0o555)
    set_mode(mirror_b, 0o555)
    try:
        with http_server(served) as port:
            # (a) hit
            dl_a = root / "dl-a"
            dl_a.mkdir()
            d = make_datastore(bb, dl_a, mirror_a, port)
            exc = fetch(bb, d, PAYLOAD_NAME, sha)
            entry = dl_a / PAYLOAD_NAME
            run.check("(a) download raised nothing", exc is None, repr(exc))
            run.check("(a) DL_DIR entry is a symlink", entry.is_symlink(), f"kind {kind(entry)}")
            target = pathlib.Path(os.path.realpath(entry)) if entry.is_symlink() else None
            run.check(
                "(a) link target is the mirror file",
                target == (mirror_a / PAYLOAD_NAME).resolve(),
                f"target {target}",
            )
            stamp = dl_a / (PAYLOAD_NAME + ".done")
            run.check(
                "(a) .done stamp is a regular file in DL_DIR",
                stamp.is_file() and not stamp.is_symlink(),
                f"kind {kind(stamp)}",
            )

            # (b) wrong checksum on a read-only mirror
            dl_b = root / "dl-b"
            dl_b.mkdir()
            d = make_datastore(bb, dl_b, mirror_b, port)
            exc = fetch(bb, d, PAYLOAD_NAME, sha)
            entry = dl_b / PAYLOAD_NAME
            run.check("(b) no exception escaped", exc is None, repr(exc))
            run.check("(b) mirror file unchanged", bad_file.read_bytes() == BAD_PAYLOAD, "mirror bytes differ")
            renamed = [p.name for p in mirror_b.iterdir() if "_bad-checksum_" in p.name]
            run.check("(b) no _bad-checksum_ file in the mirror dir", not renamed, f"found {renamed}")
            run.check("(b) DL_DIR entry is a regular file, not a link", kind(entry) == "file", f"kind {kind(entry)}")
            run.check(
                "(b) DL_DIR entry has the recipe's checksum",
                entry.is_file() and sha256_of(entry) == sha,
                "checksum differs or entry missing",
            )

            # (c) vanished target: (a)'s DL_DIR keeps its link and .done stamp
            set_mode(mirror_a, 0o755)
            (mirror_a / PAYLOAD_NAME).unlink()
            set_mode(mirror_a, 0o555)
            entry = dl_a / PAYLOAD_NAME
            run.check("(c) precondition: link is dangling", kind(entry) == "dangling-link", f"kind {kind(entry)}")
            d = make_datastore(bb, dl_a, mirror_a, port)
            exc = fetch(bb, d, PAYLOAD_NAME, sha)
            run.check("(c) no exception escaped", exc is None, repr(exc))
            run.check("(c) DL_DIR entry is a regular file", kind(entry) == "file", f"kind {kind(entry)}")
            run.check(
                "(c) DL_DIR entry has the recipe's checksum",
                entry.is_file() and sha256_of(entry) == sha,
                "checksum differs or entry missing",
            )
            run.check("(c) no dangling link left", kind(entry) != "dangling-link", "dangling link")
    finally:
        set_mode(mirror_a, 0o755)
        set_mode(mirror_b, 0o755)


def live(run, bb, root: pathlib.Path, mirror_dir: pathlib.Path, live_bytes: pathlib.Path | None = None) -> None:
    if live_bytes is not None:
        # The caller already copied the file off the mount (keeping its name), so
        # nothing here walks or reads the mirror: with the mount blocked the only
        # contact left is BitBake's own fetch, which is what the stopwatch measures.
        pick = mirror_dir / live_bytes.name
        data = live_bytes.read_bytes()
        run.check("live: file prepared off the mount", ok=True, detail=str(live_bytes))
    else:
        pick = None
        for dirpath, _dirs, files in os.walk(mirror_dir):
            for name in sorted(files):
                path = pathlib.Path(dirpath) / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_size < LIVE_MAX_BYTES:
                    pick = path
                    break
            if pick:
                break
        run.check("live: found a regular file under 5 MB", pick is not None, f"none under {mirror_dir}")
        if pick is None:
            run.check("live: final bytes match the checksum", ok=False, detail="no file picked")
            return
        data = pick.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    served = root / "http"
    served.mkdir()
    (served / pick.name).write_bytes(data)
    dl = root / "dl"
    dl.mkdir()
    with http_server(served) as port:
        d = make_datastore(bb, dl, pick.parent, port)
        start = time.monotonic()
        exc = fetch(bb, d, pick.name, sha)
        elapsed = time.monotonic() - start
    entry = dl / pick.name
    print(f"fetch-seconds={elapsed:.3f}")
    print(f"final-kind={'link' if entry.is_symlink() else 'file'}")
    run.check(
        "live: final bytes match the checksum",
        exc is None and entry.exists() and sha256_of(entry) == sha,
        repr(exc) if exc else "checksum differs or entry missing",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bitbake-dir", required=True, type=pathlib.Path, help="BitBake checkout (uses DIR/lib)")
    parser.add_argument("--mirror-dir", type=pathlib.Path, help="live mode: time one fetch from this mirror")
    parser.add_argument(
        "--live-bytes",
        type=pathlib.Path,
        help="live mode: a local copy (same file name) of one mirror file, taken while the mount was up",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(args.bitbake_dir.expanduser().resolve() / "lib"))
    import bb
    import bb.data
    import bb.fetch2

    print(f"bitbake {bb.__version__} from {pathlib.Path(bb.__file__).parent}")
    SCRATCH_PARENT.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(SCRATCH_PARENT)
    expected = LIVE_EXPECTED_CHECKS if args.mirror_dir else EXPECTED_CHECKS
    steps = () if args.mirror_dir else MANUAL_STEPS
    with (
        smoke_lib.SmokeRun(expected, manual_steps=steps) as run,
        smoke_lib.isolated_env("dl-premirror-smoke-") as (root, _env),
    ):
        if args.mirror_dir:
            live(
                run,
                bb,
                root,
                # abspath, not resolve(): resolve() stats every component, which
                # would touch a blocked mount before the stopwatch starts.
                pathlib.Path(os.path.abspath(args.mirror_dir.expanduser())),
                args.live_bytes.expanduser().resolve() if args.live_bytes else None,
            )
        else:
            hermetic(run, bb, root)
    return smoke_lib.finish(run)


if __name__ == "__main__":
    sys.exit(main())
