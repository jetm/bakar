"""Tests for the uninative DL_DIR cache-state checks.

Covers ``uninative-dldir-links`` (the dangling-payload-link repair) and
``uninative-mirror-hit`` (mirror link vs network-fetched regular file).

DESTRUCTIVE-TEST GUARD: ``_uninative_dldir`` resolves DL_DIR as
``os.environ.get("DL_DIR") or cfg.dl_dir`` - the environment wins over the config
object - and ``check_uninative_dldir_links`` calls ``shutil.rmtree`` on entries it
finds. A test that pointed only ``cfg.dl_dir`` at a fixture tree would therefore
run the repair against the operator's real download cache. The module-scope
``_neutralise_cache_env`` fixture below is ``autouse=True`` and unsets both
``DL_DIR`` and ``SSTATE_DIR`` before every test in this file, so no test can
reach the real cache even if it forgets to point DL_DIR anywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar import diagnostics
from bakar.config import BuildConfig
from bakar.diagnostics import Severity, Status

_VERSION = "2.44+r5+g7cba77790f32"
_CHECKSUM = "ab" * 32
_OTHER_CHECKSUM = "cd" * 32
_TARBALL = f"x86_64-nativesdk-libc-{_VERSION}.tar.xz"


@pytest.fixture(autouse=True)
def _neutralise_cache_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset the real cache environment before every test in this module.

    See the module docstring: without this, the repair under test would run
    against the host's real ``DL_DIR``.
    """
    monkeypatch.delenv("DL_DIR", raising=False)
    monkeypatch.delenv("SSTATE_DIR", raising=False)


def _cfg(*, host_mode: bool = True, uninative: bool = True, cluster: bool = False) -> BuildConfig:
    """Return a minimal BuildConfig for the uninative cache-state checks."""
    return BuildConfig(
        workspace=Path("/tmp"),
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        kas_container_image="img:latest",
        host_mode=host_mode,
        uninative=uninative,
        cluster=cluster,
    )


def _patch_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    mirror: Path | None = None,
    checksum: str = _CHECKSUM,
    version: str = _VERSION,
) -> Path:
    """Point the checks at an Arch-family os-release and a fixture fragment.

    The gate (``_uninative_gate``) requires an Arch-family host, so without the
    os-release fixture every assertion below would be vacuous against a SKIP.
    Returns the mirror root the fragment's ``UNINATIVE_URL`` names.
    """
    release = tmp_path / "os-release"
    release.write_text('ID=arch\nID_LIKE=""\n', encoding="utf-8")
    monkeypatch.setattr(diagnostics, "_UNINATIVE_OS_RELEASE", release)

    mirror_root = mirror if mirror is not None else tmp_path / "mirror"
    mirror_root.mkdir(parents=True, exist_ok=True)
    fragment = tmp_path / "uninative.inc"
    fragment.write_text(
        f'UNINATIVE_URL = "file://{mirror_root}/"\n'
        f'UNINATIVE_VERSION:forcevariable = "{version}"\n'
        f'UNINATIVE_CHECKSUM[x86_64] = "{checksum}"\n'
        'UNINATIVE_MAXGLIBCVERSION:forcevariable = "2.44"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(diagnostics, "_UNINATIVE_FRAGMENT", fragment)
    return mirror_root


def _set_dl_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point DL_DIR at a fixture tree and return ``<DL_DIR>/uninative``."""
    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DL_DIR", str(dl_dir))
    return dl_dir / "uninative"


def _entry(dldir: Path, checksum: str) -> Path:
    """Create and return the cache entry directory for ``checksum``."""
    entry = dldir / checksum
    entry.mkdir(parents=True, exist_ok=True)
    return entry


def _linked_payload(entry: Path, mirror_root: Path, *, name: str = _TARBALL) -> Path:
    """Mirror-link a payload into ``entry`` and stamp it done.

    Reproduces ``uninative.bbclass:53-56`` and 80-94: with a ``file://``
    UNINATIVE_URL the fetcher symlinks the cache entry at the mirror file and
    writes the ``.done`` stamp beside it.
    """
    target = mirror_root / name
    target.write_bytes(b"payload")
    link = entry / name
    link.symlink_to(target)
    (entry / f"{name}.done").touch()
    return link


@pytest.mark.unit
def test_repair_preserves_valid_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Only the entry holding a dangling link is removed; a valid sibling survives.

    The dangling entry keeps its ``.done`` stamp, which is what makes
    ``uninative.bbclass:56`` skip the fetch and silently disable uninative, so
    the stamp must go with the entry. The sibling entry for another checksum
    still resolves and must be byte-for-byte intact afterwards - a repair that
    removed the whole ``<DL_DIR>/uninative`` tree would fail here.
    """
    mirror_root = _patch_host(monkeypatch, tmp_path)
    dldir = _set_dl_dir(monkeypatch, tmp_path)

    broken = _entry(dldir, _CHECKSUM)
    broken_link = _linked_payload(broken, mirror_root)
    broken_stamp = broken / f"{_TARBALL}.done"
    # A routine package upgrade deletes the mirror file under the link.
    broken_link.readlink().unlink()
    assert broken_link.is_symlink()
    assert not broken_link.exists()

    other_name = "x86_64-nativesdk-libc-2.43+r1+gdeadbeef0000.tar.xz"
    valid = _entry(dldir, _OTHER_CHECKSUM)
    valid_link = _linked_payload(valid, mirror_root, name=other_name)
    valid_stamp = valid / f"{other_name}.done"

    result = diagnostics.check_uninative_dldir_links(_cfg())

    assert result.name == "uninative-dldir-links"
    assert result.status is Status.FAIL
    # WARN, not BLOCK: the tree is healthy again by the time the check returns,
    # and it runs inside the build pre-flight gate, where a BLOCK would abort
    # the build on a condition this same run just repaired. FAIL still stands so
    # the operator learns a cached artifact was deleted under them.
    assert result.severity is Severity.WARN
    assert str(broken) in result.message
    assert "1 uninative cache entry" in result.message

    assert not broken.exists()
    assert not broken_stamp.exists()

    assert valid.is_dir()
    assert valid_link.is_symlink()
    assert valid_link.exists()
    assert valid_link.read_bytes() == b"payload"
    assert valid_stamp.is_file()
    assert dldir.is_dir()
    assert sorted(p.name for p in dldir.iterdir()) == [_OTHER_CHECKSUM]


@pytest.mark.unit
def test_cluster_mode_reports_dangling_entry_without_removing_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a shared DL_DIR the entry is reported, never deleted.

    The payload link is an absolute path into the writing node's own /usr/share
    mirror, so a peer's entry reads dangling from here while resolving perfectly
    on the node that wrote it. Removing it would destroy a live peer's cache and
    race its in-flight fetch, so cluster mode reports and leaves it in place.
    """
    _patch_host(monkeypatch, tmp_path)
    dldir = _set_dl_dir(monkeypatch, tmp_path)
    entry = _entry(dldir, _CHECKSUM)
    broken_link = entry / _TARBALL
    broken_link.symlink_to(tmp_path / "gone" / _TARBALL)
    stamp = entry / f"{_TARBALL}.done"
    stamp.write_text("")

    result = diagnostics.check_uninative_dldir_links(_cfg(cluster=True))

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "peer" in result.message
    # The whole point: nothing was destroyed.
    assert entry.is_dir()
    assert broken_link.is_symlink()
    assert stamp.is_file()


@pytest.mark.unit
def test_dldir_links_passes_when_all_links_resolve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A cache whose every payload link resolves is left alone and passes."""
    mirror_root = _patch_host(monkeypatch, tmp_path)
    dldir = _set_dl_dir(monkeypatch, tmp_path)
    entry = _entry(dldir, _CHECKSUM)
    link = _linked_payload(entry, mirror_root)

    result = diagnostics.check_uninative_dldir_links(_cfg())

    assert result.status is Status.PASS
    assert link.exists()
    assert (entry / f"{_TARBALL}.done").is_file()


@pytest.mark.unit
def test_dldir_links_absent_tree_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unpopulated cache is a PASS, not a failure: the next build creates it."""
    _patch_host(monkeypatch, tmp_path)
    dldir = _set_dl_dir(monkeypatch, tmp_path)
    assert not dldir.exists()

    result = diagnostics.check_uninative_dldir_links(_cfg())

    assert result.status is Status.PASS
    assert "does not exist yet" in result.message


@pytest.mark.unit
def test_dldir_links_absent_dl_dir_skips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With no DL_DIR in the environment or config there is nothing to inspect."""
    _patch_host(monkeypatch, tmp_path)

    result = diagnostics.check_uninative_dldir_links(_cfg())

    assert result.status is Status.SKIP
    assert result.severity is Severity.INFO
    assert "no DL_DIR resolves" in result.message


@pytest.mark.unit
def test_mirror_hit_reports_mirror_link(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A payload symlinked into the fragment's own mirror root is the PASS case."""
    mirror_root = _patch_host(monkeypatch, tmp_path)
    dldir = _set_dl_dir(monkeypatch, tmp_path)
    _linked_payload(_entry(dldir, _CHECKSUM), mirror_root)

    result = diagnostics.check_uninative_mirror_hit(_cfg())

    assert result.name == "uninative-mirror-hit"
    assert result.status is Status.PASS
    assert str(mirror_root) in result.message


@pytest.mark.unit
def test_mirror_hit_warns_on_network_fetch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A regular file means the payload came over the network - WARN, not BLOCK."""
    mirror_root = _patch_host(monkeypatch, tmp_path)
    dldir = _set_dl_dir(monkeypatch, tmp_path)
    entry = _entry(dldir, _CHECKSUM)
    (entry / _TARBALL).write_bytes(b"payload")

    result = diagnostics.check_uninative_mirror_hit(_cfg())

    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert "regular file" in result.message
    assert str(mirror_root) in result.message


@pytest.mark.unit
def test_mirror_hit_warns_when_link_leaves_mirror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A link resolving outside the mirror root is not the validated payload."""
    _patch_host(monkeypatch, tmp_path)
    dldir = _set_dl_dir(monkeypatch, tmp_path)
    outside = tmp_path / "elsewhere" / _TARBALL
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"payload")
    entry = _entry(dldir, _CHECKSUM)
    (entry / _TARBALL).symlink_to(outside)

    result = diagnostics.check_uninative_mirror_hit(_cfg())

    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert str(outside) in result.message


@pytest.mark.unit
def test_mirror_hit_skips_when_nothing_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No cached payload yet is a SKIP: there is no evidence either way."""
    _patch_host(monkeypatch, tmp_path)
    _set_dl_dir(monkeypatch, tmp_path)

    result = diagnostics.check_uninative_mirror_hit(_cfg())

    assert result.status is Status.SKIP
    assert result.severity is Severity.INFO
    assert "nothing cached at" in result.message


@pytest.mark.unit
def test_mirror_hit_skips_on_dangling_link(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A dangling cached link defers to ``uninative-dldir-links``."""
    mirror_root = _patch_host(monkeypatch, tmp_path)
    dldir = _set_dl_dir(monkeypatch, tmp_path)
    link = _linked_payload(_entry(dldir, _CHECKSUM), mirror_root)
    link.readlink().unlink()

    result = diagnostics.check_uninative_mirror_hit(_cfg())

    assert result.status is Status.SKIP
    assert "dangling link" in result.message


@pytest.mark.unit
def test_mirror_hit_skips_when_dl_dir_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No resolvable DL_DIR leaves the mirror question unanswerable."""
    _patch_host(monkeypatch, tmp_path)

    result = diagnostics.check_uninative_mirror_hit(_cfg())

    assert result.status is Status.SKIP
    assert "no DL_DIR resolves" in result.message


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kwargs", "fragment_needle"),
    [
        ({"uninative": False}, "uninative is off"),
        ({"host_mode": False}, "container build"),
    ],
)
def test_cache_checks_skip_when_gate_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kwargs: dict[str, bool],
    fragment_needle: str,
) -> None:
    """Both checks skip before touching the cache when the gate is closed.

    A closed gate must short-circuit ahead of the repair: an operator who turned
    uninative off has not consented to entries being deleted.
    """
    mirror_root = _patch_host(monkeypatch, tmp_path)
    dldir = _set_dl_dir(monkeypatch, tmp_path)
    entry = _entry(dldir, _CHECKSUM)
    link = _linked_payload(entry, mirror_root)
    link.readlink().unlink()

    cfg = _cfg(**kwargs)
    for check in (diagnostics.check_uninative_dldir_links, diagnostics.check_uninative_mirror_hit):
        result = check(cfg)
        assert result.status is Status.SKIP
        assert fragment_needle in result.message
    assert entry.is_dir()
