"""Host-side description of a finished build run (``describe_build``)."""

from __future__ import annotations

import calendar
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from bakar import feed, feed_describe, run_discovery
from bakar.commands import _app as app_state
from bakar.commands import _feed_describe as describe
from bakar.commands import _feed_refresh as refresh
from tests._feed_world import HASH, World, make_rpm

RUN_ID = "20260101-120000-4242"
RUN_START = calendar.timegm((2026, 1, 1, 12, 0, 0))
KAS = "/srv/ws/meta-avocado/kas/m.yml"
WORKSPACE = "/srv/ws"
TESTDATA = '{"DISTRO_CODENAME": "2026/edge"}'
FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
# The layer the fake recipes live in; a bblayers.conf must name it for the recipes to be found, and it is no checkout.
RECIPE_LAYER = feed_describe.LayerCommit(repo="meta-oe", commit=None)

needs_unprivileged = pytest.mark.skipif(os.geteuid() == 0, reason="root reads files whatever their mode")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_repo(path: Path, *, message: str = "init") -> str:
    """Make ``path`` a git checkout with one empty commit and return its full HEAD."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    ident = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", *ident, "init", "-q", str(path)], check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", *ident, "-C", str(path), "commit", "-q", "--allow-empty", "-m", message],
        check=True,
        capture_output=True,
        env=env,
    )
    out = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, env=env
    )
    return out.stdout.strip()


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
        ws = self.world.workspace
        self.bblayers = ws / "build-qemux86-64" / "build" / "conf" / "bblayers.conf"
        # The fake cfg stands in for what the real resolution returns for the recorded workspace and kas file.
        self.world.cfg.bblayers_conf = self.bblayers  # type: ignore[attr-defined]
        self.world.cfg.bsp_bitbake_path = tmp_path / "no-bitbake"  # type: ignore[attr-defined]
        self.resolved: list[tuple[Path, Path, str | None, str | None]] = []
        self.provider_calls: list[list[str]] = []
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
        mode: str | None = "host",
    ) -> None:
        terminal = {"event": "run_end", "exit_code": 0} if terminal is None else terminal
        events: list[dict[str, object]] = [{"event": "run_start"}]
        if build_paths:
            events.append({"event": "build_paths", "tmpdir": str(self.world.tmpdir), "sstate_dir": "/sstate"})
        config: dict[str, object] = {"event": "build_config", "family": family, "machine": "avocado-qemux86-64"}
        if mode is not None:
            config["mode"] = mode
        events.append(config)
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

    def resolve_cfg(self, workspace: Path, kas_yaml: Path, *, mode: str | None, build_dir: str | None) -> object:
        self.resolved.append((workspace, kas_yaml, mode, build_dir))
        return self.world.cfg

    def resolve_provider(self, _cfg: object, _kas: Path, recipes: list[str]) -> None:
        self.provider_calls.append(list(recipes))

    def deps(self) -> describe.DescribeDeps:
        return describe.DescribeDeps(
            run_dir_for_run=self.lookup,
            lock_probe=self.probe,
            resolve_cfg=self.resolve_cfg,  # type: ignore[arg-type]
            resolve_provider=self.resolve_provider,
        )

    def layer_conf(self, *dirs: Path) -> None:
        """Write a bblayers.conf naming ``dirs`` and the layer that holds the fake recipes, as absolute paths."""
        self.bblayers.parent.mkdir(parents=True, exist_ok=True)
        body = " ".join(str(d) for d in (*dirs, self.world.workspace / "sources" / "meta-oe"))
        self.bblayers.write_text(f'BBLAYERS ?= " {body} "\n')

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
    assert [e.recipe for e in doc.entries] == ["htop"]
    assert doc.layers == ()
    assert doc.warnings == ()
    assert (doc.release, doc.channel) == ("2026", "edge")
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
    deps = dataclasses.replace(host.deps(), lock_probe=describe.probe_lock)
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
    deps = dataclasses.replace(host.deps(), lock_probe=describe.probe_lock)
    describe.describe_build(RUN_ID, ("htop",), deps=deps)
    assert _listing(tmp_path) == before
    assert not host.lock_file.exists()


# --- recipe entries, layer commits, release and channel -------------------------------------------------------


@pytest.fixture
def local_refresh(host: Host, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Run the real local ``run_refresh`` on the host's tree; return the result and the entries it wrote."""
    w = host.world
    monkeypatch.setattr(run_discovery, "live_workspace_runs", lambda _p, **_k: [])
    monkeypatch.setattr(refresh.feed_ops, "scripts_dir", lambda _y: tmp_path / "scripts")

    def fake_sync(_cfg: object, *, publish: feed.FeedPublish, feed_root: Path, stage_root: Path, **_kw: object) -> dict:
        chan = feed.channel_root(feed_root, release=publish.release, channel=publish.channel)
        staged = feed.channel_root(stage_root, release=publish.release, channel=publish.channel)
        for rpm in publish.deploy_dir.rglob("*.rpm"):
            dest = staged / "target" / "qemux86-64" / rpm.parent.name / rpm.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rpm, dest)
            pooled = chan / "_pkgs" / "09" / f"{rpm.name}.rpm"
            pooled.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rpm, pooled)
        (chan / "target" / "qemux86-64" / "repodata").mkdir(parents=True, exist_ok=True)
        (chan / "target" / "qemux86-64" / "repodata" / "pooled-primary.xml.gz").write_bytes(b"pooled")
        return {"snapshot": None, "channel_root": chan, "repos": ["target/qemux86-64"]}

    def fake_createrepo(repo: Path) -> None:
        hrefs = "".join(f'<location href="{p.relative_to(repo)}"/>' for p in sorted(repo.rglob("*.rpm")))
        (repo / "repodata").mkdir(exist_ok=True)
        (repo / "repodata" / "primary.xml").write_text(hrefs)

    monkeypatch.setattr(refresh.feed, "sync", fake_sync)
    monkeypatch.setattr(refresh, "_createrepo", fake_createrepo)

    def run(*recipes: str) -> tuple[refresh.RefreshResult, list[dict[str, object]]]:
        req = dataclasses.replace(w.req, recipes=recipes or ("htop",))
        result = refresh.run_refresh(req, builder=w.builder, resolve_provider=host.resolve_provider)  # type: ignore[arg-type]
        chan = feed.channel_root(req.delta_root, release=req.release, channel=req.channel)
        return result, json.loads((chan / "recipe-map.json").read_text())["entries"]

    return run


def test_entries_equals_local_refresh(host: Host, local_refresh) -> None:
    """A1: the host's entries are the ones a local refresh of the same tree writes."""
    git_repo(host.world.workspace / "sources" / "meta-oe")
    host.world.recipe("glibc", {"glibc": "libc6", "glibc-dev": "libc6-dev"}, layer="meta-oe")
    host.world.stamp("htop")
    host.world.stamp("glibc")

    result, written = local_refresh("htop", "glibc")
    doc = host.describe("htop", "glibc")

    assert [asdict(e) for e in doc.entries] == written
    assert [e["recipe"] for e in written] == ["htop", "glibc"]
    # The comparison is not vacuous: the cache-key fields are populated.
    assert written[0]["task_hash"] == HASH
    assert written[0]["layer_commit"]
    assert doc.warnings == result.warnings == ()


def test_entry_with_an_unreadable_task_hash_is_null_and_keeps_the_other_fields(host: Host, local_refresh) -> None:
    git_repo(host.world.workspace / "sources" / "meta-oe")

    _result, written = local_refresh("htop")
    (entry,) = host.describe().entries

    assert asdict(entry) == written[0]
    assert entry.task_hash is None
    assert (entry.recipe, entry.packages, entry.layer, entry.pv, entry.release) == (
        "htop",
        ["htop"],
        "meta-oe",
        "1.0",
        "2026",
    )
    assert entry.layer_commit


def test_recipe_provided_by_two_layers_carries_the_local_refresh_warning(host: Host, local_refresh) -> None:
    host.world.recipe("htop", layer="meta-extra")

    result, written = local_refresh("htop")
    doc = host.describe()

    assert doc.warnings == result.warnings
    assert len(doc.warnings) == 1
    assert "recipe htop is provided by layers meta-extra, meta-oe" in doc.warnings[0]
    assert [asdict(e) for e in doc.entries] == written
    assert host.provider_calls == [["htop"], ["htop"]]


def test_provider_lookup_failure_is_a_warning_not_a_refusal(host: Host) -> None:
    host.world.recipe("htop", layer="meta-extra")
    deps = dataclasses.replace(host.deps(), resolve_provider=lambda *_: (_ for _ in ()).throw(RuntimeError("kas down")))
    doc = describe.describe_build(RUN_ID, ("htop",), deps=deps)
    assert len(doc.warnings) == 2
    assert "could not ask bitbake which layer built htop: kas down" in doc.warnings[0]
    assert doc.entries[0].layer_commit is None


def test_release_and_channel_come_from_the_image_testdata(host: Host) -> None:
    (host.deploy_root / "images" / "qemux86-64" / "avocado-qemux86-64.testdata.json").write_text(
        '{"DISTRO_CODENAME": "2027/stable"}'
    )
    doc = host.describe()
    assert (doc.release, doc.channel) == ("2027", "stable")
    assert doc.entries[0].release == "2027"


def test_build_declaring_no_release_leaves_them_empty_and_entries_use_the_default(host: Host) -> None:
    (host.deploy_root / "images" / "qemux86-64" / "avocado-qemux86-64.testdata.json").unlink()
    doc = host.describe()
    assert (doc.release, doc.channel) == ("", "")
    assert doc.entries[0].release == feed.DEFAULT_RELEASE


def test_layer_directory_inside_a_repository_is_named_by_the_repository(host: Host) -> None:
    ws = host.world.workspace
    head = git_repo(ws / "sources" / "meta-openembedded")
    layer = ws / "sources" / "meta-openembedded" / "meta-oe"
    layer.mkdir()
    host.layer_conf(layer)

    doc = host.describe()

    assert doc.layers == (feed_describe.LayerCommit(repo="meta-openembedded", commit=head), RECIPE_LAYER)
    assert FULL_COMMIT.fullmatch(head)


def test_layer_directory_named_for_its_kas_key_reports_its_commit(host: Host) -> None:
    ws = host.world.workspace
    head = git_repo(ws / "meta-avocado")
    host.layer_conf(ws / "meta-avocado")
    assert host.describe().layers == (feed_describe.LayerCommit(repo="meta-avocado", commit=head), RECIPE_LAYER)


def test_side_checkout_is_named_by_its_parent_and_reports_its_own_commit_not_the_clones(host: Host) -> None:
    ws = host.world.workspace
    clone_head = git_repo(ws / "meta-avocado", message="clone")
    side = ws / ".bakar" / "pins" / "meta-avocado" / "0123456789ab"
    side_head = git_repo(side, message="pinned")
    (side / "meta-avocado").mkdir()
    assert side_head != clone_head
    host.layer_conf(side / "meta-avocado")

    doc = host.describe()

    assert doc.layers == (feed_describe.LayerCommit(repo="meta-avocado", commit=side_head), RECIPE_LAYER)


def test_directory_that_is_not_a_checkout_reports_a_null_commit(host: Host) -> None:
    ws = host.world.workspace
    plain = ws / "plain" / "meta-x"
    plain.mkdir(parents=True)
    host.layer_conf(plain)

    doc = host.describe()

    assert doc.layers == (feed_describe.LayerCommit(repo="meta-x", commit=None), RECIPE_LAYER)


def test_two_layers_of_one_repository_are_reported_once(host: Host) -> None:
    ws = host.world.workspace
    head = git_repo(ws / "sources" / "meta-openembedded")
    layers = [ws / "sources" / "meta-openembedded" / n for n in ("meta-oe", "meta-python")]
    for layer in layers:
        layer.mkdir()
    host.layer_conf(*layers)
    assert host.describe().layers == (feed_describe.LayerCommit(repo="meta-openembedded", commit=head), RECIPE_LAYER)


def test_bitbake_commit_is_the_full_commit_of_the_bitbake_directory(host: Host, tmp_path: Path) -> None:
    bitbake = tmp_path / "bitbake"
    (bitbake / "lib" / "bb").mkdir(parents=True)
    (bitbake / "lib" / "bb" / "__init__.py").write_text('__version__ = "2.8.0"\n')
    head = git_repo(bitbake)
    host.world.cfg.bsp_bitbake_path = bitbake  # type: ignore[attr-defined]
    doc = host.describe()
    assert doc.bitbake_commit == head
    assert FULL_COMMIT.fullmatch(head)


def test_missing_bitbake_directory_is_a_null_commit(host: Host) -> None:
    assert host.describe().bitbake_commit is None


def test_a_build_with_no_layer_configuration_reports_no_layers(host: Host) -> None:
    assert not host.bblayers.exists()
    assert host.describe().layers == ()


def test_described_document_with_layers_round_trips_the_strict_parser(host: Host) -> None:
    ws = host.world.workspace
    git_repo(ws / "meta-avocado")
    host.layer_conf(ws / "meta-avocado")
    doc = host.describe()
    assert feed_describe.parse(feed_describe.encode(doc)) == doc


def test_describing_with_layers_writes_nothing_under_the_workspace(host: Host, tmp_path: Path) -> None:
    ws = host.world.workspace
    git_repo(ws / "meta-avocado")
    host.layer_conf(ws / "meta-avocado")
    before = _listing(tmp_path)
    host.describe()
    assert _listing(tmp_path) == before


# --- resolving the host's build configuration ------------------------------------------------------------------


def test_recorded_workspace_kas_mode_and_build_dir_reach_the_resolution(host: Host) -> None:
    host.write_run(
        mode="container",
        env={
            "BAKAR_DISPATCH_KAS": KAS,
            "BAKAR_DISPATCH_WORKSPACE": WORKSPACE,
            "BAKAR_WORKTREE_BUILD_DIR": "build-{stem}-dispatched",
        },
    )
    host.describe()
    assert host.resolved == [(Path(WORKSPACE), Path(KAS), "container", "build-{stem}-dispatched")]


def test_a_run_recording_no_mode_or_build_dir_passes_none(host: Host) -> None:
    host.write_run(mode=None)
    host.describe()
    assert host.resolved == [(Path(WORKSPACE), Path(KAS), None, None)]


def test_failed_and_running_runs_resolve_no_configuration(host: Host) -> None:
    host.write_run(terminal={"event": "run_end", "exit_code": 1})
    host.describe()
    host.write_run(terminal={})
    host.describe()
    assert host.resolved == []


@pytest.fixture
def spy_resolution(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Replace ``_resolve_refresh_cfg`` with a recorder of the process-wide state it runs under."""
    from bakar.commands import feed as feed_cmd

    seen: list[dict[str, object]] = []

    def spy(workspace: Path | None, kas_yaml: Path) -> str:
        opts = app_state._GLOBAL_OPTIONS
        seen.append(
            {
                "host": opts.host,
                "container": opts.container,
                "build_dir": os.environ.get("BAKAR_WORKTREE_BUILD_DIR"),
                "args": (workspace, kas_yaml),
            }
        )
        return "cfg"

    monkeypatch.setattr(feed_cmd, "_resolve_refresh_cfg", spy)
    monkeypatch.delenv("BAKAR_WORKTREE_BUILD_DIR", raising=False)
    return seen


@pytest.mark.parametrize(
    ("mode", "flags"), [("host", (True, False)), ("container", (False, True)), (None, (False, False))]
)
def test_recorded_mode_is_applied_during_resolution_only(
    spy_resolution: list[dict[str, object]], mode: str | None, flags: tuple[bool, bool]
) -> None:
    before = app_state._GLOBAL_OPTIONS
    cfg = describe.resolve_host_cfg(Path("/ws"), Path("/ws/k.yml"), mode=mode, build_dir="build-x")
    assert cfg == "cfg"
    assert spy_resolution == [
        {"host": flags[0], "container": flags[1], "build_dir": "build-x", "args": (Path("/ws"), Path("/ws/k.yml"))}
    ]
    assert app_state._GLOBAL_OPTIONS is before
    assert "BAKAR_WORKTREE_BUILD_DIR" not in os.environ


def test_global_state_and_environment_are_restored_when_resolution_raises(
    monkeypatch: pytest.MonkeyPatch, spy_resolution: list[dict[str, object]]
) -> None:
    from bakar.commands import feed as feed_cmd

    def boom(_w: object, _k: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(feed_cmd, "_resolve_refresh_cfg", boom)
    before = app_state._GLOBAL_OPTIONS
    monkeypatch.setenv("BAKAR_WORKTREE_BUILD_DIR", "kept")
    with pytest.raises(RuntimeError, match="boom"):
        describe.resolve_host_cfg(Path("/ws"), Path("/ws/k.yml"), mode="container", build_dir="other")
    assert app_state._GLOBAL_OPTIONS is before
    assert os.environ["BAKAR_WORKTREE_BUILD_DIR"] == "kept"
    assert spy_resolution == []


def test_real_resolution_from_the_recorded_kas_path_and_workspace(
    meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2: the real ``_resolve_refresh_cfg`` resolves the dispatch's build directory from the two recorded values."""
    layout = meta_avocado_worktree_layout
    monkeypatch.delenv("BAKAR_WORKTREE_BUILD_DIR", raising=False)
    before = app_state._GLOBAL_OPTIONS

    host_cfg = describe.resolve_host_cfg(layout.ws, layout.kas_yaml, mode="host", build_dir="build-{stem}-dispatched")
    container_cfg = describe.resolve_host_cfg(
        layout.ws, layout.kas_yaml, mode="container", build_dir="build-{stem}-dispatched"
    )

    assert host_cfg.workspace == layout.ws
    assert host_cfg.bsp_root == layout.ws / "build-m-dispatched"
    assert host_cfg.host_mode is True
    assert container_cfg.host_mode is False
    assert container_cfg.bsp_root == layout.ws / "build-m-dispatched"
    assert app_state._GLOBAL_OPTIONS is before
    assert "BAKAR_WORKTREE_BUILD_DIR" not in os.environ


def test_resolution_refusal_becomes_a_describe_error_and_leaves_stdout_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(describe.DescribeError, match=r"nope\.yml"):
        describe.resolve_host_cfg(tmp_path, tmp_path / "nope.yml", mode=None, build_dir=None)
    out = capsys.readouterr()
    assert out.out == ""
    # rich wraps the message at the terminal width, which differs under xdist
    assert "nope.yml" in "".join(out.err.split())


def test_colon_joined_kas_argument_resolves_its_first_file(host: Host) -> None:
    host.write_run(
        env={"BAKAR_DISPATCH_KAS": "/srv/ws/m/kas/a.yml:/srv/ws/m/kas/b.yml", "BAKAR_DISPATCH_WORKSPACE": WORKSPACE}
    )
    host.describe()
    assert [kas for _ws, kas, _mode, _dir in host.resolved] == [Path("/srv/ws/m/kas/a.yml")]


def test_testdata_symlink_is_not_described_beside_its_target(host: Host) -> None:
    machine_dir = host.deploy_root / "images" / "qemux86-64"
    (machine_dir / "avocado-qemux86-64-link.testdata.json").symlink_to("avocado-qemux86-64.testdata.json")
    paths = [f.path for f in host.describe().files]
    assert "images/qemux86-64/avocado-qemux86-64.testdata.json" in paths
    assert "images/qemux86-64/avocado-qemux86-64-link.testdata.json" not in paths


def test_typed_release_keys_entries_when_the_build_declares_none(host: Host) -> None:
    (host.deploy_root / "images" / "qemux86-64" / "avocado-qemux86-64.testdata.json").unlink()
    doc = describe.describe_build(RUN_ID, ("htop",), release="2025.1", deps=host.deps())
    assert doc.entries[0].release == "2025.1"
    assert doc.release == ""


def test_declared_release_beats_the_typed_one_for_entries(host: Host) -> None:
    doc = describe.describe_build(RUN_ID, ("htop",), release="other", deps=host.deps())
    assert doc.entries[0].release == doc.release != "other"


def test_default_provider_runs_nothing_and_warns_that_bitbake_was_not_asked(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bakar.commands import bitbake as bitbake_cmd

    real_run, real_popen = subprocess.run, subprocess.Popen

    def forbidden(*args: object, **_kw: object) -> None:
        pytest.fail(f"describe started a process or bitbake lookup: {args!r}")

    def only_git(real):
        def guard(argv: object, *args: object, **kw: object) -> object:
            # The layer readers run read-only git queries; anything else is kas or bitbake.
            if not (isinstance(argv, list) and argv and argv[0] == "git"):
                forbidden(argv)
            return real(argv, *args, **kw)

        return guard

    monkeypatch.setattr(subprocess, "run", only_git(real_run))
    monkeypatch.setattr(subprocess, "Popen", only_git(real_popen))
    monkeypatch.setattr(bitbake_cmd, "resolve_recipe_files", forbidden)
    host.world.recipe("htop", layer="meta-extra")
    deps = describe.DescribeDeps(run_dir_for_run=host.lookup, lock_probe=host.probe, resolve_cfg=host.resolve_cfg)  # type: ignore[arg-type]

    doc = describe.describe_build(RUN_ID, ("htop",), deps=deps)

    assert [e.recipe for e in doc.entries] == ["htop"]
    assert doc.entries[0].layer_commit is None
    assert "could not ask bitbake which layer built htop" in doc.warnings[0]
    assert "does not run bitbake on the build host" in doc.warnings[0]


def _sibling(host: Host, name: str) -> Path:
    path = host.run_dir.parent / name
    path.mkdir()
    return path


def test_a_newer_run_in_the_same_build_directory_refuses_naming_it(host: Host) -> None:
    _sibling(host, "20260101-115959-9")
    _sibling(host, "20260102-080000-17")
    _sibling(host, "20260101-120001-3")
    with pytest.raises(describe.DescribeError, match="20260102-080000-17") as err:
        host.describe()
    assert "not the newest build" in str(err.value)
    assert "refresh from the newest run" in str(err.value)


def test_an_older_run_in_the_same_build_directory_is_not_a_refusal(host: Host) -> None:
    _sibling(host, "20251231-235959-1")
    assert host.describe().state == "succeeded"


def test_non_run_siblings_are_ignored(host: Host) -> None:
    (host.run_dir.parent / "20991231-000000-1.txt").write_text("")
    (host.run_dir.parent / "99999999-999999-9").write_text("not a directory")
    (host.run_dir.parent / "zz-notes").mkdir()
    newer = host.run_dir.parent.parent / "20991231-000000-2"
    newer.mkdir()
    (host.run_dir.parent / "latest").symlink_to(newer)
    (host.run_dir.parent / "20991231-000000-3").symlink_to(newer)
    assert host.describe().state == "succeeded"


@pytest.mark.parametrize("terminal", [{"event": "run_error", "exit_code": 3}, {}])
def test_failed_or_running_run_with_a_newer_sibling_still_returns_its_state(
    host: Host, terminal: dict[str, object]
) -> None:
    host.write_run(terminal=terminal)
    _sibling(host, "20260102-080000-17")
    doc = host.describe()
    assert doc.state in ("failed", "running")
    assert doc.files == ()
