"""Tests for detached signing of repository metadata, and the publish ordering.

The invariant is one sentence: a live ``repomd.xml`` always has a retrievable
signature beside it. Everything here exists to pin that, including the ugly
cases - because the interesting failure is not "signing broke", it is "signing
broke and the feed carried on serving unsigned metadata that looks fine".

The renderer writes ``repomd.xml`` last within a repodata directory, so the data
files it names always precede it. What it does not do is publish a signature, so
between the render and the signing there is an interval where live metadata has
none. Closing that interval is what ``publish_signed`` is for: it takes the
index out of the served path, signs it, and only then puts it back.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from bakar.feed_publish import (
    SIGNATURE_SUFFIX,
    publish_signed,
    signature_gaps,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _repodata(tmp_path: Path, subpath: str = "target/qemux86-64") -> Path:
    """A rendered repodata dir: data files plus the index that names them."""
    repodata = tmp_path / subpath / "repodata"
    repodata.mkdir(parents=True)
    (repodata / "abc-primary.xml.gz").write_bytes(b"primary")
    (repodata / "repomd.xml").write_text("<repomd/>")
    return repodata


class _Gpg:
    """Stand in for gpg: writes a plausible signature, or fails on demand."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self._fail = fail

    def __call__(self, argv, **kwargs):
        self.calls.append([str(a) for a in argv])
        if self._fail:
            raise subprocess.CalledProcessError(2, argv)
        out = argv[argv.index("--output") + 1]
        with open(out, "w") as fh:
            fh.write("-----BEGIN PGP SIGNATURE-----\nx\n-----END PGP SIGNATURE-----\n")
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_publish_signed_leaves_the_index_beside_its_signature(tmp_path, monkeypatch) -> None:
    """After publishing, both the index and its signature are live."""
    gpg = _Gpg()
    monkeypatch.setattr(subprocess, "run", gpg)
    repodata = _repodata(tmp_path)

    publish_signed(repodata, key="local@example.invalid")

    assert (repodata / "repomd.xml").is_file()
    assert (repodata / f"repomd.xml{SIGNATURE_SUFFIX}").is_file()


def test_publish_signed_uses_a_detached_armored_signature(tmp_path, monkeypatch) -> None:
    """The signature is detached and armored, matching what a dnf client expects."""
    gpg = _Gpg()
    monkeypatch.setattr(subprocess, "run", gpg)
    repodata = _repodata(tmp_path)

    publish_signed(repodata, key="local@example.invalid")

    argv = gpg.calls[0]
    assert "--detach-sign" in argv
    assert "--armor" in argv
    assert argv[argv.index("--local-user") + 1] == "local@example.invalid"


def test_publish_signed_signs_before_the_index_goes_live(tmp_path, monkeypatch) -> None:
    """The signature exists before the index is placed at its served path.

    Asserted by observing the served path at signing time rather than after the
    fact: a check that only runs at the end cannot tell "signed first" from
    "signed second", which is the entire property.
    """
    repodata = _repodata(tmp_path)
    live = repodata / "repomd.xml"
    seen: list[bool] = []

    def gpg(argv, **kwargs):
        # At the moment of signing, the index must NOT be at its served path.
        seen.append(live.exists())
        out = argv[argv.index("--output") + 1]
        with open(out, "w") as fh:
            fh.write("sig\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", gpg)

    publish_signed(repodata, key="local@example.invalid")

    assert seen == [False]


def test_publish_signed_fails_closed_when_signing_fails(tmp_path, monkeypatch) -> None:
    """A signing failure must not leave unsigned metadata live.

    Fail closed deliberately. Restoring the index so the repo keeps working
    would leave live metadata with no signature, which is the exact state the
    invariant forbids - and it would do so silently, because an unsigned repo
    serves perfectly well. An unavailable repository is a loud failure; an
    unsigned one is not.
    """
    monkeypatch.setattr(subprocess, "run", _Gpg(fail=True))
    repodata = _repodata(tmp_path)

    with pytest.raises(subprocess.CalledProcessError):
        publish_signed(repodata, key="local@example.invalid")

    assert not (repodata / "repomd.xml").exists()
    assert not (repodata / f"repomd.xml{SIGNATURE_SUFFIX}").exists()


def test_publish_signed_without_a_key_claims_no_signature(tmp_path, monkeypatch) -> None:
    """With no key the index stays live and unsigned, and says so.

    The forbidden outcome is reporting a signature that does not exist, not
    publishing unsigned. Returning None is how the caller learns which happened.
    """
    gpg = _Gpg()
    monkeypatch.setattr(subprocess, "run", gpg)
    repodata = _repodata(tmp_path)

    result = publish_signed(repodata, key=None)

    assert result is None
    assert (repodata / "repomd.xml").is_file()
    assert not (repodata / f"repomd.xml{SIGNATURE_SUFFIX}").exists()
    assert gpg.calls == []


def test_publish_signed_leaves_no_temporary_files_behind(tmp_path, monkeypatch) -> None:
    """A reader never sees a partial signature, and none is left on disk.

    The signature is written to a temporary name and renamed into place, so the
    served path either has a complete signature or none.
    """
    monkeypatch.setattr(subprocess, "run", _Gpg())
    repodata = _repodata(tmp_path)

    publish_signed(repodata, key="local@example.invalid")

    leftovers = [p.name for p in repodata.iterdir() if ".tmp" in p.name or p.name.startswith(".")]
    assert leftovers == []


def test_signature_gaps_reports_live_metadata_with_no_signature(tmp_path) -> None:
    """The audit names every served index missing its signature."""
    signed = _repodata(tmp_path, "target/signed")
    (signed / f"repomd.xml{SIGNATURE_SUFFIX}").write_text("sig\n")
    unsigned = _repodata(tmp_path, "target/unsigned")

    assert signature_gaps(tmp_path) == [unsigned / "repomd.xml"]


def test_signature_gaps_is_empty_when_everything_is_signed(tmp_path) -> None:
    """A fully signed feed reports no gaps."""
    for sub in ("sdk/all", "target/qemux86-64"):
        repodata = _repodata(tmp_path, sub)
        (repodata / f"repomd.xml{SIGNATURE_SUFFIX}").write_text("sig\n")

    assert signature_gaps(tmp_path) == []


def test_signature_gaps_ignores_a_signature_with_no_index(tmp_path) -> None:
    """An orphan signature is not a gap; a gap is unsigned LIVE metadata.

    Reporting orphans here would conflate a leftover with the security finding
    this audit exists to surface.
    """
    repodata = tmp_path / "target/orphan" / "repodata"
    repodata.mkdir(parents=True)
    (repodata / f"repomd.xml{SIGNATURE_SUFFIX}").write_text("sig\n")

    assert signature_gaps(tmp_path) == []
