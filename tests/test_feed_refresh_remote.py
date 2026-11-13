"""Deciding whether a described remote build may be published: pure planning."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from bakar.commands._feed_refresh import RefreshFailedError
from bakar.commands._feed_refresh_remote import RemotePlan, plan_remote_refresh
from bakar.feed_delta import RecipeEntry
from bakar.feed_describe import BuildDescription, DescribedFile, LayerCommit

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
