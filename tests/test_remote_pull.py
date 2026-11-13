"""Validation of a pulled build description and of the staged tree it produced."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from bakar.feed_describe import BuildDescription, DescribedFile
from bakar.steps import remote_pull
from bakar.steps.remote_pull import (
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    PullRefused,
    check_free_space,
    check_repo_map,
    make_staging,
    pull_files,
    remove_staging,
    validate_description,
    verify_staged_tree,
)

RPM = "rpm/core2_64/busybox-1.36.rpm"
MAP = "rpm/avocado-repo.map"
TESTDATA = "images/qemux86-64/avocado-image.testdata.json"
# Lines exactly as meta-avocado's avocado-repo-map.bbclass writes them.
REPO_MAP = (
    b"core2_64=$releasever/target/qemux86-64/core2_64\n"
    b"aarch64_avocadosdk=$releasever/sdk/qemux86-64\n"
    b"repo=$releasever/sdk/all\n"
)


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


_ABSOLUTE = "is absolute"
_BACKSLASH = "contains a backslash"
_BAD_COMPONENT = "has an empty, '.' or '..' component"


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("/rpm/core2_64/a.rpm", _ABSOLUTE),
        ("rpm/../etc/a.rpm", _BAD_COMPONENT),
        ("../rpm/core2_64/a.rpm", _BAD_COMPONENT),
        ("rpm//core2_64/a.rpm", _BAD_COMPONENT),
        ("rpm/core2_64/a.rpm/", _BAD_COMPONENT),
        ("rpm\\core2_64\\a.rpm", _BACKSLASH),
        ("rpm/core2_64/a\\b.rpm", _BACKSLASH),
        ("rpm/./core2_64/a.rpm", _BAD_COMPONENT),
        ("", "is empty"),
    ],
)
def test_bad_paths_are_refused_naming_the_entry_and_the_reason(path: str, reason: str) -> None:
    with pytest.raises(PullRefused) as err:
        validate_description(_desc_for_paths(path))
    assert str(err.value) == f"described path {path!r} {reason}"


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


# --- pull_files: end to end through the real rsync and a local stand-in for ssh ---

HOST = "builder"
RPM_B = "rpm/core2_64/zlib-1.3.rpm"


@pytest.fixture
def local_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put an ``ssh`` on PATH that drops its options and host and runs the command locally."""
    if shutil.which("rsync") is None:
        pytest.skip("rsync not installed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text('#!/bin/sh\nwhile [ "$1" = "-o" ]; do shift 2; done\nshift\nexec "$@"\n')
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


class _Host:
    """A host deploy directory plus the description of what is in it."""

    def __init__(self, root: Path, contents: dict[str, bytes]) -> None:
        self.deploy = root / "deploy"
        self.contents = contents
        for rel, data in contents.items():
            dest = self.deploy / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

    def desc(self, **changes: object) -> BuildDescription:
        base = _desc([_described(rel, data) for rel, data in self.contents.items()])
        return dataclasses.replace(base, deploy_dir=str(self.deploy), **changes)


@pytest.fixture
def host(tmp_path: Path) -> _Host:
    return _Host(
        tmp_path / "host",
        {RPM: b"busybox-bytes", RPM_B: b"zlib-bytes", MAP: REPO_MAP, TESTDATA: b"{}"},
    )


def _new_staging(tmp_path: Path) -> Path:
    parent = tmp_path / "staging-parent"
    parent.mkdir(exist_ok=True)
    return make_staging(parent)


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_pull_copies_exactly_the_described_files(local_ssh: None, host: _Host, tmp_path: Path) -> None:
    staging = _new_staging(tmp_path)
    desc = host.desc()
    pull_files(HOST, desc, staging, describe_again=lambda: desc)
    assert _tree(staging) == host.contents


def test_pull_does_not_copy_files_the_description_omits(local_ssh: None, host: _Host, tmp_path: Path) -> None:
    (host.deploy / "rpm" / "core2_64" / "extra-1.0.rpm").write_bytes(b"not described")
    (host.deploy / "secret.txt").write_bytes(b"not described")
    staging = _new_staging(tmp_path)
    desc = host.desc()
    pull_files(HOST, desc, staging, describe_again=lambda: desc)
    assert _tree(staging) == host.contents


def test_planted_symlink_in_place_of_an_rpm_is_refused(local_ssh: None, host: _Host, tmp_path: Path) -> None:
    target = tmp_path / "outside.txt"
    target.write_bytes(b"busybox-bytes")
    planted = host.deploy / RPM
    planted.unlink()
    planted.symlink_to(target)
    staging = _new_staging(tmp_path)
    desc = host.desc()
    with pytest.raises(PullRefused, match=r"busybox-1\.36\.rpm"):
        pull_files(HOST, desc, staging, describe_again=lambda: desc)
    assert not staging.exists()


def test_file_that_grew_after_the_description_is_refused(local_ssh: None, host: _Host, tmp_path: Path) -> None:
    desc = host.desc()
    (host.deploy / RPM).write_bytes(b"busybox-bytes-and-more")
    staging = _new_staging(tmp_path)
    with pytest.raises(PullRefused, match=r"busybox-1\.36\.rpm"):
        pull_files(HOST, desc, staging, describe_again=lambda: desc)
    assert not staging.exists()


def test_a_build_that_started_during_the_pull_is_refused_naming_the_signal(
    local_ssh: None, host: _Host, tmp_path: Path
) -> None:
    staging = _new_staging(tmp_path)
    first = host.desc()
    second = host.desc(live=True, live_reason="bitbake lock held by pid 42")
    with pytest.raises(PullRefused, match="bitbake lock held by pid 42"):
        pull_files(HOST, first, staging, describe_again=lambda: second)
    assert not staging.exists()


def test_a_state_other_than_succeeded_in_the_second_description_is_refused(
    local_ssh: None, host: _Host, tmp_path: Path
) -> None:
    staging = _new_staging(tmp_path)
    second = host.desc(state="failed")
    with pytest.raises(PullRefused, match="failed"):
        pull_files(HOST, host.desc(), staging, describe_again=lambda: second)
    assert not staging.exists()


def test_a_checksum_that_differs_in_the_second_description_is_refused_naming_the_file(
    local_ssh: None, host: _Host, tmp_path: Path
) -> None:
    staging = _new_staging(tmp_path)
    first = host.desc()
    changed = tuple(DescribedFile(f.path, f.size, _sha(b"other")) if f.path == RPM_B else f for f in first.files)
    second = dataclasses.replace(first, files=changed)
    with pytest.raises(PullRefused, match=r"zlib-1\.3\.rpm"):
        pull_files(HOST, first, staging, describe_again=lambda: second)
    assert not staging.exists()


def test_a_size_that_differs_in_the_second_description_is_refused_naming_the_file(
    local_ssh: None, host: _Host, tmp_path: Path
) -> None:
    staging = _new_staging(tmp_path)
    first = host.desc()
    changed = tuple(DescribedFile(f.path, f.size + 1, f.sha256) if f.path == MAP else f for f in first.files)
    second = dataclasses.replace(first, files=changed)
    with pytest.raises(PullRefused, match=r"avocado-repo\.map"):
        pull_files(HOST, first, staging, describe_again=lambda: second)
    assert not staging.exists()


def test_an_unreachable_host_is_refused_naming_the_host(
    tmp_path: Path, host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("rsync") is None:
        pytest.skip("rsync not installed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text("#!/bin/sh\necho 'ssh: connect to host builder port 22: No route to host' >&2\nexit 255\n")
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    staging = _new_staging(tmp_path)
    desc = host.desc()
    with pytest.raises(PullRefused) as err:
        pull_files(HOST, desc, staging, describe_again=lambda: desc)
    assert HOST in str(err.value)
    assert "No route to host" in str(err.value)
    assert not staging.exists()


def test_a_host_starting_with_a_hyphen_is_refused_before_any_subprocess(host: _Host, tmp_path: Path) -> None:
    calls: list[object] = []
    staging = _new_staging(tmp_path)
    desc = host.desc()
    with pytest.raises(PullRefused, match="-oProxyCommand"):
        pull_files(
            "-oProxyCommand=x",
            desc,
            staging,
            describe_again=lambda: desc,
            runner=lambda argv, timeout: calls.append(argv) or (0, ""),
        )
    assert calls == []
    assert not staging.exists()


def test_free_space_below_the_need_is_refused_before_copying(host: _Host, tmp_path: Path) -> None:
    calls: list[object] = []
    staging = _new_staging(tmp_path)
    desc = host.desc()
    with pytest.raises(PullRefused, match="free"):
        pull_files(
            HOST,
            desc,
            staging,
            describe_again=lambda: desc,
            runner=lambda argv, timeout: calls.append(argv) or (0, ""),
            disk_usage=_usage(1),
        )
    assert calls == []
    assert not staging.exists()


def test_a_description_with_an_unsafe_path_is_refused_before_copying(host: _Host, tmp_path: Path) -> None:
    calls: list[object] = []
    staging = _new_staging(tmp_path)
    desc = _desc([_described("../escape.rpm")])
    with pytest.raises(PullRefused, match="escape"):
        pull_files(
            HOST,
            desc,
            staging,
            describe_again=lambda: desc,
            runner=lambda argv, timeout: calls.append(argv) or (0, ""),
        )
    assert calls == []
    assert not staging.exists()


def test_runner_timeout_and_oserror_are_refused_naming_the_host(host: _Host, tmp_path: Path) -> None:
    desc = host.desc()

    def timed_out(argv: list[str], timeout: float) -> tuple[int, str]:
        raise subprocess.TimeoutExpired(argv, timeout)

    def no_rsync(argv: list[str], timeout: float) -> tuple[int, str]:
        raise FileNotFoundError("rsync")

    for runner in (timed_out, no_rsync):
        staging = _new_staging(tmp_path)
        with pytest.raises(PullRefused, match=HOST):
            pull_files(HOST, desc, staging, describe_again=lambda: desc, runner=runner)
        assert not staging.exists()


def test_a_failing_describe_again_removes_staging(local_ssh: None, host: _Host, tmp_path: Path) -> None:
    staging = _new_staging(tmp_path)

    def boom() -> BuildDescription:
        raise RuntimeError("host went away")

    with pytest.raises(RuntimeError, match="host went away"):
        pull_files(HOST, host.desc(), staging, describe_again=boom)
    assert not staging.exists()


def test_success_leaves_staging_for_the_caller_and_removes_the_file_list(
    local_ssh: None, host: _Host, tmp_path: Path
) -> None:
    seen: list[list[str]] = []
    staging = _new_staging(tmp_path)
    desc = host.desc()

    def spy(argv: list[str], timeout: float) -> tuple[int, str]:
        seen.append(argv)
        return remote_pull._run_rsync(argv, timeout)

    pull_files(HOST, desc, staging, describe_again=lambda: desc, runner=spy)
    assert staging.is_dir()
    (argv,) = seen
    list_arg = next(a for a in argv if a.startswith("--files-from="))
    list_path = Path(list_arg.split("=", 1)[1])
    assert not list_path.exists()
    assert staging not in list_path.parents


def test_rsync_argv_names_exact_files_with_the_bounded_flags(host: _Host, tmp_path: Path) -> None:
    seen: list[list[str]] = []
    listing: list[bytes] = []
    staging = _new_staging(tmp_path)
    desc = host.desc()

    def spy(argv: list[str], timeout: float) -> tuple[int, str]:
        seen.append(argv)
        listing.append(Path(next(a for a in argv if a.startswith("--files-from=")).split("=", 1)[1]).read_bytes())
        for rel, data in host.contents.items():
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        return 0, ""

    pull_files(HOST, desc, staging, describe_again=lambda: desc, runner=spy)
    (argv,) = seen
    assert argv[0] == "rsync"
    for flag in (
        "--times",
        "--no-links",
        "--no-perms",
        "--no-owner",
        "--no-group",
        "--no-devices",
        "--no-specials",
        "--protect-args",
        "--chmod=Du+rwx,Fu+rw",
        "--timeout=60",
        f"--max-size={MAX_FILE_BYTES}",
        "--from0",
    ):
        assert flag in argv
    ssh_cmd = argv[argv.index("-e") + 1]
    assert "BatchMode=yes" in ssh_cmd
    assert "ConnectTimeout=15" in ssh_cmd
    assert argv[-2:] == [f"{HOST}:{host.deploy}/", f"{staging}/"]
    assert not any(ch in arg for arg in argv[-2:] for ch in "*?[")
    assert listing == [b"\0".join(p.encode() for p in host.contents) + b"\0"]


def test_rerun_after_an_interrupted_pull_matches_an_uninterrupted_pull(
    local_ssh: None, host: _Host, tmp_path: Path
) -> None:
    desc = host.desc()

    def dies_halfway(argv: list[str], timeout: float) -> tuple[int, str]:
        staging_dir = Path(argv[-1])
        first = min(host.contents)
        dest = staging_dir / first
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(host.contents[first])
        raise OSError("connection killed")

    interrupted = _new_staging(tmp_path)
    with pytest.raises(PullRefused):
        pull_files(HOST, desc, interrupted, describe_again=lambda: desc, runner=dies_halfway)
    assert not interrupted.exists()

    rerun = _new_staging(tmp_path)
    pull_files(HOST, desc, rerun, describe_again=lambda: desc)
    clean = _new_staging(tmp_path)
    pull_files(HOST, desc, clean, describe_again=lambda: desc)
    assert _tree(rerun) == _tree(clean) == host.contents


@pytest.mark.parametrize("deploy_dir", ["", "deploy", "~/deploy"])
def test_description_with_a_deploy_directory_that_is_not_absolute_is_refused(deploy_dir: str) -> None:
    desc = dataclasses.replace(_desc_for_paths("rpm/a.rpm"), deploy_dir=deploy_dir)
    with pytest.raises(PullRefused, match="not an absolute path"):
        validate_description(desc)


@pytest.mark.parametrize("deploy_dir", ["/t/de\x00ploy", "/t/a\nb", "/t/\x1b[0m", "/t/a\x7f", "/t/a\rb"])
def test_description_with_a_control_character_in_the_deploy_directory_is_refused(deploy_dir: str) -> None:
    desc = dataclasses.replace(_desc_for_paths(RPM), deploy_dir=deploy_dir)
    with pytest.raises(PullRefused, match="control character") as err:
        validate_description(desc)
    assert repr(deploy_dir) in str(err.value)


# --- make_staging under a setgid parent --------------------------------------


def test_make_staging_accepts_a_setgid_parent(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(parent.stat().st_mode | stat.S_ISGID)
    if not parent.stat().st_mode & stat.S_ISGID:
        pytest.skip("filesystem does not keep the setgid bit")
    staging = make_staging(parent)
    st = staging.lstat()
    assert st.st_mode & 0o777 == 0o700
    assert st.st_mode & stat.S_ISGID


# --- check_repo_map ----------------------------------------------------------


def test_repo_map_with_the_producer_line_shapes_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "avocado-repo.map"
    path.write_bytes(REPO_MAP + b"\n")
    check_repo_map(path)


def test_empty_repo_map_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "avocado-repo.map"
    path.write_bytes(b"")
    check_repo_map(path)


_BAD_MAP_LINES = [
    "core2_64=$(touch x)$releasever/target/q",
    "core2_64=`touch x`$releasever/target/q",
    "repo=$releasever/../../../../home/u/x",
    "core2_64=$releasever/target;touch x",
    'core2_64=$releasever/"target"',
    "core2_64=$releasever/target\r",
    "repo=${HOME}/sdk/all",
    "core2_64=target/qemux86-64/core2_64",
    "core2_64=$releasever",
    "core2_64=$releasever/.hidden",
    "core 2=$releasever/target",
    "repo = $releasever/sdk/all",
]


@pytest.mark.parametrize("line", _BAD_MAP_LINES)
def test_repo_map_line_outside_the_producer_shape_is_refused(tmp_path: Path, line: str) -> None:
    path = tmp_path / "avocado-repo.map"
    path.write_bytes(b"core2_64=$releasever/target/qemux86-64/core2_64\n" + line.encode() + b"\n")
    with pytest.raises(PullRefused) as err:
        check_repo_map(path)
    message = str(err.value)
    assert "line 2" in message
    assert repr(line) in message
    assert "evaluate" in message


def _staging_runner(contents: dict[str, bytes], staging: Path):
    def runner(argv: list[str], timeout: float) -> tuple[int, str]:
        for rel, data in contents.items():
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        return 0, ""

    return runner


@pytest.mark.parametrize("line", _BAD_MAP_LINES)
def test_pull_refuses_a_hostile_repo_map_and_removes_staging(tmp_path: Path, line: str) -> None:
    contents = {RPM: b"busybox-bytes", MAP: line.encode() + b"\n"}
    desc = _desc([_described(rel, data) for rel, data in contents.items()])
    staging = _new_staging(tmp_path)
    with pytest.raises(PullRefused, match="evaluate"):
        pull_files(HOST, desc, staging, describe_again=lambda: desc, runner=_staging_runner(contents, staging))
    assert not staging.exists()


def test_pull_without_a_repo_map_succeeds(tmp_path: Path) -> None:
    contents = {RPM: b"busybox-bytes"}
    desc = _desc([_described(rel, data) for rel, data in contents.items()])
    staging = _new_staging(tmp_path)
    pull_files(HOST, desc, staging, describe_again=lambda: desc, runner=_staging_runner(contents, staging))
    assert _tree(staging) == contents


# --- _run_rsync interrupted -------------------------------------------------


def test_an_interrupt_while_waiting_stops_and_reaps_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    class InterruptedOnce(real_popen):  # type: ignore[misc, valid-type]
        interrupted = False

        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            started.append(self)

        def wait(self, timeout: float | None = None) -> int:
            if not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            return super().wait(timeout)

    monkeypatch.setattr(subprocess, "Popen", InterruptedOnce)
    try:
        with pytest.raises(KeyboardInterrupt):
            remote_pull._run_rsync(["sleep", "30"], 60)
        (proc,) = started
        assert proc.returncode is not None
        with pytest.raises(ChildProcessError):
            os.waitpid(proc.pid, os.WNOHANG)
    finally:
        for proc in started:
            if proc.returncode is None:
                proc.kill()
                real_popen.wait(proc)


# --- remove_staging ----------------------------------------------------------


def test_remove_staging_removes_a_tree_holding_a_read_only_directory(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    locked = staging / "rpm" / "core2_64"
    locked.mkdir(parents=True)
    (locked / "a.rpm").write_bytes(b"x")
    locked.chmod(0o555)
    try:
        assert remove_staging(staging) is None
        assert not staging.exists()
    finally:
        if locked.exists():
            locked.chmod(0o755)


def test_remove_staging_of_a_missing_directory_returns_none(tmp_path: Path) -> None:
    assert remove_staging(tmp_path / "absent") is None
