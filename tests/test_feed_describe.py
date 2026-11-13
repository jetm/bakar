"""Host-side description of a finished build run (``describe_build``)."""

from __future__ import annotations

import calendar
import fcntl
import hashlib
import json
import os
import re
from typing import TYPE_CHECKING

import pytest

from bakar import feed_describe
from bakar.commands import _feed_describe as describe
from tests._feed_world import World, make_rpm

if TYPE_CHECKING:
    from pathlib import Path

RUN_ID = "20260101-120000-4242"
RUN_START = calendar.timegm((2026, 1, 1, 12, 0, 0))
KAS = "/srv/ws/meta-avocado/kas/m.yml"
WORKSPACE = "/srv/ws"
TESTDATA = '{"DISTRO_CODENAME": "2026/edge"}'

needs_unprivileged = pytest.mark.skipif(os.geteuid() == 0, reason="root reads files whatever their mode")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Host:
    """A fake build host: a World tree plus a registered run directory."""

    def __init__(self, tmp_path: Path) -> None:
        self.world = World(tmp_path)
        self.run_dir = tmp_path / "runroot" / "build" / "runs" / RUN_ID
        self.run_dir.mkdir(parents=True)
        self.lock_file = self.run_dir.parent.parent / "bitbake.lock"
        self.probe_result: tuple[str, str | None] = ("free", None)
        self.probed: list[Path] = []
        self.deploy_root = self.world.tmpdir / "deploy"
        testdata = self.deploy_root / "images" / "qemux86-64" / "avocado-qemux86-64.testdata.json"
        testdata.parent.mkdir(parents=True)
        testdata.write_text(TESTDATA)
        self.world.recipe("htop")
        self.write_run()

    def write_run(
        self,
        *,
        terminal: dict[str, object] | None = None,
        family: str = "avocado",
        build_paths: bool = True,
        env: dict[str, str] | None = None,
    ) -> None:
        terminal = {"event": "run_end", "exit_code": 0} if terminal is None else terminal
        events: list[dict[str, object]] = [{"event": "run_start"}]
        if build_paths:
            events.append({"event": "build_paths", "tmpdir": str(self.world.tmpdir), "sstate_dir": "/sstate"})
        events.append({"event": "build_config", "family": family, "machine": "avocado-qemux86-64", "mode": "host"})
        if terminal:
            events.append(terminal)
        (self.run_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        env = {"BAKAR_DISPATCH_KAS": KAS, "BAKAR_DISPATCH_WORKSPACE": WORKSPACE} if env is None else env
        (self.run_dir / "env.txt").write_text("".join(f"{k}={v}\n" for k, v in env.items()))

    def lookup(self, run_id: str) -> Path | None:
        return self.run_dir if run_id == RUN_ID else None

    def probe(self, path: Path) -> tuple[str, str | None]:
        self.probed.append(path)
        return self.probe_result

    def deps(self) -> describe.DescribeDeps:
        return describe.DescribeDeps(run_dir_for_run=self.lookup, lock_probe=self.probe)

    def describe(self, *recipes: str) -> feed_describe.BuildDescription:
        return describe.describe_build(RUN_ID, recipes or ("htop",), deps=self.deps())


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


def test_succeeded_run_document_fields(host: Host) -> None:
    doc = host.describe()
    assert doc.schema_version == feed_describe.SCHEMA_VERSION
    assert doc.run_id == RUN_ID
    assert doc.state == "succeeded"
    assert doc.exit_code == 0
    assert doc.machine == "avocado-qemux86-64"
    assert doc.tmpdir == str(host.world.tmpdir)
    assert doc.deploy_dir == str(host.deploy_root)
    assert doc.live is False
    assert doc.live_reason is None
    assert doc.entries == ()
    assert doc.layers == ()
    assert doc.warnings == ()
    assert host.probed == [host.lock_file]


def test_document_round_trips_through_the_strict_parser(host: Host) -> None:
    doc = host.describe()
    assert feed_describe.parse(feed_describe.encode(doc)) == doc


def test_unknown_run_id_is_refused_naming_it(host: Host) -> None:
    with pytest.raises(describe.DescribeError, match="nope-1"):
        describe.describe_build("nope-1", ("htop",), deps=host.deps())


@pytest.mark.parametrize("family", ["nxp", "ti"])
def test_vendor_family_is_refused_naming_it(host: Host, family: str) -> None:
    host.write_run(family=family)
    with pytest.raises(describe.DescribeError, match=family):
        host.describe()


def test_run_without_build_paths_is_refused(host: Host) -> None:
    host.write_run(build_paths=False)
    with pytest.raises(describe.DescribeError, match="no build output location"):
        host.describe()


@pytest.mark.parametrize("missing", ["BAKAR_DISPATCH_KAS", "BAKAR_DISPATCH_WORKSPACE"])
def test_run_without_dispatch_variable_is_refused_not_described(host: Host, missing: str) -> None:
    env = {"BAKAR_DISPATCH_KAS": KAS, "BAKAR_DISPATCH_WORKSPACE": WORKSPACE}
    del env[missing]
    host.write_run(env=env)
    with pytest.raises(describe.DescribeError, match="predates remote refresh support") as err:
        host.describe()
    assert "dispatched again" in str(err.value)
    assert missing in str(err.value)


def test_run_without_env_file_is_refused(host: Host) -> None:
    (host.run_dir / "env.txt").unlink()
    with pytest.raises(describe.DescribeError, match="predates remote refresh support"):
        host.describe()


def test_missing_repository_map_is_refused_naming_it(host: Host) -> None:
    (host.world.deploy / "avocado-repo.map").unlink()
    with pytest.raises(describe.DescribeError, match=r"avocado-repo\.map") as err:
        host.describe()
    assert str(host.world.deploy) in str(err.value)


def test_recipe_with_no_packages_is_refused_naming_it(host: Host) -> None:
    with pytest.raises(describe.DescribeError, match="ghost"):
        host.describe("htop", "ghost")


@needs_unprivileged
def test_unreadable_file_is_refused_without_a_document(host: Host) -> None:
    rpm = host.world.deploy / "core2_64" / "htop-1.0-r0.1.core2_64.rpm"
    rpm.chmod(0)
    try:
        with pytest.raises(describe.DescribeError, match=re.escape(rpm.name)):
            host.describe()
    finally:
        rpm.chmod(0o644)


def test_stale_map_is_refused_naming_map_and_deploy_directory(host: Host) -> None:
    make_rpm(host.world.deploy, "core2_64", "zlib", mtime=5_000_000)
    with pytest.raises(describe.DescribeError, match=r"avocado-repo\.map") as err:
        host.describe()
    assert str(host.world.deploy) in str(err.value)


def test_current_map_is_described(host: Host) -> None:
    make_rpm(host.world.deploy, "core2_64", "zlib", mtime=2_500_000)
    assert host.describe().state == "succeeded"


def test_packages_written_during_the_run_do_not_make_the_map_stale(host: Host) -> None:
    make_rpm(host.world.deploy, "core2_64", "zlib", mtime=RUN_START + 60)
    assert host.describe().state == "succeeded"


def test_named_recipes_own_packages_newer_than_the_map_are_not_stale(host: Host) -> None:
    make_rpm(host.world.deploy, "core2_64", "htop", version="1.1", mtime=5_000_000)
    assert host.describe().state == "succeeded"


def test_files_are_the_named_packages_the_map_and_testdata_with_size_and_sha(host: Host) -> None:
    host.world.recipe("nano", {"nano": "nano", "nano-doc": "nano-doc"})
    host.world.recipe("curl")
    doc = host.describe("htop", "nano")
    rpm = b"rpm"
    map_bytes = (host.world.deploy / "avocado-repo.map").read_bytes()
    expected = {
        "images/qemux86-64/avocado-qemux86-64.testdata.json": TESTDATA.encode(),
        "rpm/avocado-repo.map": map_bytes,
        "rpm/core2_64/htop-1.0-r0.1.core2_64.rpm": rpm,
        "rpm/core2_64/nano-1.0-r0.1.core2_64.rpm": rpm,
        "rpm/core2_64/nano-doc-1.0-r0.1.core2_64.rpm": rpm,
    }
    assert [f.path for f in doc.files] == sorted(expected)
    for f in doc.files:
        assert (f.size, f.sha256) == (len(expected[f.path]), sha(expected[f.path]))


def test_file_without_chunk_alignment_is_hashed_whole(host: Host) -> None:
    big = os.urandom(describe.CHUNK_SIZE * 2 + 17)
    (host.world.deploy / "core2_64" / "htop-1.0-r0.1.core2_64.rpm").write_bytes(big)
    listed = {f.path: f for f in host.describe().files}["rpm/core2_64/htop-1.0-r0.1.core2_64.rpm"]
    assert (listed.size, listed.sha256) == (len(big), sha(big))


def test_only_the_newest_build_of_a_package_is_listed(host: Host) -> None:
    make_rpm(host.world.deploy, "core2_64", "htop", version="1.0", release="r1.1", mtime=2_000_000)
    paths = [f.path for f in host.describe().files]
    assert "rpm/core2_64/htop-1.0-r1.1.core2_64.rpm" in paths
    assert "rpm/core2_64/htop-1.0-r0.1.core2_64.rpm" not in paths


def test_failed_run_returns_a_document_with_no_files_and_no_entries(host: Host) -> None:
    host.write_run(terminal={"event": "run_error", "exit_code": 3})
    doc = host.describe()
    assert (doc.state, doc.exit_code) == ("failed", 3)
    assert doc.files == ()
    assert doc.entries == ()
    assert doc.live is False


def test_failed_run_is_described_even_when_the_deploy_tree_is_broken(host: Host) -> None:
    host.write_run(terminal={"event": "run_end", "exit_code": 2})
    (host.world.deploy / "avocado-repo.map").unlink()
    assert host.describe("ghost").files == ()


def test_running_run_is_live_with_no_files_and_the_lock_untested(host: Host) -> None:
    host.write_run(terminal={})
    doc = host.describe()
    assert (doc.state, doc.exit_code, doc.live) == ("running", None, True)
    assert doc.live_reason is not None
    assert "running" in doc.live_reason
    assert doc.files == ()
    assert doc.entries == ()
    assert host.probed == []


def test_lock_holder_makes_the_build_live_naming_the_lock(host: Host) -> None:
    host.probe_result = ("held", None)
    doc = host.describe()
    assert doc.live is True
    assert doc.live_reason is not None
    assert str(host.lock_file) in doc.live_reason
    assert doc.files != ()


def test_untestable_lock_means_live_and_says_the_state_was_unknown(host: Host) -> None:
    host.probe_result = ("unknown", "cannot open lock: Permission denied")
    doc = host.describe()
    assert doc.live is True
    assert doc.live_reason is not None
    assert "unknown" in doc.live_reason
    assert "Permission denied" in doc.live_reason


def test_a_real_flock_holder_is_seen_through_the_default_probe(host: Host) -> None:
    host.lock_file.write_text("")
    deps = describe.DescribeDeps(run_dir_for_run=host.lookup)
    with host.lock_file.open("rb") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        held = describe.describe_build(RUN_ID, ("htop",), deps=deps)
    free = describe.describe_build(RUN_ID, ("htop",), deps=deps)
    assert (held.live, free.live) == (True, False)
    assert held.live_reason is not None
    assert str(host.lock_file) in held.live_reason


def test_probe_reports_a_missing_lock_file_as_free_and_creates_nothing(tmp_path: Path) -> None:
    lock = tmp_path / "bitbake.lock"
    assert describe.probe_lock(lock) == ("free", None)
    assert not lock.exists()


def test_probe_releases_its_shared_lock(tmp_path: Path) -> None:
    lock = tmp_path / "bitbake.lock"
    lock.write_text("")
    assert describe.probe_lock(lock) == ("free", None)
    with lock.open("rb") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_probe_reports_a_shared_holder_as_free_and_an_exclusive_one_as_held(tmp_path: Path) -> None:
    lock = tmp_path / "bitbake.lock"
    lock.write_text("")
    with lock.open("rb") as holder:
        fcntl.flock(holder, fcntl.LOCK_SH)
        assert describe.probe_lock(lock) == ("free", None)
        fcntl.flock(holder, fcntl.LOCK_EX)
        assert describe.probe_lock(lock) == ("held", None)


@needs_unprivileged
def test_probe_reports_an_unopenable_lock_file_as_unknown(tmp_path: Path) -> None:
    lock = tmp_path / "bitbake.lock"
    lock.write_text("")
    lock.chmod(0)
    state, detail = describe.probe_lock(lock)
    assert state == "unknown"
    assert detail is not None
    assert str(lock) in detail


def _listing(root: Path) -> dict[str, tuple[int, int, int]]:
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        out[str(p)] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return out


def test_describing_writes_nothing_under_the_host_tree_or_the_run_registry(host: Host, tmp_path: Path) -> None:
    before = _listing(tmp_path)
    describe.describe_build(RUN_ID, ("htop",), deps=describe.DescribeDeps(run_dir_for_run=host.lookup))
    assert _listing(tmp_path) == before
    assert not host.lock_file.exists()
