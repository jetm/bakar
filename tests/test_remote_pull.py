"""Validation of a pulled build description and of the staged tree it produced."""

from __future__ import annotations

import hashlib
import os
import stat
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from bakar.feed_describe import BuildDescription, DescribedFile
from bakar.steps import remote_pull
from bakar.steps.remote_pull import (
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    PullRefused,
    check_free_space,
    make_staging,
    validate_description,
    verify_staged_tree,
)

if TYPE_CHECKING:
    from pathlib import Path

RPM = "rpm/core2_64/busybox-1.36.rpm"
MAP = "rpm/avocado-repo.map"
TESTDATA = "images/qemux86-64/avocado-image.testdata.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _desc(files: list[DescribedFile]) -> BuildDescription:
    return BuildDescription(
        schema_version=1,
        run_id="20260101-000000-abcd",
        state="succeeded",
        exit_code=0,
        machine="qemux86-64",
        release="1",
        channel="dev",
        tmpdir="/t",
        deploy_dir="/t/deploy",
        live=False,
        live_reason=None,
        files=tuple(files),
        entries=(),
        layers=(),
        bitbake_commit=None,
        warnings=(),
    )


def _described(path: str, data: bytes = b"payload") -> DescribedFile:
    return DescribedFile(path=path, size=len(data), sha256=_sha(data))


def _desc_for_paths(*paths: str) -> BuildDescription:
    return _desc([_described(p) for p in paths])


def _stage(tmp_path: Path, contents: dict[str, bytes]) -> tuple[Path, BuildDescription]:
    staging = tmp_path / "staging"
    staging.mkdir()
    files = []
    for rel, data in contents.items():
        dest = staging / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        files.append(_described(rel, data))
    return staging, _desc(files)


# --- validate_description: accepted shapes ---------------------------------


@pytest.mark.parametrize("path", [RPM, MAP, TESTDATA, "rpm/all/a_b.c+d~e-1.rpm"])
def test_allowed_shapes_are_accepted(path: str) -> None:
    validate_description(_desc_for_paths(path))


def test_empty_description_is_accepted() -> None:
    validate_description(_desc([]))


def test_constants_have_the_specified_values() -> None:
    assert MAX_FILE_BYTES == 2 * 1024**3
    assert MAX_TOTAL_BYTES == 8 * 1024**3


# --- validate_description: path refusals -----------------------------------


@pytest.mark.parametrize(
    ("path", "why"),
    [
        ("/rpm/core2_64/a.rpm", "absolute"),
        ("rpm/../etc/a.rpm", "dotdot"),
        ("../rpm/core2_64/a.rpm", "leading dotdot"),
        ("rpm//core2_64/a.rpm", "empty component"),
        ("rpm/core2_64/a.rpm/", "trailing slash"),
        ("rpm\\core2_64\\a.rpm", "backslash"),
        ("rpm/core2_64/a\\b.rpm", "backslash in name"),
        ("rpm/./core2_64/a.rpm", "dot component"),
        ("", "empty"),
    ],
)
def test_bad_paths_are_refused_naming_the_entry(path: str, why: str) -> None:
    with pytest.raises(PullRefused) as err:
        validate_description(_desc_for_paths(path))
    assert repr(path) in str(err.value) or path in str(err.value), why


def test_duplicate_entry_is_refused() -> None:
    desc = _desc([_described(RPM), _described(RPM)])
    with pytest.raises(PullRefused, match="repeats") as err:
        validate_description(desc)
    assert RPM in str(err.value)


@pytest.mark.parametrize(
    "path",
    [
        "rpm/core2_64/a.txt",  # wrong suffix
        "rpm/a.rpm",  # missing arch level
        "rpm/core2_64/sub/a.rpm",  # too deep
        "rpm/avocado-repo.map.bak",
        "rpm/other.map",
        "images/qemux86-64/a.json",
        "images/a.testdata.json",
        "images/m/x/a.testdata.json",
        "var/a.rpm",
        "rpm",
        "rpm/core2_64/.rpm",
    ],
)
def test_a_path_matching_no_allowed_shape_is_refused(path: str) -> None:
    with pytest.raises(PullRefused) as err:
        validate_description(_desc_for_paths(path))
    assert path in str(err.value)


@pytest.mark.parametrize(
    "path",
    [
        "rpm/core2_64/a b.rpm",
        "rpm/core2_64/a;b.rpm",
        "rpm/core2_64/a$b.rpm",
        "rpm/core2_64/ä.rpm",
        "rpm/core2_64/a\nb.rpm",
        "rpm/core 2/a.rpm",
    ],
)
def test_characters_outside_the_component_alphabet_are_refused(path: str) -> None:
    with pytest.raises(PullRefused) as err:
        validate_description(_desc_for_paths(path))
    assert path in str(err.value) or repr(path) in str(err.value)


@pytest.mark.parametrize(
    "path",
    ["rpm/.hidden/a.rpm", "rpm/core2_64/.a.rpm", "images/.m/a.testdata.json", "images/m/.a.testdata.json"],
)
def test_a_leading_dot_component_is_refused(path: str) -> None:
    with pytest.raises(PullRefused):
        validate_description(_desc_for_paths(path))


# --- validate_description: caps --------------------------------------------


def test_file_over_the_per_file_cap_is_refused_naming_cap_and_size() -> None:
    desc = _desc([DescribedFile(path=RPM, size=11, sha256=_sha(b""))])
    with pytest.raises(PullRefused) as err:
        validate_description(desc, max_file=10, max_total=1000)
    message = str(err.value)
    assert RPM in message
    assert "10" in message
    assert "11" in message


def test_file_exactly_at_the_cap_is_accepted() -> None:
    desc = _desc([DescribedFile(path=RPM, size=10, sha256=_sha(b""))])
    validate_description(desc, max_file=10, max_total=1000)


def test_total_over_the_cap_is_refused_naming_cap_and_total() -> None:
    desc = _desc(
        [
            DescribedFile(path=RPM, size=6, sha256=_sha(b"")),
            DescribedFile(path=MAP, size=6, sha256=_sha(b"")),
        ]
    )
    with pytest.raises(PullRefused) as err:
        validate_description(desc, max_file=10, max_total=11)
    message = str(err.value)
    assert "11" in message
    assert "12" in message


def test_total_exactly_at_the_cap_is_accepted() -> None:
    desc = _desc(
        [
            DescribedFile(path=RPM, size=6, sha256=_sha(b"")),
            DescribedFile(path=MAP, size=5, sha256=_sha(b"")),
        ]
    )
    validate_description(desc, max_file=10, max_total=11)


# --- verify_staged_tree ----------------------------------------------------


def test_matching_tree_passes(tmp_path: Path) -> None:
    staging, desc = _stage(tmp_path, {RPM: b"rpm-bytes", MAP: b"map", TESTDATA: b"{}"})
    verify_staged_tree(staging, desc)


def test_empty_tree_for_empty_description_passes(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    verify_staged_tree(staging, _desc([]))


def test_staged_symlink_is_refused(tmp_path: Path) -> None:
    staging, desc = _stage(tmp_path, {RPM: b"rpm-bytes"})
    target = tmp_path / "outside.bin"
    target.write_bytes(b"rpm-bytes")
    (staging / RPM).unlink()
    (staging / RPM).symlink_to(target)
    with pytest.raises(PullRefused, match="symbolic link") as err:
        verify_staged_tree(staging, desc)
    assert RPM in str(err.value)


def test_staged_fifo_is_refused(tmp_path: Path) -> None:
    staging, desc = _stage(tmp_path, {RPM: b"rpm-bytes"})
    os.mkfifo(staging / MAP)
    with pytest.raises(PullRefused, match="not a regular file") as err:
        verify_staged_tree(staging, desc)
    assert MAP in str(err.value)


def test_staged_unix_socket_is_refused(tmp_path: Path) -> None:
    import socket

    staging, desc = _stage(tmp_path, {RPM: b"rpm-bytes"})
    sock = socket.socket(socket.AF_UNIX)
    try:
        # A short relative bind path stays under the sun_path length limit.
        cwd = os.getcwd()
        os.chdir(staging / "rpm")
        try:
            sock.bind("s.sock")
        finally:
            os.chdir(cwd)
        with pytest.raises(PullRefused, match="not a regular file") as err:
            verify_staged_tree(staging, desc)
        assert "s.sock" in str(err.value)
    finally:
        sock.close()


def test_hard_link_to_a_file_outside_staging_is_refused(tmp_path: Path) -> None:
    staging, desc = _stage(tmp_path, {RPM: b"rpm-bytes"})
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"rpm-bytes")
    (staging / RPM).unlink()
    os.link(outside, staging / RPM)
    assert (staging / RPM).stat().st_nlink == 2
    with pytest.raises(PullRefused, match="hard link") as err:
        verify_staged_tree(staging, desc)
    assert RPM in str(err.value)


def test_file_that_grew_by_one_byte_is_refused(tmp_path: Path) -> None:
    staging, desc = _stage(tmp_path, {RPM: b"rpm-bytes"})
    with (staging / RPM).open("ab") as fh:
        fh.write(b"x")
    with pytest.raises(PullRefused, match="size") as err:
        verify_staged_tree(staging, desc)
    message = str(err.value)
    assert RPM in message
    assert str(len(b"rpm-bytes")) in message
    assert str(len(b"rpm-bytes") + 1) in message


def test_wrong_checksum_with_the_same_size_is_refused(tmp_path: Path) -> None:
    staging, desc = _stage(tmp_path, {RPM: b"rpm-bytes"})
    (staging / RPM).write_bytes(b"rpm-BYTES")
    with pytest.raises(PullRefused, match="sha256") as err:
        verify_staged_tree(staging, desc)
    assert RPM in str(err.value)


def test_described_file_missing_from_staging_is_refused(tmp_path: Path) -> None:
    staging, desc = _stage(tmp_path, {RPM: b"rpm-bytes", MAP: b"map"})
    (staging / MAP).unlink()
    with pytest.raises(PullRefused, match="missing") as err:
        verify_staged_tree(staging, desc)
    assert MAP in str(err.value)


def test_extra_undescribed_file_is_refused(tmp_path: Path) -> None:
    staging, desc = _stage(tmp_path, {RPM: b"rpm-bytes"})
    (staging / "rpm" / "core2_64" / "extra.rpm").write_bytes(b"e")
    with pytest.raises(PullRefused, match="not in the description") as err:
        verify_staged_tree(staging, desc)
    assert "extra.rpm" in str(err.value)


def test_undescribed_empty_directory_is_refused(tmp_path: Path) -> None:
    staging, desc = _stage(tmp_path, {RPM: b"rpm-bytes"})
    (staging / "rpm" / "empty-arch").mkdir()
    with pytest.raises(PullRefused, match="not in the description") as err:
        verify_staged_tree(staging, desc)
    assert "empty-arch" in str(err.value)


def test_symlinked_parent_directory_escape_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "core2_64").mkdir(parents=True)
    (outside / "core2_64" / "busybox-1.36.rpm").write_bytes(b"rpm-bytes")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "rpm").symlink_to(outside, target_is_directory=True)
    desc = _desc([_described(RPM, b"rpm-bytes")])
    with pytest.raises(PullRefused) as err:
        verify_staged_tree(staging, desc)
    assert "rpm" in str(err.value)


def test_resolved_path_outside_staging_is_refused_even_if_the_walk_is_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The containment check is a second, independent guard: when the entry-type
    # test is blind to a link, resolve() still places the file outside staging.
    outside = tmp_path / "outside"
    (outside / "core2_64").mkdir(parents=True)
    (outside / "core2_64" / "busybox-1.36.rpm").write_bytes(b"rpm-bytes")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "rpm").symlink_to(outside, target_is_directory=True)
    desc = _desc([_described(RPM, b"rpm-bytes")])
    monkeypatch.setattr(remote_pull, "_walk_staging", lambda root: ({RPM: staging / RPM}, set()))
    with pytest.raises(PullRefused, match="outside") as err:
        verify_staged_tree(staging, desc)
    assert RPM in str(err.value)


def test_staging_root_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(PullRefused, match="symbolic link"):
        verify_staged_tree(link, _desc([]))


def test_chunked_hash_handles_files_larger_than_one_chunk(tmp_path: Path) -> None:
    data = b"a" * (1024 * 1024 + 17)
    staging, desc = _stage(tmp_path, {RPM: data})
    verify_staged_tree(staging, desc)
    (staging / RPM).write_bytes(b"a" * (1024 * 1024) + b"b" * 17)
    with pytest.raises(PullRefused, match="sha256"):
        verify_staged_tree(staging, desc)


# --- make_staging ----------------------------------------------------------


def test_make_staging_creates_a_0700_directory_owned_by_the_caller(tmp_path: Path) -> None:
    staging = make_staging(tmp_path)
    st = staging.lstat()
    assert stat.S_ISDIR(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o700
    assert st.st_uid == os.geteuid()
    assert staging.parent == tmp_path
    assert list(staging.iterdir()) == []


def test_make_staging_mode_is_0700_under_a_permissive_umask(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        staging = make_staging(tmp_path)
    finally:
        os.umask(old)
    assert stat.S_IMODE(staging.lstat().st_mode) == 0o700


def test_make_staging_returns_a_fresh_directory_each_call(tmp_path: Path) -> None:
    first = make_staging(tmp_path)
    second = make_staging(tmp_path)
    assert first != second
    assert first.is_dir()
    assert second.is_dir()


def test_make_staging_refuses_a_symlink_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(PullRefused, match="symbolic link"):
        make_staging(link)
    assert list(real.iterdir()) == []


def test_make_staging_refuses_a_missing_parent(tmp_path: Path) -> None:
    with pytest.raises(PullRefused, match="does not exist"):
        make_staging(tmp_path / "nope")


def test_make_staging_refuses_a_parent_that_is_a_file(tmp_path: Path) -> None:
    plain = tmp_path / "file"
    plain.write_text("x")
    with pytest.raises(PullRefused, match="not a directory"):
        make_staging(plain)


def test_make_staging_refuses_and_removes_a_directory_owned_by_someone_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(PullRefused, match="owned"):
        make_staging(tmp_path)
    assert list(tmp_path.iterdir()) == []


# --- check_free_space ------------------------------------------------------


def _usage(free: int):
    return lambda path: SimpleNamespace(total=free * 10, used=0, free=free)


def test_free_space_below_total_plus_ten_percent_is_refused_naming_both(tmp_path: Path) -> None:
    desc = _desc([DescribedFile(path=RPM, size=1000, sha256=_sha(b""))])
    with pytest.raises(PullRefused) as err:
        check_free_space(tmp_path, desc, disk_usage=_usage(1099))
    message = str(err.value)
    assert "1099" in message
    assert "1100" in message


def test_free_space_exactly_total_plus_ten_percent_passes(tmp_path: Path) -> None:
    desc = _desc([DescribedFile(path=RPM, size=1000, sha256=_sha(b""))])
    check_free_space(tmp_path, desc, disk_usage=_usage(1100))


def test_free_space_check_asks_about_the_staging_directory(tmp_path: Path) -> None:
    seen: list[object] = []

    def usage(path: object) -> SimpleNamespace:
        seen.append(path)
        return SimpleNamespace(total=0, used=0, free=10**9)

    check_free_space(tmp_path, _desc([]), disk_usage=usage)
    assert seen == [tmp_path]
