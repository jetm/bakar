"""Detached signing of repository metadata, and the ordering that makes it mean something.

A signature is only useful if live metadata can never be fetched without it.
That is an ordering property, not a cryptographic one, and it is the half that
gets skipped: signing after publication leaves an interval in which a client
fetches an index no signature covers, and nothing about that interval is visible
afterwards.

The renderer already does its half. ``write_repodata`` emits the data files
first and ``repomd.xml`` last, so the index never names a file that is not
there. What it does not do is publish a signature, so this module supplies the
missing step - and supplies it in the only order that closes the interval:

1. take ``repomd.xml`` out of the served path,
2. sign it and rename the signature into place,
3. put the index back.

Step 3 is the flip, and it is a rename, so the index appears atomically and
never before its signature. On a first publish there is no interval at all. On
a re-publish the index is briefly absent, which is a 404 - a loud, transient
failure that a client retries. That is the deliberate trade: an unavailable
repository announces itself, an unsigned one does not.

Signing FAILS CLOSED for the same reason. If gpg fails, the index is left out of
the served path rather than restored, because restoring it would produce exactly
the state the invariant forbids and would do it silently. The caller sees the
exception; the operator sees a repository that is down rather than one that is
quietly unverifiable.

Production's renderer carries this as a TODO and has never implemented it, which
is why the local feed leads it: the ordering above is cheap to demonstrate and
the remaining production question is key custody, not sequencing.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# What a dnf client looks for beside repomd.xml when repo_gpgcheck is on.
SIGNATURE_SUFFIX = ".asc"

_INDEX = "repomd.xml"

# The index rests here while it is being signed. Leading dot so a static file
# server configured to hide dotfiles does not serve a half-published index, and
# a name that cannot collide with createrepo_c's hash-prefixed output.
_PENDING = ".repomd.xml.pending"

_SIGNATURE_TMP = ".repomd.xml.asc.tmp"


def sign_detached(target: Path, *, key: str, gpg: str = "gpg") -> Path:
    """Write a detached, armored signature for ``target`` and return its path.

    Written to a temporary name and renamed into place, so a reader sees a
    complete signature or none - never a truncated one that fails verification
    and looks like tampering.
    """
    signature = target.with_name(target.name + SIGNATURE_SUFFIX)
    tmp = target.with_name(_SIGNATURE_TMP)
    subprocess.run(
        [
            gpg,
            "--batch",
            "--yes",
            "--detach-sign",
            "--armor",
            "--local-user",
            key,
            "--output",
            str(tmp),
            str(target),
        ],
        check=True,
    )
    tmp.replace(signature)
    return signature


def publish_signed(repodata: Path, *, key: str | None, gpg: str = "gpg") -> Path | None:
    """Publish ``repodata``'s index so it is never live without its signature.

    Returns the signature path, or None when no key was supplied - which is how
    the caller distinguishes "published signed" from "published unsigned". It
    never returns a path for a signature that was not written, because a caller
    reporting a signature that does not exist is worse than one reporting none.

    Raises:
        CalledProcessError: signing failed. The index is deliberately left out
            of the served path; see the module docstring on failing closed.
    """
    index = repodata / _INDEX
    if key is None:
        return None
    if not index.is_file():
        return None

    pending = repodata / _PENDING
    index.replace(pending)
    signature = sign_detached(pending, key=key, gpg=gpg)
    # Name the signature for the index, not for the staging file it was made over.
    signature = signature.replace(repodata / (_INDEX + SIGNATURE_SUFFIX))
    pending.replace(index)
    return signature


def signature_gaps(feed_root: Path) -> list[Path]:
    """Return every live index under ``feed_root`` that has no signature.

    The falsifiable form of this module's invariant, and the check worth running
    over a whole feed rather than trusting that every publish took the signed
    path. An orphan signature with no index is not reported: that is a leftover,
    and conflating it with unsigned live metadata would bury the finding.
    """
    return [
        index
        for index in sorted(feed_root.rglob(f"repodata/{_INDEX}"))
        if not index.with_name(_INDEX + SIGNATURE_SUFFIX).is_file()
    ]
