"""Read the FLOCK rows of ``/proc/locks`` for one lock file.

The device field of a ``/proc/locks`` row is the superblock device, while
``os.stat().st_dev`` on btrfs is a per-subvolume device, so the two never
match there. The row is therefore matched on the inode number alone.

``flock_rows`` is self-contained (``os`` and ``Path`` only) so a test can
inject its source into a ``python -c`` script with ``inspect.getsource``.
"""

from __future__ import annotations

import os
from pathlib import Path


def flock_rows(lock: str | Path) -> list[tuple[bool, str, int]]:
    """``(waiting, mode, pid)`` for every FLOCK on ``lock``'s inode, read from ``/proc/locks``.

    A lock file that does not exist yet has no rows. The inode is the only key, so a FLOCK on a
    different filesystem that happens to share the inode number would match too; callers that
    need exclusivity also filter on the pid, as the guard test does with its process tree.
    """
    try:
        ino = str(os.stat(lock).st_ino)
    except FileNotFoundError:
        return []
    rows = []
    for line in Path("/proc/locks").read_text().splitlines():
        fields = line.split()
        waiting = len(fields) > 1 and fields[1] == "->"
        if waiting:
            del fields[1]
        if len(fields) >= 6 and fields[1] == "FLOCK" and fields[5].rsplit(":", 1)[-1] == ino:
            rows.append((waiting, fields[3], int(fields[4])))
    return rows
