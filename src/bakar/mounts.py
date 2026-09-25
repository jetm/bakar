"""Mount-table primitives: reading ``/proc/mounts`` and judging NFS options.

The cluster answers three questions about a path's backing filesystem - which
mount entry covers it, whether that entry is NFS, and whether the mount's
options bound how long a stale view can survive - plus the ``/proc/locks``
delegation count that shares the same NFS subject. All of it reads only
``pathlib.Path`` and its own constants, so it sits below the diagnostics checks
and the build-stop lock guard that consume it rather than inside either.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

# The NFS subset of _FS_BLOCK. Only these are fatal for the build TMPDIR
# (bitbake's sanity check aborts on TMPDIR over NFS); the rest break only the
# source-layer workspace root, so a local TMPDIR override cannot rescue them.
_FS_NFS: frozenset[str] = frozenset({"nfs", "nfs4"})

# Above systemd's 15-second automount mount timeout on the cluster's fstab
# lines, so a slow-but-successful first mount is not misreported as
# unresponsive. See design.md's "Probe readiness with a statfs child process"
# decision. Moved above the mount-table helpers below (rather than staying
# next to probe_statfs, its original home) because :func:`_resolve_bounded`
# now needs it as a default argument at function-definition time.
CACHE_PROBE_DEADLINE_S = 20.0

# GNU coreutils' stderr text for a path with a missing component, forced to
# English via the child's LC_ALL=C/LANGUAGE=C environment (see probe_statfs
# and _resolve_bounded). Both ``stat -f`` and ``realpath`` render ENOENT via
# the same strerror(3) text, so one constant covers both children. Used to
# distinguish a genuinely absent path - which must NOT block, and for
# _resolve_bounded must still fall back to a lexical comparison target
# rather than losing the answer outright - from every other, more dangerous
# failure (permission denied, a symlink loop, an unexpected error), which
# must fail closed to undetermined instead.
_ENOENT_TEXT = "No such file or directory"


# Octal escapes ``/proc/mounts`` and ``/etc/fstab`` use for whitespace and
# backslash in path fields (so a space-containing mountpoint still splits
# cleanly on whitespace when the line is tokenized). A single combined regex
# substitution decodes all four in one pass - decoding them one at a time
# with sequential ``str.replace`` calls would risk one escape's *output*
# (e.g. a literal backslash from ``\134``) being mis-read as the start of a
# later escape during a subsequent pass.
_MOUNT_FIELD_ESCAPES: dict[str, str] = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}
_MOUNT_FIELD_ESCAPE_RE = re.compile(r"\\(040|011|012|134)")


def _decode_mount_field(field: str) -> str:
    """Decode ``/proc/mounts``/``/etc/fstab`` octal path escapes in ``field``.

    Without this, a mountpoint or source whose real path contains a space is
    written by the kernel/fstab as e.g. ``/mnt/yocto\\040cache``, which never
    textually matches a resolved target path built from the real (unescaped)
    directory name - the entry silently fails to cover it and lookup falls
    through to a shorter, wrong-fstype parent mount.
    """
    return _MOUNT_FIELD_ESCAPE_RE.sub(lambda m: _MOUNT_FIELD_ESCAPES[m.group(1)], field)


def _lexical_normalize(path: Path) -> Path:
    """Absolute, lexically normalized form of ``path`` with no filesystem access.

    Makes ``path`` absolute against the current working directory when it is
    relative, then collapses ``.``/``..`` components purely by string
    manipulation (``os.path.normpath``). Unlike ``Path.resolve()`` this never
    calls ``os.path.realpath()`` - no ``stat``/``lstat`` touches ``path`` or
    any of its components, so a wedged NFS mount under one of those
    components cannot put the caller into uninterruptible sleep.

    This is deliberately the FALLBACK path now, not the primary one -
    :func:`_resolve_bounded` calls it only when a bounded symlink-resolution
    child exits non-zero for a reason other than a hang (e.g. the path does
    not exist yet), and every caller in this module goes through that
    resolver rather than calling this function to classify a path. Kept
    filesystem-I/O-free because that is exactly the property that makes it a
    safe fallback when the bounded resolution itself could not run to
    completion for any reason short of an outright wedge.
    """
    absolute = path if path.is_absolute() else Path.cwd() / path
    return Path(os.path.normpath(absolute))


def _run_realpath_bounded(specs: dict[Path, list[str]], deadline: float) -> dict[Path, tuple[int, bytes, bytes] | None]:
    """Fork one ``realpath`` child per entry in ``specs``, bounded by ``deadline``.

    ``specs`` maps each target path to the full ``realpath`` argv, so a
    caller can run the plain (no-flags) form and, for a subset of paths, a
    second ``-m`` (``--canonicalize-missing``) pass without duplicating the
    fork/poll/kill mechanics. ``deadline`` is an absolute ``time.monotonic()``
    value shared across every call a single :func:`_resolve_bounded`
    invocation makes - a retry batch spends only what remains of the
    ORIGINAL ``deadline_s`` budget, never a fresh one, so the two batches
    together can never block the caller past ``deadline_s`` in total.

    Returns, per path, ``None`` for a child still running at ``deadline``
    (killed and abandoned - a wedge that does not even respond to SIGKILL
    within the trailing ``wait(timeout=0.5)`` is abandoned rather than waited
    on further), or the child's ``(returncode, stdout, stderr)`` bytes.
    """
    env = {**os.environ, "LC_ALL": "C", "LANGUAGE": "C"}
    procs: dict[Path, subprocess.Popen[bytes]] = {
        path: subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        for path, argv in specs.items()
    }

    exit_codes: dict[Path, int] = {}
    pending = set(procs)
    while pending:
        for path in list(pending):
            rc = procs[path].poll()
            if rc is not None:
                exit_codes[path] = rc
                pending.discard(path)
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(0.05)

    results: dict[Path, tuple[int, bytes, bytes] | None] = {}
    for path in pending:
        proc = procs[path]
        proc.kill()
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        results[path] = None

    for path, rc in exit_codes.items():
        proc = procs[path]
        stdout_bytes = proc.stdout.read() if proc.stdout is not None else b""
        stderr_bytes = proc.stderr.read() if proc.stderr is not None else b""
        results[path] = (rc, stdout_bytes, stderr_bytes)

    for proc in procs.values():
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()

    return results


def _resolve_bounded(paths: Sequence[Path], *, deadline_s: float = CACHE_PROBE_DEADLINE_S) -> dict[Path, Path | None]:
    """Bounded, concurrent, symlink-following resolution for ``paths``.

    Forks one ``realpath --`` child per path, all at once, and polls them the
    same way :func:`probe_statfs` polls its ``stat -f`` children: never
    ``communicate()`` (which would wait on a child stuck in an uninterruptible
    NFS sleep), just ``poll()`` against a single shared ``deadline_s``, then
    SIGKILL plus one bounded ``wait(timeout=0.5)`` for anything still alive
    past it - a child that does not even respond to SIGKILL within that
    half-second is abandoned rather than waited on further.

    Real symlink resolution needs exactly the ``lstat``-every-component work
    ``Path.resolve()`` does, which is the same call this module replaced with
    :func:`_lexical_normalize` (bug: that swap silently dropped symlink
    resolution outright, fail-opening the ``nfs-safe-lock-clearing`` guard for
    a symlinked build directory whose target lives on NFS). Forking the
    resolution into a bounded child recovers the correct, symlink-aware
    classification for the common (healthy) case while keeping the caller
    itself never blocked past ``deadline_s`` - the property
    ``_lexical_normalize`` alone could not deliver together with correctness.

    ``os.path.islink()``/``os.lstat()`` on the path directly was considered as
    a cheap pre-check to skip the fork when no symlink is present, but it
    carries the identical hang risk this function exists to bound: an
    ``lstat`` still has to traverse every parent directory to locate the
    entry, so a wedged automount ancestor hangs it exactly as it would hang
    ``Path.resolve()``. There is no filesystem-touching operation on ``path``
    that is both cheap AND safe from that hang, so every call pays the fork -
    correctness on this safety-critical (lock-deletion, cache-mount-block)
    path matters more than saving one.

    The plain (no-flags) ``realpath`` fails ENOENT as soon as it reaches a
    missing path component, BEFORE resolving anything past that point - so a
    path with an existing symlink prefix followed by a missing deeper
    component (e.g. ``/local/cache-link/newsubdir/sstate`` where
    ``cache-link`` -> ``/mnt/nfs/share`` and ``newsubdir`` does not exist yet)
    fails without ever following ``cache-link``. Substituting
    :func:`_lexical_normalize` directly for that failure - the fallback this
    function used before this paragraph was added - loses the symlink
    resolution outright and returns the path in its LEXICAL (unfollowed)
    spelling, which then classifies against the wrong mount entry (the local
    filesystem under ``/local`` instead of the NFS share ``cache-link``
    actually points at). So an ENOENT exit from the plain invocation is
    retried, within the same bounded mechanism and the same shared
    ``deadline_s``, with ``realpath -m --`` (``--canonicalize-missing``):
    GNU coreutils resolves every symlink it CAN reach in that mode and only
    leaves the components that do not exist as literal text, which is the
    correct behaviour for a not-yet-created leaf under a real (possibly
    symlinked) ancestor. Measured empirically (not merely per the man page):
    ``realpath -m`` does NOT itself fail ENOENT for a missing component
    (that is precisely what ``-m`` is for), but it also does not reliably
    fail on a genuine symlink loop - it can return exit 0 with the loop's own
    unresolved spelling rather than erroring. That is why the retry is
    ENOENT-gated rather than unconditional: the plain, no-flags call is what
    still catches ELOOP/EACCES/etc and fails those closed below, and ``-m``
    is only ever asked to resolve a path already known (from the first,
    stricter call) to have failed for the benign "missing leaf" reason alone.

    Returns a dict mapping each input path to:

    - its resolved, absolute ``Path`` (following every symlink component;
      GNU ``realpath``'s default mode requires only that all but the LAST
      path component exist, so a not-yet-created leaf - a cache directory
      before its first build - still resolves through any real ancestors and
      exits 0 to do so, without ever needing the ``-m`` retry above);
    - the ``-m`` retry's resolved, absolute ``Path`` when the plain
      invocation exited non-zero with an ENOENT ("No such file or directory")
      stderr AND the retry itself exits 0 - the case above, where a missing
      component sits beneath a symlink the plain call never reached;
    - ``None``, when: the plain invocation's child was still running at the
      deadline (truly wedged); the plain invocation exited non-zero for any
      reason OTHER than ENOENT - permission denied (EACCES), a symlink loop
      (ELOOP), or any other unexpected error whose stderr does not match the
      ENOENT text; or the ENOENT-triggered ``-m`` retry itself timed out or
      exited non-zero. Each of those is a real, current problem with the
      path (or an inability to determine one), and none may be papered over
      with :func:`_lexical_normalize`'s guess at the symlink's own, possibly
      wrong, local spelling - that guess is exactly what reintroduced the
      fail-open bug this function was built to close, through a second code
      path. This is a deliberate sentinel every caller MUST treat as
      "undetermined" - never substitute the lexical path for it - because a
      wedge (or any other unresolved failure) is exactly the case
      :func:`is_path_on_nfs` promises to fail closed on rather than silently
      guess through.
    """
    # Dedupe before spawning: two logical targets that name the identical
    # path (e.g. SSTATE_DIR and DL_DIR configured to the same directory)
    # must not fork a second ``realpath`` child that silently overwrites the
    # first in ``procs`` below - that child would never be polled, reaped on
    # timeout, or have its pipes closed, leaking a subprocess and two fds per
    # duplicate. Every original path (duplicates included) still gets a
    # result via the final broadcast back over ``paths``.
    unique_paths = list(dict.fromkeys(paths))
    deadline = time.monotonic() + deadline_s

    plain_batch = _run_realpath_bounded({path: ["realpath", "--", str(path)] for path in unique_paths}, deadline)

    results: dict[Path, Path | None] = {}
    enoent_retry: list[Path] = []
    for path in unique_paths:
        outcome = plain_batch[path]
        if outcome is None:
            results[path] = None
            continue
        rc, stdout_bytes, stderr_bytes = outcome
        if rc == 0:
            text = stdout_bytes.decode("utf-8", errors="replace").strip()
            results[path] = Path(text) if text else _lexical_normalize(path)
            continue
        stderr = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
        if _ENOENT_TEXT in stderr:
            # Missing component - could be the leaf, or a component past an
            # unresolved symlink prefix. Retry with -m below rather than
            # assuming the lexical spelling is safe to trust.
            enoent_retry.append(path)
        else:
            # Permission denied, a symlink loop, or any other error this
            # module cannot positively identify as "just missing" - a real,
            # current problem with the path. Fail closed to None exactly
            # like the timeout branch above, rather than trusting the
            # symlink's own (possibly wrong) local spelling - the exact
            # fail-open this function exists to close.
            results[path] = None

    if enoent_retry:
        retry_batch = _run_realpath_bounded(
            {path: ["realpath", "-m", "--", str(path)] for path in enoent_retry}, deadline
        )
        for path in enoent_retry:
            outcome = retry_batch[path]
            if outcome is None:
                # Still wedged past the (shared) deadline - fail closed.
                results[path] = None
                continue
            rc, stdout_bytes, _stderr_bytes = outcome
            if rc == 0:
                text = stdout_bytes.decode("utf-8", errors="replace").strip()
                results[path] = Path(text) if text else _lexical_normalize(path)
            else:
                # -m is not expected to fail for a merely-missing component -
                # a non-zero exit here is some other real problem. Fail
                # closed rather than falling back to the lexical guess this
                # function was built to stop trusting.
                results[path] = None

    return {path: results[path] for path in paths}


def _resolve_one_bounded(path: Path, *, deadline_s: float = CACHE_PROBE_DEADLINE_S) -> Path | None:
    """Single-path convenience wrapper over :func:`_resolve_bounded`."""
    return _resolve_bounded([path], deadline_s=deadline_s)[path]


def _mount_entry_for_target(mounts_raw: str, target: Path) -> tuple[str, str, str, str] | None:
    """Longest-prefix ``/proc/mounts`` entry covering an ALREADY-resolved ``target``.

    Returns ``(source, mountpoint, fstype, opts)`` or None when no mountpoint
    covers the path. Sorting by mountpoint length descending makes the most
    specific (longest) prefix win, which resolves bind/overlay mounts to the
    real backing filesystem. When multiple entries share that longest
    mountpoint (a filesystem mounted over an existing trap at the same path -
    e.g. an nfs4 share mounted over its systemd ``autofs`` mountpoint), the
    entry listed LAST in the table wins: ``/proc/mounts`` lists mounts in the
    order the kernel applied them, so the last-listed entry at a given
    mountpoint is the one the kernel currently resolves that path through.

    Pure comparison logic, no I/O: callers that already have a resolved
    target (:func:`_mount_entry_in`'s single-path path, or
    :func:`~bakar.mounts.assess_cache_mounts`'s batch-resolved targets) call
    this directly to avoid re-resolving.
    """
    entries: list[tuple[str, str, str, str]] = []
    for line in mounts_raw.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        source = _decode_mount_field(fields[0])
        mountpoint = _decode_mount_field(fields[1])
        entries.append((source, mountpoint, fields[2], fields[3]))
    # Reverse before the stable sort so entries tied on mountpoint length keep
    # their reversed relative order - the last-listed of a tied group then
    # sorts first, and the loop below returns the first match it finds.
    entries.reverse()
    entries.sort(key=lambda e: len(e[1]), reverse=True)

    for source, mountpoint, fstype, opts in entries:
        try:
            mp = Path(mountpoint)
        except TypeError, ValueError:
            continue
        if target == mp or target.is_relative_to(mp):
            return (source, mountpoint, fstype, opts)
    return None


def _mount_entry_in(mounts_raw: str, path: Path) -> tuple[str, str, str, str] | None:
    """Longest-prefix ``/proc/mounts`` entry covering ``path``.

    Shared by :func:`is_path_on_nfs` (the lock-deletion gate) and
    :func:`~bakar.diagnostics.check_workspace_filesystem` (fstype only).

    ``path`` is resolved with :func:`_resolve_bounded` - a bounded,
    symlink-following child process, not a synchronous ``Path.resolve()`` -
    so a symlinked build directory whose target lives on NFS is classified
    correctly, while a child that hangs past the deadline still leaves this
    function returning ``None`` (undetermined) rather than blocking. See
    :func:`_resolve_bounded` for why every call pays the fork rather than
    trying a cheap symlink pre-check first.
    """
    target = _resolve_one_bounded(path, deadline_s=CACHE_PROBE_DEADLINE_S)
    if target is None:
        return None
    return _mount_entry_for_target(mounts_raw, target)


# NFS mount options that bound negative-lookup (absence) caching. Without one
# of these -- or a low attribute-cache timeout -- a client can report a file as
# still-absent for the full attribute timeout after a peer created it.
# Real kernels normalize "positive" -> "pos" when reporting mount options in
# /proc/mounts (the source of truth this check reads -- NOT /etc/fstab or
# whatever a user passed to `mount`), so "lookupcache=pos" MUST be present or
# every correctly-configured "lookupcache=positive" mount false-WARNs. Keep
# "lookupcache=positive" too, for synthetic option strings built in tests.
_NFS_BOUNDED_LOOKUP_OPTS: frozenset[str] = frozenset({"lookupcache=positive", "lookupcache=pos", "lookupcache=none"})

# An attribute-cache timeout at or below this many seconds counts as bounded.
_NFS_LOW_ACTIMEO_SECONDS = 10

# Attribute-cache timeout options that bound how long a stale absence view can
# survive. ``acdirmax`` is included because directory-entry caching is what
# actually governs negative lookups.
_NFS_ACTIMEO_OPTS: tuple[str, ...] = ("actimeo", "acregmax", "acdirmax")


def is_path_on_nfs(path: Path) -> bool | None:
    """Tri-state: is ``path`` backed by an nfs/nfs4 mount?

    Returns:
        ``True`` - the longest-prefix ``/proc/mounts`` entry covering ``path``
        (last-listed among any sharing that mountpoint) names an nfs/nfs4
        filesystem.

        ``False`` - a covering entry exists and names some other filesystem
        that is not ``autofs``, so the path is CONFIRMED local to this node.

        ``None`` - the filesystem could not be determined: ``/proc/mounts`` is
        unreadable, no entry covers the path, the covering entry is a systemd
        ``autofs`` trap, or the bounded symlink-resolution child never
        returned (a wedged automount ancestor). An autofs mountpoint is a
        placeholder the kernel resolves through to whatever gets auto-mounted
        on first access (frequently NFS on this fleet); it names no real
        filesystem, so it cannot confirm the path is local.

    ``None`` is deliberately distinct from ``False``, and callers MUST treat it
    as shared (fail closed). This helper backs a deletion guard for
    ``bitbake.lock``, not an advisory: a caller that read an undetermined path
    as local would run a node-local PID probe against a PID number owned by a
    peer fleet node and unlink that peer's live lock mid-build.

    To stub the mount table under this function, patch
    ``bakar.mounts._mount_entry_in`` - NOT ``bakar.diagnostics._mount_entry_in``.
    The name is re-exported there for the checks that still read it, so a patch
    aimed at that path resolves, takes no effect here, and leaves this function
    reading the developer's real ``/proc/mounts``. Both lived in ``diagnostics``
    before the split, when either spelling worked.

    ``path`` is resolved through :func:`_mount_entry_in`'s bounded,
    symlink-following child process - this function backs the lock-ownership
    gate's classification (``nfs-safe-lock-clearing`` spec: "never classify
    such a directory as confirmed local"), and a symlinked build directory
    whose target lives on NFS must still classify as NFS, which a purely
    lexical (non-symlink-following) comparison cannot do. A resolution child
    that hangs past its deadline surfaces here as ``None`` (undetermined)
    rather than hanging the gate itself - strictly better than any of the
    three documented answers going missing entirely.
    """
    try:
        mounts_raw = Path("/proc/mounts").read_text()
    except OSError:
        return None
    entry = _mount_entry_in(mounts_raw, path)
    if entry is None:
        return None
    fstype = entry[2]
    if fstype == "autofs":
        return None
    return fstype in _FS_NFS


def _nfs_lookup_cache_bounded(opts: str) -> bool:
    """True when NFS mount ``opts`` bound how long a stale absence view lives.

    Either an explicit ``lookupcache=positive``/``lookupcache=pos``/
    ``lookupcache=none`` (the kernel reports the abbreviated ``pos`` form in
    ``/proc/mounts``, while ``positive`` is what users type in fstab/mount
    options), or an attribute-cache timeout of at most
    :data:`_NFS_LOW_ACTIMEO_SECONDS` seconds. An unparseable timeout value
    counts as unbounded.
    """
    entries = [opt.strip() for opt in opts.split(",")]
    if any(opt in _NFS_BOUNDED_LOOKUP_OPTS for opt in entries):
        return True
    for opt in entries:
        key, _, value = opt.partition("=")
        if key not in _NFS_ACTIMEO_OPTS or not value:
            continue
        try:
            seconds = int(value)
        except ValueError:
            continue
        if seconds <= _NFS_LOW_ACTIMEO_SECONDS:
            return True
    return False


# Active NFS delegations on the build device before a local build is judged
# harmful. A handful is normal churn from a peer that just read a few files;
# tens of thousands means the export has handed out read delegations across the
# whole tree, and every conflicting local open pays a recall.
_NFS_DELEG_WARN_THRESHOLD = 500


def _deleg_counts(locks_raw: str, device: tuple[int, int]) -> tuple[int, int]:
    """Return ``(on_device, total)`` NFS delegation counts from ``/proc/locks`` text.

    ``/proc/locks`` renders a delegation as ``N: DELEG ACTIVE READ <pid>
    <major>:<minor>:<inode> ...`` with the device numbers in HEX (``103:02`` is
    259:2). Lines that do not parse are skipped rather than aborting the count -
    the file is a best-effort diagnostic, not a contract.
    """
    on_device = total = 0
    for line in locks_raw.splitlines():
        fields = line.split()
        if len(fields) < 6 or "DELEG" not in fields:
            continue
        total += 1
        parts = fields[5].split(":")
        if len(parts) < 3:
            continue
        try:
            if (int(parts[0], 16), int(parts[1], 16)) == device:
                on_device += 1
        except ValueError:
            continue
    return on_device, total


CacheMountState = Literal["ready", "unresponsive", "error", "missing"]


@dataclass(frozen=True)
class CacheMountStatus:
    """The readiness assessment for one effective cache directory.

    ``fstype``, ``mountpoint`` and ``source`` come from the ``/proc/mounts``
    entry covering ``path`` (all ``None`` when no entry covers it, or the
    mount table could not be read). ``declared_nfs``/``fstab_source`` come
    from the ``/etc/fstab`` entry covering ``path`` (``fstab_source`` is
    ``None`` when ``declared_nfs`` is False). ``detail`` carries the probe
    child's stripped stderr for ``error``/``missing`` states, and is empty
    otherwise. ``critical`` mirrors the ``critical`` flag the caller passed
    into :func:`assess_cache_mounts` for this target (sstate/downloads are
    critical, a ccache dir is not). It no longer gates :attr:`blocking` - the
    cache-mount-readiness spec's "any effective cache directory" wording
    carries no criticality carve-out, so every unusable target blocks
    uniformly - but it is still stored, since :func:`check_shared_cache_mounts`
    (the separate, older cluster shared-mount validator) and existing tests
    read it directly. Defaults to ``True`` so a caller that builds one without
    specifying it - existing tests among them - keeps the historical default.
    """

    label: str
    path: Path
    state: CacheMountState
    fstype: str | None
    mountpoint: str | None
    source: str | None
    declared_nfs: bool
    fstab_source: str | None
    detail: str
    critical: bool = True

    @property
    def _has_problem(self) -> bool:
        """True when this status is a hard problem.

        Covers: the probe failing outright (``unresponsive``/``error``); a
        directory fstab declares NFS that does not even exist yet
        (``missing`` and ``declared_nfs``); or a directory fstab declares NFS
        that resolved ready but to a non-NFS filesystem (the "automount unit
        dead, bare local directory exposed" case from design.md). A
        ``missing`` directory that fstab does NOT declare NFS is not a
        problem - it may simply not have been created yet.
        """
        if self.state in ("unresponsive", "error"):
            return True
        if self.state == "missing":
            return self.declared_nfs
        if self.state == "ready":
            return self.declared_nfs and self.fstype not in _FS_NFS
        return False

    @property
    def blocking(self) -> bool:
        """True when this status should fail a cache-mount-readiness check.

        Every target with a problem blocks, regardless of ``critical`` - the
        cache-mount-readiness spec's "The cache-mounts doctor check reports
        every cache directory and blocks on unusable shares" requirement
        states unconditionally that the check "SHALL return FAIL at BLOCK
        severity when any effective cache directory is unresponsive or fails
        to mount", with no carve-out for a non-critical (ccache) target.
        """
        return self._has_problem


def _fstab_nfs_entry_for_target(fstab_text: str, target: Path) -> tuple[str, str] | None:
    """Longest-prefix ``/etc/fstab`` NFS entry covering an ALREADY-resolved ``target``.

    Picks the fstab entry with the longest mountpoint covering ``target``
    component-wise across EVERY filesystem type, then returns its
    ``(source, mountpoint)`` only when that entry is ``nfs`` or ``nfs4``. The
    fstype test comes after selection, as in :func:`_mount_entry_for_target`:
    a local mount nested under an NFS share makes the paths beneath it local,
    so the shorter NFS entry must not be reported for them. Blank lines and
    lines whose first non-space character is ``#`` are skipped, matching
    fstab's own comment syntax. Returns ``None`` when no entry covers
    ``target`` or the longest covering entry is not NFS.

    Pure comparison logic, no I/O - see :func:`_mount_entry_for_target`.
    """
    best: tuple[str, str, str] | None = None
    best_len = -1
    for line in fstab_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 3:
            continue
        source, mountpoint, fstype = _decode_mount_field(fields[0]), _decode_mount_field(fields[1]), fields[2]
        try:
            mp = Path(mountpoint)
        except TypeError, ValueError:
            continue
        if target != mp and not target.is_relative_to(mp):
            continue
        if len(mountpoint) > best_len:
            best = (source, mountpoint, fstype)
            best_len = len(mountpoint)
    if best is None or best[2] not in _FS_NFS:
        return None
    return (best[0], best[1])


def fstab_nfs_entry(fstab_text: str, path: Path) -> tuple[str, str] | None:
    """Longest-prefix ``/etc/fstab`` NFS entry covering ``path``.

    ``path`` is resolved with :func:`_resolve_bounded` - the same bounded,
    symlink-following child process :func:`_mount_entry_in` uses - so a
    symlinked cache directory whose target is declared NFS in fstab is still
    reported as declared-NFS. A resolution child that hangs past the deadline
    surfaces here as ``None`` ("no covering NFS entry"), matching this
    function's existing not-covered contract rather than adding a new state;
    :func:`assess_cache_mounts` treats that ambiguity as a probe problem
    (``unresponsive``) instead of trusting a bare ``None`` as "confirmed not
    NFS" for its own blocking decision.
    """
    target = _resolve_one_bounded(path, deadline_s=CACHE_PROBE_DEADLINE_S)
    if target is None:
        return None
    return _fstab_nfs_entry_for_target(fstab_text, target)


def probe_statfs(paths: Sequence[Path], *, deadline_s: float) -> dict[Path, tuple[CacheMountState, str]]:
    """Bounded, concurrent readiness probe for ``paths``.

    Starts one ``stat -f`` child per path, all at once, so the whole call is
    bounded by a single shared ``deadline_s`` rather than by N sequential
    per-path timeouts. ``stat -f`` (rather than a plain ``stat``) forces a real
    ``statfs`` syscall, which is what actually triggers a systemd automount and
    forces an NFS server round trip - a path a plain ``stat``/``os.access``
    would silently leave un-mounted. The child's environment forces
    ``LC_ALL=C``/``LANGUAGE=C`` so its stderr text is locale-independent: a
    localized "No such file or directory" would otherwise be misread as a
    generic error rather than a missing path, and a missing-but-not-declared-
    NFS directory must NOT block (see :meth:`CacheMountStatus.blocking`).

    Polls with ``poll()`` (never ``communicate()``, which would wait on a
    child stuck in an uninterruptible NFS sleep) until every child exits or the
    shared deadline passes. Any child still running past the deadline is sent
    SIGKILL and given exactly one bounded ``wait(timeout=0.5)`` to be reaped;
    a child that does not even respond to SIGKILL within that half-second
    (truly wedged in uninterruptible sleep) is abandoned rather than waited on
    further, and is reported ``unresponsive`` regardless.

    Returns a dict mapping each input path to ``(state, detail)``: ``ready``
    with an empty detail, ``unresponsive`` with an empty detail, ``missing``
    with the child's stripped stderr (which matched the English ENOENT text),
    or ``error`` with the child's stripped stderr (anything else).
    """
    # Dedupe before spawning - see the matching comment in
    # :func:`_resolve_bounded`, which shares this exact leak shape.
    unique_paths = list(dict.fromkeys(paths))
    env = {**os.environ, "LC_ALL": "C", "LANGUAGE": "C"}
    procs: dict[Path, subprocess.Popen[bytes]] = {
        path: subprocess.Popen(
            ["stat", "-f", "-c", "%T", "--", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        for path in unique_paths
    }

    deadline = time.monotonic() + deadline_s
    exit_codes: dict[Path, int] = {}
    pending = set(procs)
    while pending:
        for path in list(pending):
            rc = procs[path].poll()
            if rc is not None:
                exit_codes[path] = rc
                pending.discard(path)
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(0.05)

    results: dict[Path, tuple[CacheMountState, str]] = {}
    for path in pending:
        proc = procs[path]
        proc.kill()
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        results[path] = ("unresponsive", "")

    for path, rc in exit_codes.items():
        proc = procs[path]
        stderr_bytes = proc.stderr.read() if proc.stderr is not None else b""
        stderr = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
        if rc == 0:
            results[path] = ("ready", "")
        elif _ENOENT_TEXT in stderr:
            results[path] = ("missing", stderr)
        else:
            results[path] = ("error", stderr)

    for proc in procs.values():
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()

    return {path: results[path] for path in paths}


def assess_cache_mounts(
    targets: Sequence[tuple[str, Path, bool]],
    *,
    deadline_s: float = CACHE_PROBE_DEADLINE_S,
) -> list[CacheMountStatus]:
    """Probe and classify every ``(label, path, critical)`` cache target.

    Probes all paths concurrently (see :func:`probe_statfs`), and separately
    resolves all paths concurrently (see :func:`_resolve_bounded`) - one batch
    call each, rather than one bounded child per target per lookup. The two
    batches are themselves started concurrently, on separate threads, rather
    than one after the other: each batch owns its own bounded ``deadline_s``
    poll loop already, so running them back-to-back would double the
    worst-case wall-clock cost of this function to two full deadlines when
    both a statfs probe and a symlink resolution are wedged at once. Starting
    ``_resolve_bounded`` on a helper thread while ``probe_statfs`` runs
    synchronously on this one means both sets of child processes are alive
    and being polled for nearly all of the call, so the total cost stays
    bounded by roughly one ``deadline_s`` (plus thread-join overhead) rather
    than two. Threading, not a second process pool, because the two batches
    already do all of their own actual waiting via non-blocking ``poll()`` in
    a subprocess, never inside the GIL - a helper thread only needs to host
    that same non-blocking loop concurrently with this one's.

    Then reads ``/proc/mounts`` and ``/etc/fstab`` exactly once each, a single
    snapshot every target is classified against rather than a table re-read
    per target. An unreadable fstab means no path is treated as NFS-declared;
    an unreadable mount table leaves every ``fstype``/``mountpoint``/``source``
    ``None`` rather than failing the whole assessment. ``critical`` IS stored
    on :class:`CacheMountStatus` (its own ``critical`` field), read directly by
    :func:`check_shared_cache_mounts` and by tests, though it no longer gates
    :attr:`CacheMountStatus.blocking`.

    The ``/proc/mounts``/``/etc/fstab`` lookup runs for every target,
    including one whose probe reported ``unresponsive`` or ``error``: the
    resolution batch already ran to its own deadline before this loop starts,
    so there is no additional hang risk left to defend against by skipping
    them. Running the lookup unconditionally also means a status for an
    unresponsive/errored target still names the mount source or fstab entry
    when one exists, rather than reporting "server unknown" for a share whose
    server is in fact known.

    A target whose symlink resolution itself timed out (:func:`_resolve_bounded`
    returned ``None`` - the child never came back) is reported as
    ``unresponsive`` regardless of what ``probe_statfs`` found for it: the
    resolution hang is its own distinct failure mode (a wedged automount
    ancestor a plain ``stat -f`` on the unresolved path did not happen to
    touch), and it must fail closed exactly like every other probe problem
    rather than silently falling back to an unresolved comparison target.
    """
    paths = [path for _label, path, _critical in targets]

    # See the docstring above: start the resolution batch on a helper thread
    # so its child processes are spawned and polled concurrently with the
    # probe batch's, rather than only after the probe batch's full poll loop
    # has already returned.
    resolved: dict[Path, Path | None] = {}
    resolve_error: BaseException | None = None

    def _run_resolve() -> None:
        nonlocal resolve_error
        try:
            resolved.update(_resolve_bounded(paths, deadline_s=deadline_s))
        except BaseException as exc:  # noqa: BLE001 - re-raised on this thread below
            resolve_error = exc

    resolve_thread = threading.Thread(target=_run_resolve, name="cache-mount-resolve-bounded")
    resolve_thread.start()
    try:
        probe_results = probe_statfs(paths, deadline_s=deadline_s)
    finally:
        resolve_thread.join()
    if resolve_error is not None:
        raise resolve_error

    try:
        mounts_raw: str | None = Path("/proc/mounts").read_text()
    except OSError:
        mounts_raw = None

    try:
        fstab_raw: str | None = Path("/etc/fstab").read_text()
    except OSError:
        fstab_raw = None

    statuses: list[CacheMountStatus] = []
    for label, path, critical in targets:
        state, detail = probe_results[path]
        target = resolved[path]

        if target is None:
            # The bounded resolution child for this path never returned -
            # treat it as a probe failure of its own, same as a wedged
            # `stat -f`, rather than trusting an unresolved lexical
            # comparison target for a mount-table lookup on a
            # lock-deletion-adjacent classification.
            state = "unresponsive"
            detail = detail or f"symlink resolution did not finish within {deadline_s:g}s"
            mount_entry = None
            fstab_entry = None
        else:
            mount_entry = _mount_entry_for_target(mounts_raw, target) if mounts_raw is not None else None
            fstab_entry = _fstab_nfs_entry_for_target(fstab_raw, target) if fstab_raw is not None else None

        source = mount_entry[0] if mount_entry is not None else None
        mountpoint = mount_entry[1] if mount_entry is not None else None
        fstype = mount_entry[2] if mount_entry is not None else None

        declared_nfs = fstab_entry is not None
        fstab_source = fstab_entry[0] if fstab_entry is not None else None

        statuses.append(
            CacheMountStatus(
                label=label,
                path=path,
                state=state,
                fstype=fstype,
                mountpoint=mountpoint,
                source=source,
                declared_nfs=declared_nfs,
                fstab_source=fstab_source,
                detail=detail,
                critical=critical,
            )
        )
    return statuses
