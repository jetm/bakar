"""Tests for the native/cross sstate seed.

The seed is the largest measured build-time lever on this fleet (26.4 min to
9.0 min on PC3, 65%), so the contract under test is mostly about it failing
LOUDLY rather than quietly reverting to a slow build:

* selection takes ``${NATIVELSBSTRING}``-prefixed subtrees wholesale and, inside
  plain hash-prefix dirs, only the native/cross objects bitbake leaves there.
* a target-only object is never seeded, because seeding one would serve a stale
  target artifact to a build that should have rebuilt it.
* ``.siginfo`` sidecars travel with their objects.
* releases get separate seeds, so a scarthgap seed can never be offered to a
  wrynose build.
* a populate run leaves a marker saying what it was built from.

The headline falsifier: an empty or absent source must report
``source_missing`` rather than returning a clean zero, because "nothing to copy"
and "nothing was read" call for opposite responses and a bare 0 conflates them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bakar.sstate_seed import (
    MARKER_NAME,
    is_hash_prefix_dir,
    matches_fallback,
    populate_seed,
    read_seed_marker,
    resolve_seed_for_workspace,
    seed_dir_for,
    seed_mirror_line,
)

if TYPE_CHECKING:
    from pathlib import Path


def _obj(root: Path, rel: str, body: str = "x", *, siginfo: bool = True) -> Path:
    """Create an sstate object at *rel* under *root*, with its sidecar."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    if siginfo:
        path.with_name(path.name + ".siginfo").write_text("sig")
    return path


def test_hash_prefix_dir_recognises_only_two_hex_chars() -> None:
    assert is_hash_prefix_dir("7a")
    assert is_hash_prefix_dir("FF")
    assert not is_hash_prefix_dir("universal")
    assert not is_hash_prefix_dir("cachyos")
    # A three-char or non-hex name is a NATIVELSBSTRING prefix, not a hash dir.
    assert not is_hash_prefix_dir("7ab")
    assert not is_hash_prefix_dir("zz")


def test_fallback_substrings_match_native_but_not_target() -> None:
    assert matches_fallback("sstate:gcc-cross-x86_64:...:do_populate_lic.tgz")
    assert matches_fallback("sstate:clang-native:...:do_populate_lic.tgz")
    assert matches_fallback("sstate:nativesdk-foo:...tgz")
    assert not matches_fallback("sstate:busybox:x86-64:...:do_package.tgz")


def test_prefixed_subtree_is_taken_wholesale(tmp_path: Path) -> None:
    src = tmp_path / "sstate"
    _obj(src, "universal/aa/sstate:quilt-native:do_populate_sysroot.tgz")
    _obj(src, "cachyos/bb/sstate:clang-native:do_compile.tgz")
    dest = tmp_path / "seed"

    result = populate_seed(src, dest)

    assert not result.source_missing
    # two objects plus two sidecars
    assert result.files == 4
    assert (dest / "universal/aa/sstate:quilt-native:do_populate_sysroot.tgz").is_file()
    assert (dest / "cachyos/bb/sstate:clang-native:do_compile.tgz").is_file()


def test_target_object_in_hash_dir_is_not_seeded(tmp_path: Path) -> None:
    """A target object must never enter the seed.

    This is the one that protects correctness rather than speed: the seed is
    offered to every build on the release, so a target artifact in it would be
    restored where the build should have rebuilt it.
    """
    src = tmp_path / "sstate"
    _obj(src, "7a/sstate:busybox:x86-64:do_package.tgz")
    _obj(src, "7a/sstate:gcc-cross-x86_64:do_populate_lic.tgz")
    dest = tmp_path / "seed"

    result = populate_seed(src, dest)

    assert (dest / "7a/sstate:gcc-cross-x86_64:do_populate_lic.tgz").is_file()
    assert not (dest / "7a/sstate:busybox:x86-64:do_package.tgz").exists()
    assert result.files == 2  # the cross object and its sidecar, nothing else


def test_siginfo_sidecar_travels_with_its_object(tmp_path: Path) -> None:
    src = tmp_path / "sstate"
    _obj(src, "7a/sstate:libgcc-cross-x86_64:do_compile.tgz")
    dest = tmp_path / "seed"

    populate_seed(src, dest)

    assert (dest / "7a/sstate:libgcc-cross-x86_64:do_compile.tgz").is_file()
    assert (dest / "7a/sstate:libgcc-cross-x86_64:do_compile.tgz.siginfo").is_file()


def test_orphan_siginfo_is_not_copied_alone(tmp_path: Path) -> None:
    """A bare sidecar carries no object, so seeding it buys a miss and a file."""
    src = tmp_path / "sstate"
    (src / "7a").mkdir(parents=True)
    (src / "7a" / "sstate:foo-native:do_compile.tgz.siginfo").write_text("sig")
    dest = tmp_path / "seed"

    result = populate_seed(src, dest)

    assert result.files == 0


def test_absent_source_reports_missing_not_a_clean_zero(tmp_path: Path) -> None:
    """The headline falsifier: 0 files copied has two causes and they differ."""
    result = populate_seed(tmp_path / "nope", tmp_path / "seed")

    assert result.source_missing is True
    assert result.files == 0


def test_empty_source_is_read_and_reports_zero_not_missing(tmp_path: Path) -> None:
    src = tmp_path / "sstate"
    src.mkdir()
    result = populate_seed(src, tmp_path / "seed")

    assert result.source_missing is False
    assert result.files == 0


def test_repopulate_overwrites_rather_than_skipping(tmp_path: Path) -> None:
    """After a pin bump a stale object must be replaced, not left to shadow."""
    src = tmp_path / "sstate"
    obj = _obj(src, "7a/sstate:foo-native:do_compile.tgz", body="old")
    dest = tmp_path / "seed"
    populate_seed(src, dest)

    obj.write_text("new")
    populate_seed(src, dest)

    assert (dest / "7a/sstate:foo-native:do_compile.tgz").read_text() == "new"


def test_releases_get_separate_seed_directories(tmp_path: Path) -> None:
    scarthgap = seed_dir_for(tmp_path, "scarthgap")
    wrynose = seed_dir_for(tmp_path, "wrynose")

    assert scarthgap != wrynose
    assert scarthgap.name == "scarthgap"


def test_unknown_release_gets_its_own_bucket(tmp_path: Path) -> None:
    """An unnamed release must not share the root with a named one."""
    unknown = seed_dir_for(tmp_path, None)

    assert unknown.name == "_unknown"
    assert unknown != seed_dir_for(tmp_path, "scarthgap")


def test_mirror_line_carries_downloadfilename(tmp_path: Path) -> None:
    """Without ``downloadfilename=PATH`` the mirror is configured and never hits."""
    line = seed_mirror_line(tmp_path / "seed")

    assert line.startswith("file://.* file://")
    assert line.endswith("/PATH;downloadfilename=PATH")


def test_marker_records_what_the_seed_was_built_for(tmp_path: Path) -> None:
    src = tmp_path / "sstate"
    _obj(src, "7a/sstate:foo-native:do_compile.tgz")
    dest = tmp_path / "seed"

    populate_seed(src, dest, release_key="scarthgap")
    marker = read_seed_marker(dest)

    assert marker is not None
    assert marker.release_key == "scarthgap"
    assert marker.files == 2
    assert marker.source_dir == str(src)


def test_marker_absent_reads_as_none(tmp_path: Path) -> None:
    assert read_seed_marker(tmp_path) is None


def test_unparseable_marker_reads_as_none(tmp_path: Path) -> None:
    """A corrupt marker means the seed cannot say what it is - same as having none."""
    (tmp_path / MARKER_NAME).write_text("{not json")

    assert read_seed_marker(tmp_path) is None


def test_marker_with_wrong_value_types_reads_as_none(tmp_path: Path) -> None:
    """Valid JSON with wrong types must not escape as a traceback.

    This one parses fine and only fails inside ``float()``, so catching
    ValueError alone would let a TypeError out of a status command - a corrupt
    marker crashing the very code that exists to report corrupt markers.
    """
    (tmp_path / MARKER_NAME).write_text('{"created": {}, "files": 1}')

    assert read_seed_marker(tmp_path) is None


def test_marker_that_is_not_an_object_reads_as_none(tmp_path: Path) -> None:
    (tmp_path / MARKER_NAME).write_text("[1, 2, 3]")

    assert read_seed_marker(tmp_path) is None


def _oe_core(workspace: Path, corenames: str) -> None:
    """Write the oe-core layer.conf the release key is read from."""
    conf = workspace / "openembedded-core" / "meta" / "conf"
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "layer.conf").write_text(f'LAYERSERIES_CORENAMES = "{corenames}"\n')


def test_workspace_release_selects_the_matching_seed(tmp_path: Path) -> None:
    """The branch-portability contract: the seed follows the workspace's release."""
    workspace = tmp_path / "ws"
    _oe_core(workspace, "scarthgap")

    seed, release_key = resolve_seed_for_workspace(workspace, tmp_path / "sstate")

    assert release_key == "scarthgap"
    assert seed == seed_dir_for(tmp_path / "sstate", "scarthgap")


def test_two_releases_never_share_a_seed(tmp_path: Path) -> None:
    """A scarthgap seed must never be offered to a wrynose build.

    It would not corrupt anything - the hashes simply never match - but it would
    read as a configured seed that silently never hits, which is the failure the
    whole release keying exists to prevent.
    """
    ws_a, ws_b = tmp_path / "a", tmp_path / "b"
    _oe_core(ws_a, "scarthgap")
    _oe_core(ws_b, "wrynose")
    sstate = tmp_path / "sstate"

    seed_a, _ = resolve_seed_for_workspace(ws_a, sstate)
    seed_b, _ = resolve_seed_for_workspace(ws_b, sstate)

    assert seed_a != seed_b


def test_workspace_without_oe_core_resolves_to_the_unknown_bucket(
    tmp_path: Path,
) -> None:
    """Before kas has cloned, the release is unknowable - it must not share a seed."""
    seed, release_key = resolve_seed_for_workspace(tmp_path / "ws", tmp_path / "sstate")

    assert release_key is None
    assert seed.name == "_unknown"
