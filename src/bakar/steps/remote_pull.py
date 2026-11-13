"""Checks around pulling a remote build's files into a private staging directory.

The description a remote host prints is untrusted input, and so is every byte the
copy brings back. Two independent checks guard that: ``validate_description``
refuses a bad listing before anything is copied, and ``verify_staged_tree``
walks what actually landed and decides on type, containment, size and checksum.
The walk is the authority, because the remote tree can change between the
listing and the copy and a transfer option can be mis-set.

``pull_files`` is the one function here that starts a process: it copies exactly
the described paths with rsync, then re-checks the tree and the host.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from bakar.feed_mirror_paths import UnsafePathError, resolve_destination
from bakar.steps.remote_dispatch import RSYNC_IO_TIMEOUT_SECONDS, SSH_BOUND_OPTS

if TYPE_CHECKING:
    from collections.abc import Callable

    from bakar.feed_describe import BuildDescription

    # (argv, timeout seconds) -> (exit code, stderr text); raises TimeoutExpired or OSError.
    Runner = Callable[[list[str], float], tuple[int, str]]

MAX_FILE_BYTES = 2 * 1024**3
MAX_TOTAL_BYTES = 8 * 1024**3
_HASH_CHUNK = 1024 * 1024
_FREE_SPACE_MARGIN_PERCENT = 10

# A path component: letters, digits and ``_.+~-``, never starting with a dot.
_COMPONENT = r"[A-Za-z0-9_+~-][A-Za-z0-9_.+~-]*"
_ALLOWED_SHAPES = (
    re.compile(rf"rpm/{_COMPONENT}/{_COMPONENT}\.rpm"),
    re.compile(r"rpm/avocado-repo\.map"),
    re.compile(rf"images/{_COMPONENT}/{_COMPONENT}\.testdata\.json"),
)


class PullRefused(RuntimeError):  # noqa: N818 - name fixed by the pull spec
    """The description or the staged tree failed a check; the message names the entry."""


def _check_path(path: str) -> None:
    if not path:
        raise PullRefused("described path '' is empty")
    if path.startswith("/"):
        raise PullRefused(f"described path {path!r} is absolute")
    if "\\" in path:
        raise PullRefused(f"described path {path!r} contains a backslash")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise PullRefused(f"described path {path!r} has an empty, '.' or '..' component")
    # fullmatch: ``$`` would let a trailing newline through.
    if not any(shape.fullmatch(path) for shape in _ALLOWED_SHAPES):
        raise PullRefused(
            f"described path {path!r} matches no allowed shape "
            "(rpm/<arch>/<name>.rpm, rpm/avocado-repo.map, images/<machine>/<name>.testdata.json)"
        )


def validate_description(
    desc: BuildDescription, *, max_file: int = MAX_FILE_BYTES, max_total: int = MAX_TOTAL_BYTES
) -> None:
    """Refuse a description whose file list is unsafe to copy, before any byte moves."""
    seen: set[str] = set()
    total = 0
    for item in desc.files:
        _check_path(item.path)
        if item.path in seen:
            raise PullRefused(f"described path {item.path!r} repeats another entry")
        seen.add(item.path)
        if item.size > max_file:
            raise PullRefused(
                f"described file {item.path!r} is {item.size} bytes, over the {max_file} byte per-file cap"
            )
        total += item.size
    if total > max_total:
        raise PullRefused(f"described files total {total} bytes, over the {max_total} byte total cap")


def _walk_staging(root: Path) -> tuple[dict[str, Path], set[str]]:
    """Return the regular files and directories under ``root``, refusing anything else.

    Every entry is examined with ``lstat`` so a link is seen as a link, and none is
    followed. Keys are POSIX paths relative to ``root``.
    """
    files: dict[str, Path] = {}
    dirs: set[str] = set()
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            children = sorted(entries, key=lambda e: e.name)
        for entry in children:
            path = Path(entry.path)
            rel = path.relative_to(root).as_posix()
            mode = path.lstat()
            if stat.S_ISLNK(mode.st_mode):
                raise PullRefused(f"staged entry {rel!r} is a symbolic link")
            if stat.S_ISDIR(mode.st_mode):
                dirs.add(rel)
                pending.append(path)
            elif stat.S_ISREG(mode.st_mode):
                if mode.st_nlink > 1:
                    raise PullRefused(f"staged file {rel!r} has {mode.st_nlink} hard links")
                files[rel] = path
            else:
                raise PullRefused(f"staged entry {rel!r} is not a regular file or directory")
    return files, dirs


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verify_staged_tree(staging: Path, desc: BuildDescription) -> None:
    """Refuse unless ``staging`` holds exactly the described regular files, intact."""
    if staging.is_symlink():
        raise PullRefused(f"staging directory {str(staging)!r} is a symbolic link")
    files, dirs = _walk_staging(staging)
    for rel in files:
        try:
            resolve_destination(staging, PurePosixPath(rel))
        except UnsafePathError as exc:
            raise PullRefused(f"staged file {rel!r} resolves outside the staging directory: {exc}") from exc

    described = {item.path: item for item in desc.files}
    allowed_dirs = {parent.as_posix() for rel in described for parent in PurePosixPath(rel).parents if parent.parts}
    for rel in sorted(dirs - allowed_dirs):
        raise PullRefused(f"staged directory {rel!r} is not in the description")
    for rel in sorted(described.keys() - files.keys()):
        raise PullRefused(f"described file {rel!r} is missing from staging")
    for rel in sorted(files.keys() - described.keys()):
        raise PullRefused(f"staged file {rel!r} is not in the description")

    for rel, item in sorted(described.items()):
        actual_size = files[rel].lstat().st_size
        if actual_size != item.size:
            raise PullRefused(f"staged file {rel!r} has size {actual_size}, described size {item.size}")
    for rel, item in sorted(described.items()):
        actual = _sha256_of(files[rel])
        if actual != item.sha256:
            raise PullRefused(f"staged file {rel!r} has sha256 {actual}, described sha256 {item.sha256}")


def make_staging(parent: Path) -> Path:
    """Create a fresh owner-only directory inside ``parent`` and return it.

    ``parent`` must be an existing real directory: a symlink there would let the
    staging area, and so the pulled files, land wherever the link points.
    """
    if parent.is_symlink():
        raise PullRefused(f"staging parent {str(parent)!r} is a symbolic link")
    if not parent.exists():
        raise PullRefused(f"staging parent {str(parent)!r} does not exist")
    if not parent.is_dir():
        raise PullRefused(f"staging parent {str(parent)!r} is not a directory")
    staging = Path(tempfile.mkdtemp(prefix="pull-", dir=parent))
    info = staging.lstat()
    if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
        staging.rmdir()
        raise PullRefused(
            f"staging directory {str(staging)!r} is mode {stat.S_IMODE(info.st_mode):04o} "
            f"owned by uid {info.st_uid}, expected 0700 owned by uid {os.geteuid()}"
        )
    return staging


def check_free_space(
    staging: Path,
    desc: BuildDescription,
    *,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
) -> None:
    """Refuse when ``staging``'s filesystem cannot hold the described total plus ten percent."""
    total = sum(item.size for item in desc.files)
    needed = total + -(-total * _FREE_SPACE_MARGIN_PERCENT // 100)
    free = disk_usage(staging).free
    if free < needed:
        raise PullRefused(
            f"staging filesystem has {free} bytes free, below the {needed} bytes needed "
            f"({total} described plus {_FREE_SPACE_MARGIN_PERCENT} percent)"
        )


# Total copy timeout: a floor plus the described bytes at a pessimistic 2 MiB/s.
_PULL_TIMEOUT_FLOOR_SECONDS = 600
_PULL_MIN_BYTES_PER_SECOND = 2 * 1024**2
_KILL_GRACE_SECONDS = 5.0


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "no output"


def _terminate_group(proc: subprocess.Popen[bytes]) -> None:
    """Stop ``proc``'s own process group, and only that group."""
    if os.getpgid(proc.pid) != proc.pid:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + _KILL_GRACE_SECONDS
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.05)


def _run_rsync(argv: list[str], timeout: float) -> tuple[int, str]:
    """Run ``argv`` in its own session with output in files; return (exit code, stderr text)."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, stdout=out, stderr=err, stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_group(proc)
            proc.wait()
            raise
        err.seek(0)
        return code, err.read(65536).decode("utf-8", "replace")


def build_pull_argv(host: str, desc: BuildDescription, staging: Path, list_file: Path) -> list[str]:
    """The rsync argv: exact listed files only, links and special files never copied."""
    ssh_cmd = shlex.join(["ssh", *SSH_BOUND_OPTS, "-o", "BatchMode=yes"])
    return [
        "rsync",
        "--times",
        "--no-links",
        "--no-perms",
        "--no-owner",
        "--no-group",
        "--no-devices",
        "--no-specials",
        "--protect-args",
        f"--timeout={RSYNC_IO_TIMEOUT_SECONDS}",
        f"--max-size={MAX_FILE_BYTES}",
        "-e",
        ssh_cmd,
        f"--files-from={list_file}",
        "--from0",
        f"{host}:{desc.deploy_dir}/",
        f"{staging}/",
    ]


def _compare_descriptions(first: BuildDescription, second: BuildDescription) -> None:
    if second.live:
        raise PullRefused(f"host reports a live build after the copy: {second.live_reason or 'no reason given'}")
    if second.state != "succeeded":
        raise PullRefused(f"host reports state {second.state!r} after the copy, expected 'succeeded'")
    before = {f.path: f for f in first.files}
    after = {f.path: f for f in second.files}
    for path in sorted(before.keys() | after.keys()):
        if path not in after:
            raise PullRefused(f"file {path!r} is no longer described after the copy")
        if path not in before:
            raise PullRefused(f"file {path!r} appeared in the description after the copy")
        if before[path].size != after[path].size:
            raise PullRefused(
                f"file {path!r} changed size during the pull: {before[path].size} then {after[path].size}"
            )
        if before[path].sha256 != after[path].sha256:
            raise PullRefused(f"file {path!r} changed checksum during the pull")


def _copy(host: str, desc: BuildDescription, staging: Path, runner: Runner) -> None:
    total = sum(item.size for item in desc.files)
    timeout = _PULL_TIMEOUT_FLOOR_SECONDS + total / _PULL_MIN_BYTES_PER_SECOND
    # The list lives outside staging so it can never be mistaken for a pulled file.
    fd, name = tempfile.mkstemp(prefix="bakar-pull-list-")
    list_file = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"".join(item.path.encode() + b"\0" for item in desc.files))
        argv = build_pull_argv(host, desc, staging, list_file)
        try:
            code, stderr = runner(argv, timeout)
        except subprocess.TimeoutExpired as exc:
            raise PullRefused(f"copy from {host} timed out after {exc.timeout:g}s") from exc
        except OSError as exc:
            raise PullRefused(f"copy from {host} could not run rsync: {exc}") from exc
        if code != 0:
            raise PullRefused(f"copy from {host} failed with exit {code}: {_first_line(stderr)}")
    finally:
        list_file.unlink(missing_ok=True)


def pull_files(  # noqa: PLR0913 - the spec fixes this signature
    host: str,
    desc: BuildDescription,
    staging: Path,
    *,
    describe_again: Callable[[], BuildDescription],
    runner: Runner | None = None,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
) -> None:
    """Copy exactly the described files from ``host`` into ``staging`` and re-check them.

    On success ``staging`` is left for the caller, which removes it after
    publishing. On any failure ``staging`` is removed before the error propagates.
    """
    try:
        if host.startswith("-"):
            raise PullRefused(f"invalid host {host!r}: must not begin with '-' (it would parse as an ssh/rsync option)")
        validate_description(desc)
        check_free_space(staging, desc, disk_usage=disk_usage)
        _copy(host, desc, staging, runner or _run_rsync)
        verify_staged_tree(staging, desc)
        _compare_descriptions(desc, describe_again())
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
