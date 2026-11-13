"""Remote refresh: the pure plan, then the orchestration over a fake host tree."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from bakar import feed, feed_delta, feed_describe, layers, run_discovery
from bakar.commands import _feed_refresh as refresh
from bakar.commands import _feed_refresh_remote as remote_mod
from bakar.commands._feed_refresh import RefreshFailedError, RefreshRefusedError
from bakar.commands._feed_refresh_remote import RemotePlan, plan_remote_refresh, run_refresh_remote
from bakar.feed_delta import RecipeEntry
from bakar.feed_describe import BuildDescription, DescribedFile, LayerCommit
from bakar.steps.remote_dispatch import CaptureTimedOut
from bakar.steps.remote_pull import PullRefused
from tests._feed_world import World, make_rpm

if TYPE_CHECKING:
    from pathlib import Path

SHA = "a" * 64
DELIVERED = "b" * 40
PIN = "c" * 40
OTHER = "d" * 40
TAIL = "the previous delta is untouched"


def _entry(recipe: str = "hello") -> RecipeEntry:
    return RecipeEntry(
        recipe=recipe,
        packages=[recipe, f"{recipe}-dev"],
        layer="meta-avocado",
        epoch=0,
        pv="1.2.3",
        pr="r0.1",
        srcrev=None,
        layer_commit=DELIVERED,
        machine="qemux86-64",
        release="2025.1",
        task_hash=None,
    )


def _desc(**over: Any) -> BuildDescription:
    base: dict[str, Any] = {
        "schema_version": 1,
        "run_id": "20261112-101500-4242",
        "state": "succeeded",
        "exit_code": 0,
        "machine": "qemux86-64",
        "release": "2025.1",
        "channel": "dev",
        "tmpdir": "/ws/build/tmp",
        "deploy_dir": "/ws/build/tmp/deploy",
        "live": False,
        "live_reason": None,
        "files": (DescribedFile("rpm/core2_64/hello-1.2.3-r0.1.rpm", 12, SHA),),
        "entries": (_entry(),),
        "layers": (LayerCommit("meta-avocado", DELIVERED), LayerCommit("meta-x", PIN)),
        "bitbake_commit": None,
        "warnings": (),
    }
    base.update(over)
    return BuildDescription(**base)


def _plan(desc: BuildDescription | None = None, **over: Any) -> RemotePlan:
    args: dict[str, Any] = {
        "recipes": ["hello"],
        "release": None,
        "channel": None,
        "declared_pins": {"meta-x": {"commit": PIN}},
        "local_head": DELIVERED,
        "allow_pin_mismatch": False,
    }
    args.update(over)
    return plan_remote_refresh(desc or _desc(), **args)


def _refusal(desc: BuildDescription | None = None, **over: Any) -> str:
    with pytest.raises(RefreshFailedError) as exc:
        _plan(desc, **over)
    return str(exc.value)


def test_success_returns_declared_pair_entries_and_observed_layers() -> None:
    plan = _plan()
    assert plan.release == "2025.1"
    assert plan.channel == "dev"
    assert plan.entries == (_entry(),)
    assert plan.observed_layers == {"meta-avocado": DELIVERED, "meta-x": PIN}
    assert plan.pin_override_used is False
    assert plan.warnings == ()


def test_plan_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        _plan().release = "x"  # type: ignore[misc]


@pytest.mark.parametrize(("state", "code"), [("failed", 2), ("running", None)])
def test_non_succeeded_state_refused_naming_run_and_state(state: str, code: int | None) -> None:
    msg = _refusal(_desc(state=state, exit_code=code, files=(), entries=()))
    assert "20261112-101500-4242" in msg
    assert f"state {state}" in msg
    assert msg.endswith(TAIL)
    if code is not None:
        assert f"exit code {code}" in msg


def test_live_build_refused_naming_reason() -> None:
    msg = _refusal(_desc(live=True, live_reason="bitbake.lock is held"))
    assert "still live: bitbake.lock is held" in msg
    assert msg.endswith(TAIL)


def test_recipe_without_entry_refused_naming_recipe() -> None:
    msg = _refusal(recipes=["hello", "ghost"])
    assert "recipe ghost has no recipe-map entry" in msg
    assert msg.endswith(TAIL)


def test_recipe_without_package_file_refused_naming_recipe() -> None:
    msg = _refusal(_desc(files=(DescribedFile("rpm/core2_64/other-1.0-r0.rpm", 1, SHA),)))
    assert "recipe hello has no package file" in msg
    assert msg.endswith(TAIL)


def test_requested_release_differs_refused_naming_both() -> None:
    msg = _refusal(release="2026.1")
    assert "requested release 2026.1" in msg
    assert "declares 2025.1/dev" in msg
    assert msg.endswith(TAIL)


def test_requested_channel_differs_refused_naming_both() -> None:
    msg = _refusal(channel="stable")
    assert "requested channel stable" in msg
    assert "declares 2025.1/dev" in msg
    assert msg.endswith(TAIL)


def test_requested_pair_equal_to_declared_accepted() -> None:
    plan = _plan(release="2025.1", channel="dev")
    assert (plan.release, plan.channel) == ("2025.1", "dev")


def test_only_release_requested_takes_channel_from_declared() -> None:
    plan = _plan(release="2025.1")
    assert (plan.release, plan.channel) == ("2025.1", "dev")


def test_only_channel_requested_takes_release_from_declared() -> None:
    plan = _plan(channel="dev")
    assert (plan.release, plan.channel) == ("2025.1", "dev")


def test_empty_declared_pair_with_nothing_requested_refused() -> None:
    msg = _refusal(_desc(release="", channel=""))
    assert "the build declares no release and channel; request one with --release and --channel" in msg
    assert msg.endswith(TAIL)


def test_empty_declared_pair_with_pair_requested_is_planned() -> None:
    plan = _plan(_desc(release="", channel=""), release="2025.1", channel="dev")
    assert (plan.release, plan.channel) == ("2025.1", "dev")


def test_pin_differs_refused_naming_repo_both_commits_and_override_flag() -> None:
    msg = _refusal(declared_pins={"meta-x": {"commit": OTHER}})
    assert f"meta-x declared {OTHER} but the host built {PIN}" in msg
    assert "--allow-pin-mismatch" in msg
    assert msg.endswith(TAIL)


def test_missing_evidence_refusal_says_no_evidence_and_names_override() -> None:
    desc = _desc(layers=(LayerCommit("meta-avocado", DELIVERED), LayerCommit("meta-x", None)))
    msg = _refusal(desc)
    assert f"meta-x declared {PIN} but the host reported no evidence for it" in msg
    assert "--allow-pin-mismatch" in msg


def test_pinned_repo_absent_from_description_is_not_a_match() -> None:
    msg = _refusal(_desc(layers=(LayerCommit("meta-avocado", DELIVERED),)))
    assert "meta-x declared" in msg
    assert "no evidence" in msg


def test_override_publishes_and_records_observed_layers() -> None:
    plan = _plan(declared_pins={"meta-x": {"commit": OTHER}}, allow_pin_mismatch=True)
    assert plan.pin_override_used is True
    assert plan.observed_layers == {"meta-avocado": DELIVERED, "meta-x": PIN}


def test_override_keeps_null_observed_commits() -> None:
    desc = _desc(layers=(LayerCommit("meta-avocado", DELIVERED), LayerCommit("meta-x", None)))
    plan = _plan(desc, allow_pin_mismatch=True)
    assert plan.pin_override_used is True
    assert plan.observed_layers == {"meta-avocado": DELIVERED, "meta-x": None}


def test_override_flag_without_refusals_is_not_used() -> None:
    plan = _plan(allow_pin_mismatch=True)
    assert plan.pin_override_used is False


def test_override_does_not_bypass_live_build() -> None:
    msg = _refusal(_desc(live=True, live_reason="x"), allow_pin_mismatch=True)
    assert "still live" in msg


def test_delivered_commit_differs_from_local_head_warns_naming_both() -> None:
    plan = _plan(local_head=OTHER)
    assert len(plan.warnings) == 1
    assert DELIVERED in plan.warnings[0]
    assert OTHER in plan.warnings[0]


def test_delivered_commit_equal_to_local_head_no_warning() -> None:
    assert _plan(local_head=DELIVERED).warnings == ()


def test_local_head_none_no_warning() -> None:
    assert _plan(local_head=None).warnings == ()


def test_delivered_none_no_warning() -> None:
    desc = _desc(layers=(LayerCommit("meta-x", PIN),))
    assert _plan(desc, local_head=OTHER).warnings == ()


def test_description_warnings_carried_through_before_commit_warning() -> None:
    plan = _plan(_desc(warnings=("two layers provide hello",)), local_head=OTHER)
    assert plan.warnings[0] == "two layers provide hello"
    assert len(plan.warnings) == 2


# ---------------------------------------------------------------------------
# run_refresh_remote: orchestration over a fake host tree
# ---------------------------------------------------------------------------

HOST = "pc2"
RUN = "20261112-101500-4242"
HOST_PIN = "e" * 40


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """The pipeline's fake workspace, with the publishing tail's external steps faked."""
    w = World(tmp_path)
    monkeypatch.setattr(refresh.feed_ops, "scripts_dir", lambda _y: tmp_path / "scripts")

    def fake_sync(_cfg: object, *, publish: feed.FeedPublish, feed_root: Path, stage_root: Path, **_kw: object) -> dict:
        chan = feed.channel_root(feed_root, release=publish.release, channel=publish.channel)
        staged = feed.channel_root(stage_root, release=publish.release, channel=publish.channel)
        for rpm in publish.deploy_dir.rglob("*.rpm"):
            dest = staged / "target" / "qemux86-64" / rpm.parent.name / rpm.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rpm, dest)
        (chan / "target" / "qemux86-64" / "repodata").mkdir(parents=True, exist_ok=True)
        (chan / "target" / "qemux86-64" / "repodata" / "pooled-primary.xml.gz").write_bytes(b"pooled")
        return {"snapshot": None, "channel_root": chan, "repos": ["target/qemux86-64"]}

    def fake_createrepo(repo: Path) -> None:
        hrefs = "".join(f'<location href="{p.relative_to(repo)}"/>' for p in sorted(repo.rglob("*.rpm")))
        (repo / "repodata").mkdir(exist_ok=True)
        (repo / "repodata" / "primary.xml").write_text(hrefs)

    monkeypatch.setattr(refresh.feed, "sync", fake_sync)
    monkeypatch.setattr(refresh, "_createrepo", fake_createrepo)
    monkeypatch.setattr(
        layers, "collect_layer_hashes", lambda _cfg: [layers.LayerHash(repo="m", short_hash="abc", branch="")]
    )
    return w


class Host:
    """A fake build host: a tree of described files and a record of every call made to it."""

    def __init__(self, tmp: Path) -> None:
        self.tree = tmp / "host-tree"
        self.calls: list[str] = []
        self.describe_args: list[tuple[str, str, tuple[str, ...]]] = []
        self.staging_seen: list[Path] = []
        self.preflight_ok = True
        self.desc = self._build_desc()

    def _build_desc(self, **over: Any) -> BuildDescription:
        rpm = make_rpm(self.tree / "rpm", "core2_64", "htop", version="1.0", release="r0.1")
        repo_map = self.tree / "rpm" / "avocado-repo.map"
        repo_map.write_text("repo=$releasever/target/qemux86-64\n")
        files = tuple(
            DescribedFile(
                p.relative_to(self.tree).as_posix(), p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest()
            )
            for p in (rpm, repo_map)
        )
        entry = RecipeEntry(
            recipe="htop",
            packages=["htop"],
            layer="meta-oe",
            epoch=0,
            pv="1.0",
            pr="r0.1",
            srcrev=None,
            layer_commit="abc123def456",
            machine="qemux86-64",
            release="2026",
            task_hash="f" * 64,
        )
        base: dict[str, Any] = {
            "schema_version": 1,
            "run_id": RUN,
            "state": "succeeded",
            "exit_code": 0,
            "machine": "qemux86-64",
            "release": "2026",
            "channel": "edge",
            "tmpdir": "/ws/tmp",
            "deploy_dir": "/ws/tmp/deploy",
            "live": False,
            "live_reason": None,
            "files": files,
            "entries": (entry,),
            "layers": (LayerCommit("meta-avocado", DELIVERED), LayerCommit("meta-x", HOST_PIN)),
            "bitbake_commit": None,
            "warnings": (),
        }
        base.update(over)
        return BuildDescription(**base)

    def describe(self, host: str, run_id: str, recipes: Any) -> BuildDescription:
        self.calls.append("describe")
        self.describe_args.append((host, run_id, tuple(recipes)))
        return self.desc

    def preflight(self, host: str, *, assume_yes: bool) -> bool:
        self.calls.append("preflight")
        return self.preflight_ok

    def puller(self, host: str, desc: BuildDescription, staging: Path, *, describe_again: Any, **_kw: Any) -> None:
        self.calls.append("pull")
        self.staging_seen.append(staging)
        for item in desc.files:
            dest = staging / item.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.tree / item.path, dest)
        assert describe_again() == desc
        self.calls.append("describe_again_ok")


@pytest.fixture
def host(world: World, monkeypatch: pytest.MonkeyPatch) -> Host:
    h = Host(world.tmp)
    monkeypatch.setattr(remote_mod, "preflight_or_report", h.preflight)
    monkeypatch.setattr(remote_mod, "meta_avocado_checkout", lambda _y: world.tmp / "checkout")
    monkeypatch.setattr(remote_mod.kas_pins, "probe_pins", lambda _y, _c: ("2026", {"meta-x": {"commit": HOST_PIN}}))
    monkeypatch.setattr(
        remote_mod,
        "run_git",
        lambda argv, **_k: SimpleNamespace(returncode=0, stdout=f"{DELIVERED}\n"),
    )
    return h


def _go(world: World, host: Host, **over: Any) -> refresh.RefreshResult:
    args: dict[str, Any] = {
        "host": HOST,
        "run_id": RUN,
        "allow_pin_mismatch": False,
        "assume_yes": False,
        "describe": host.describe,
        "puller": host.puller,
    }
    args.update(over)
    req = args.pop("req", world.req)
    return run_refresh_remote(req, **args)


def _delta_files(world: World, release: str = "2026", channel: str = "edge") -> dict[str, bytes]:
    chan = feed.channel_root(world.req.delta_root, release=release, channel=channel)
    return {str(p.relative_to(chan)): p.read_bytes() for p in sorted(chan.rglob("*")) if p.is_file()}


def _no_staging(world: World) -> bool:
    return not list(world.req.work_root.parent.glob("pull-*"))


def test_success_publishes_pulled_package_and_records_source(world: World, host: Host) -> None:
    result = _go(world, host)

    chan = feed.channel_root(world.req.delta_root, release="2026", channel="edge")
    assert result.channel_dir == chan
    assert result.run_id == RUN
    assert result.recipes == ("htop",)
    assert result.entries == list(host.desc.entries)
    assert result.warnings == ()
    rpms = sorted(p.name for p in chan.rglob("*.rpm"))
    assert rpms == ["htop-1.0-r0.1.core2_64.rpm"]
    delta = json.loads((chan / feed_delta.DELTA_NAME).read_text())
    assert delta["built_recipes"] == ["htop"]
    assert delta["bakar_run_id"] == RUN
    assert delta["base"] == {"release": "2025", "channel": "stable", "snapshot_id": "snap-1"}
    source = delta["source"]
    assert set(source) == {"host", "run_id", "pulled_at", "observed_layers", "pin_override"}
    assert source["host"] == HOST
    assert source["run_id"] == RUN
    assert source["observed_layers"] == {"meta-avocado": DELIVERED, "meta-x": HOST_PIN}
    assert source["pin_override"] is False
    pulled = datetime.fromisoformat(source["pulled_at"])
    assert pulled.utcoffset() == timedelta(0)
    (entry,) = json.loads((chan / feed_delta.RECIPE_MAP_NAME).read_text())["entries"]
    assert entry["recipe"] == "htop"
    assert entry["layer_commit"] == "abc123def456"
    assert entry["task_hash"] == "f" * 64


def test_call_order_preflight_describe_pull_describe_again(world: World, host: Host) -> None:
    _go(world, host)
    assert host.calls == ["preflight", "describe", "pull", "describe", "describe_again_ok"]
    assert host.describe_args == [(HOST, RUN, ("htop",)), (HOST, RUN, ("htop",))]


def test_staging_is_removed_after_success(world: World, host: Host) -> None:
    _go(world, host)
    (staging,) = host.staging_seen
    assert staging.parent == world.req.work_root.parent
    assert not staging.exists()
    assert _no_staging(world)


def test_override_records_pin_override_and_observed_commits(world: World, host: Host) -> None:
    host.desc = host._build_desc(layers=(LayerCommit("meta-avocado", DELIVERED), LayerCommit("meta-x", OTHER)))
    with pytest.raises(RefreshFailedError, match=r"--allow-pin-mismatch"):
        _go(world, host)
    assert not _delta_files(world)

    _go(world, host, allow_pin_mismatch=True)

    chan = feed.channel_root(world.req.delta_root, release="2026", channel="edge")
    source = json.loads((chan / feed_delta.DELTA_NAME).read_text())["source"]
    assert source["pin_override"] is True
    assert source["observed_layers"] == {"meta-avocado": DELIVERED, "meta-x": OTHER}


def test_delivered_commit_differing_from_local_head_warns_and_publishes(
    world: World, host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote_mod, "run_git", lambda argv, **_k: SimpleNamespace(returncode=0, stdout=f"{OTHER}\n"))
    result = _go(world, host)
    assert result.warnings == (f"the host built meta-avocado {DELIVERED} but the local checkout is at {OTHER}",)
    assert _delta_files(world)


def test_unreadable_local_head_gives_no_warning(world: World, host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_mod, "run_git", lambda argv, **_k: None)
    assert _go(world, host).warnings == ()


def test_declared_pair_replaces_the_request_pair(world: World, host: Host) -> None:
    host.desc = host._build_desc(release="2027", channel="beta")
    result = _go(world, host)
    assert result.channel_dir == feed.channel_root(world.req.delta_root, release="2027", channel="beta")
    assert _delta_files(world, "2027", "beta")
    assert not _delta_files(world, "2026", "edge")


def test_requested_pair_that_differs_refuses_naming_both(world: World, host: Host) -> None:
    host.desc = host._build_desc(release="2027", channel="beta")
    with pytest.raises(RefreshFailedError, match=r"requested release 2026 but the build declares 2027/beta") as info:
        _go(world, host, requested_release="2026", requested_channel="edge")
    assert str(info.value).endswith(TAIL)
    assert "pull" not in host.calls


def test_lock_held_refuses_before_the_host_is_contacted(world: World, host: Host) -> None:
    with refresh.refresh_lock(world.req.feed_root), pytest.raises(RefreshRefusedError, match="another refresh holds"):
        _go(world, host)
    assert host.calls == []


def test_malformed_lock_timeout_refuses_before_the_host_is_contacted(
    world: World, host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAKAR_FEED_LOCK_TIMEOUT", "soon")
    with pytest.raises(RefreshFailedError, match="BAKAR_FEED_LOCK_TIMEOUT"):
        _go(world, host)
    assert host.calls == []


def test_delta_root_aliasing_the_feed_refuses_before_the_host_is_contacted(world: World, host: Host) -> None:
    req = dataclasses.replace(world.req, delta_root=world.req.feed_root)
    with pytest.raises(RefreshRefusedError, match="resolves to the synced feed root"):
        _go(world, host, req=req)
    assert host.calls == []


def test_missing_scripts_refuses_before_the_host_is_contacted(
    world: World, host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(refresh.feed_ops, "scripts_dir", lambda _y: None)
    with pytest.raises(RefreshFailedError, match="meta-avocado scripts not found"):
        _go(world, host)
    assert host.calls == []


def test_mirror_marker_in_the_planned_channel_refuses_before_the_pull(world: World, host: Host) -> None:
    marked = feed.channel_root(world.req.delta_root, release="2026", channel="edge") / "target" / "x"
    marked.mkdir(parents=True)
    (marked / feed.MIRROR_MARKER).write_text("{}")
    with pytest.raises(RefreshRefusedError, match="bakar feed mirror"):
        _go(world, host)
    assert "pull" not in host.calls


def test_preflight_refusal_stops_before_describe(world: World, host: Host) -> None:
    host.preflight_ok = False
    with pytest.raises(RefreshFailedError, match=rf"{HOST} was refused") as info:
        _go(world, host)
    assert str(info.value).endswith(TAIL)
    assert host.calls == ["preflight"]
    assert not _delta_files(world)


def test_local_live_build_does_not_block_and_no_build_starts(
    world: World, host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def live(*_a: object, **_k: object) -> list:
        raise AssertionError("a remote refresh must not look at local builds")

    def no_build(*_a: object, **_k: object) -> None:
        raise AssertionError("a remote refresh must not build")

    monkeypatch.setattr(run_discovery, "live_workspace_runs", live)
    monkeypatch.setattr(refresh.bitbake_cmd, "build_targets", no_build)
    _go(world, host)
    assert _delta_files(world)


def test_plan_refusal_copies_nothing(world: World, host: Host) -> None:
    host.desc = host._build_desc(state="failed", exit_code=1)
    with pytest.raises(RefreshFailedError, match=f"run {RUN} is in state failed"):
        _go(world, host)
    assert host.calls == ["preflight", "describe"]
    assert _no_staging(world)


def test_unsafe_description_refused_before_any_staging_exists(world: World, host: Host) -> None:
    files = (*host.desc.files, DescribedFile("rpm/../etc/passwd", 1, "0" * 64))
    host.desc = host._build_desc(files=files)
    with pytest.raises(RefreshFailedError, match="passwd") as info:
        _go(world, host)
    assert str(info.value).endswith(TAIL)
    assert "pull" not in host.calls
    assert _no_staging(world)


def test_puller_refusal_keeps_previous_delta_and_leaves_no_staging(world: World, host: Host) -> None:
    _go(world, host)
    before = _delta_files(world)

    def refusing(host_name: str, desc: BuildDescription, staging: Path, **_kw: Any) -> None:
        host.staging_seen.append(staging)
        (staging / "half").write_text("x")
        raise PullRefused("the host build changed during the copy")

    host.staging_seen.clear()
    with pytest.raises(RefreshFailedError, match="the host build changed during the copy") as info:
        _go(world, host, puller=refusing)

    assert str(info.value).endswith(TAIL)
    assert _delta_files(world) == before
    (staging,) = host.staging_seen
    assert not staging.exists()
    assert _no_staging(world)


def test_publish_failure_keeps_previous_delta_and_leaves_no_staging(
    world: World, host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _go(world, host)
    before = _delta_files(world)
    host.staging_seen.clear()

    def boom(_repo: Path) -> None:
        raise subprocess.CalledProcessError(1, "createrepo_c")

    monkeypatch.setattr(refresh, "_createrepo", boom)
    with pytest.raises(subprocess.CalledProcessError):
        _go(world, host)

    assert _delta_files(world) == before
    (staging,) = host.staging_seen
    assert not staging.exists()
    assert _no_staging(world)
    assert not list(world.tmp.glob("delta*.new"))


def test_staging_parent_is_created_when_absent(world: World, host: Host) -> None:
    req = dataclasses.replace(world.req, work_root=world.tmp / "deep" / "er" / "delta-work")
    _go(world, host, req=req)
    (staging,) = host.staging_seen
    assert staging.parent == world.tmp / "deep" / "er"
    assert not staging.exists()


# -- the default describe: the ssh script and its refusals --------------------


class FakeSsh:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, host: str, script: str, *, batch_mode: bool = True, timeout: float = 5.0) -> Any:
        self.calls.append((host, script, timeout))
        return self.result


def _completed(stdout: str = "", stderr: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["ssh"], returncode=code, stdout=stdout, stderr=stderr)


def _ssh(monkeypatch: pytest.MonkeyPatch, result: Any) -> FakeSsh:
    fake = FakeSsh(result)
    monkeypatch.setattr(remote_mod.remote_dispatch, "_ssh_bash_capture", fake)
    return fake


def test_default_describe_sends_the_quoted_command_over_bash_s(
    world: World, host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    odd = dataclasses.replace(host.desc.entries[0], recipe="it's odd")
    host.desc = host._build_desc(entries=(*host.desc.entries, odd))
    fake = _ssh(monkeypatch, _completed(feed_describe.encode(host.desc)))
    req = dataclasses.replace(world.req, recipes=("htop", "it's odd"))

    _go(world, host, req=req, describe=None)

    assert fake.calls[0] == (
        HOST,
        f"bakar feed describe {RUN} --recipe htop --recipe 'it'\"'\"'s odd'\n",
        300.0,
    )


def test_default_describe_timeout_refuses_naming_host(
    world: World, host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    timed_out = CaptureTimedOut(args=["ssh"], returncode=255, stdout="", stderr="could not reach pc2")
    timed_out.timeout = 300.0
    _ssh(monkeypatch, timed_out)
    with pytest.raises(RefreshFailedError, match=rf"{HOST}.*timed out after 300s") as info:
        _go(world, host, describe=None)
    assert str(info.value).endswith(TAIL)
    assert "pull" not in host.calls


def test_default_describe_nonzero_exit_names_host_and_first_stderr_line(
    world: World, host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ssh(monkeypatch, _completed(stderr="run 123 not found\nsecond line\n", code=1))
    with pytest.raises(RefreshFailedError) as info:
        _go(world, host, describe=None)
    text = str(info.value)
    assert HOST in text
    assert "exit 1" in text
    assert "run 123 not found" in text
    assert "second line" not in text


@pytest.mark.parametrize("assume_yes", [False, True])
def test_host_without_the_verb_is_refused_whatever_assume_yes_says(
    world: World, host: Host, monkeypatch: pytest.MonkeyPatch, assume_yes: bool
) -> None:
    _ssh(monkeypatch, _completed(stderr="Usage: bakar feed [OPTIONS]\nError: No such command 'describe'.\n", code=2))
    with pytest.raises(RefreshFailedError) as info:
        _go(world, host, describe=None, assume_yes=assume_yes)
    text = str(info.value)
    assert HOST in text
    assert "bakar feed describe" in text
    assert "lacks" in text
    assert host.calls == ["preflight"]


def test_default_describe_unparseable_answer_refuses(world: World, host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _ssh(monkeypatch, _completed(stdout="not json"))
    with pytest.raises(RefreshFailedError, match=rf"{HOST}.*unreadable description") as info:
        _go(world, host, describe=None)
    assert str(info.value).endswith(TAIL)


def test_only_release_requested_with_no_declared_channel_refused() -> None:
    msg = _refusal(_desc(release="", channel=""), release="2025.1")
    assert "the build declares no channel; request one with --channel" in msg


@pytest.mark.parametrize("bad", ["..", ".", "../../x", "a/b", "a\\b"])
def test_host_declared_release_or_channel_that_is_not_one_directory_name_refused(bad: str) -> None:
    assert "is not a single directory name" in _refusal(_desc(release="2025.1", channel=bad))
    assert "is not a single directory name" in _refusal(_desc(release=bad, channel="dev"))


def test_ssh_describe_forwards_a_typed_release_only_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_capture(_host: str, script: str, timeout: float) -> object:
        seen.append(script)
        raise RuntimeError("stop")

    monkeypatch.setattr(remote_mod.remote_dispatch, "_ssh_bash_capture", fake_capture)
    for release in ("2025.1", None):
        with pytest.raises(RuntimeError):
            remote_mod._ssh_describe("pc2", "RUN", ["htop"], release)
    assert "--release 2025.1" in seen[0]
    assert "--release" not in seen[1]


def test_bitbake_pin_is_judged_by_the_hosts_bitbake_commit() -> None:
    pins = {"meta-x": {"commit": PIN}, "bitbake": {"commit": OTHER}}
    plan = _plan(_desc(bitbake_commit=OTHER), declared_pins=pins)
    assert plan.observed_layers["bitbake"] == OTHER


def test_bitbake_pin_that_differs_from_the_hosts_bitbake_commit_is_refused() -> None:
    pins = {"meta-x": {"commit": PIN}, "bitbake": {"commit": OTHER}}
    msg = _refusal(_desc(bitbake_commit=PIN), declared_pins=pins)
    assert f"bitbake declared {OTHER} but the host built {PIN}" in msg


def test_bitbake_pin_with_no_host_bitbake_commit_says_no_evidence() -> None:
    pins = {"meta-x": {"commit": PIN}, "bitbake": {"commit": OTHER}}
    assert "bitbake declared" in _refusal(_desc(bitbake_commit=None), declared_pins=pins)
