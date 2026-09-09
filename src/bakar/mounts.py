"""Mount-table primitives: reading ``/proc/mounts`` and judging NFS options.

The cluster answers three questions about a path's backing filesystem - which
mount entry covers it, whether that entry is NFS, and whether the mount's
options bound how long a stale view can survive - plus the ``/proc/locks``
delegation count that shares the same NFS subject. All of it reads only
``pathlib.Path`` and its own constants, so it sits below the diagnostics checks
and the build-stop lock guard that consume it rather than inside either.
"""

from __future__ import annotations

from pathlib import Path

# The NFS subset of _FS_BLOCK. Only these are fatal for the build TMPDIR
# (bitbake's sanity check aborts on TMPDIR over NFS); the rest break only the
# source-layer workspace root, so a local TMPDIR override cannot rescue them.
_FS_NFS: frozenset[str] = frozenset({"nfs", "nfs4"})


def _mount_entry_in(mounts_raw: str, path: Path) -> tuple[str, str, str, str] | None:
    """Longest-prefix ``/proc/mounts`` entry covering ``path``.

    Returns ``(source, mountpoint, fstype, opts)`` or None when no mountpoint
    covers the path. Sorting by mountpoint length descending makes the most
    specific (longest) prefix win, which resolves bind/overlay mounts to the
    real backing filesystem. Shared by :func:`check_workspace_filesystem`
    (fstype only) and :func:`check_shared_cache_mounts` (source + opts too).
    """
    entries: list[tuple[str, str, str, str]] = []
    for line in mounts_raw.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        entries.append((fields[0], fields[1], fields[2], fields[3]))
    entries.sort(key=lambda e: len(e[1]), reverse=True)

    target = path.resolve()
    for source, mountpoint, fstype, opts in entries:
        try:
            mp = Path(mountpoint)
        except TypeError, ValueError:
            continue
        if target == mp or target.is_relative_to(mp):
            return (source, mountpoint, fstype, opts)
    return None


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
        names an nfs/nfs4 filesystem.

        ``False`` - a covering entry exists and names some other filesystem, so
        the path is CONFIRMED local to this node.

        ``None`` - the filesystem could not be determined: ``/proc/mounts`` is
        unreadable, or no entry covers the path.

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
    """
    path = path.resolve(strict=False)
    try:
        mounts_raw = Path("/proc/mounts").read_text()
    except OSError:
        return None
    entry = _mount_entry_in(mounts_raw, path)
    if entry is None:
        return None
    return entry[2] in _FS_NFS


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
