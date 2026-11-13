"""Decide whether a build described by a remote host may be published as a delta.

The planning step is pure: the description, the pins the local kas file declares
and the local checkout head are arguments, so every refusal is testable without
a host or a file tree. A refusal raises before anything is pulled or written.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bakar.commands._feed_refresh import RefreshFailedError
from bakar.feed_pins import compare_pins, describe_refusal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from bakar.feed_delta import RecipeEntry
    from bakar.feed_describe import BuildDescription

_UNTOUCHED = "the previous delta is untouched"
OVERRIDE_FLAG = "--allow-pin-mismatch"


@dataclass(frozen=True)
class RemotePlan:
    """What a remote refresh will publish: the pair, the entries and how the pins were judged."""

    release: str
    channel: str
    entries: tuple[RecipeEntry, ...]
    observed_layers: Mapping[str, str | None]
    pin_override_used: bool
    warnings: tuple[str, ...]


def _refuse(message: str) -> RefreshFailedError:
    return RefreshFailedError(f"{message}; {_UNTOUCHED}")


def _has_package_file(desc: BuildDescription, entry: RecipeEntry) -> bool:
    # RPM file names are <name>-<version>-<release>.<arch>.rpm; matching name and
    # version keeps "hello" from claiming the files of "hello-world".
    prefixes = tuple(f"{pkg}-{entry.pv}-" for pkg in entry.packages)
    return any(f.path.rsplit("/", 1)[-1].startswith(prefixes) for f in desc.files)


def _resolve_pair(desc: BuildDescription, release: str | None, channel: str | None) -> tuple[str, str]:
    declared = f"{desc.release}/{desc.channel}"
    for kind, requested, actual in (
        ("release", release, desc.release),
        ("channel", channel, desc.channel),
    ):
        if requested is not None and actual and requested != actual:
            raise _refuse(f"requested {kind} {requested} but the build declares {declared}")
    if release is None and channel is None and not (desc.release and desc.channel):
        raise _refuse("the build declares no release and channel; request one with --release and --channel")
    return (release if release is not None else desc.release, channel if channel is not None else desc.channel)


def plan_remote_refresh(
    desc: BuildDescription,
    *,
    recipes: Sequence[str],
    release: str | None,
    channel: str | None,
    declared_pins: Mapping[str, object],
    local_head: str | None,
    allow_pin_mismatch: bool,
) -> RemotePlan:
    """Return the plan for publishing ``desc``, or raise ``RefreshFailedError``."""
    if desc.state != "succeeded":
        code = f" (exit code {desc.exit_code})" if desc.exit_code is not None else ""
        raise _refuse(f"run {desc.run_id} is in state {desc.state}{code}, not succeeded")
    if desc.live:
        raise _refuse(f"run {desc.run_id} is still live: {desc.live_reason}")
    by_recipe = {e.recipe: e for e in desc.entries}
    chosen: list[RecipeEntry] = []
    for recipe in recipes:
        entry = by_recipe.get(recipe)
        if entry is None:
            raise _refuse(f"recipe {recipe} has no recipe-map entry in run {desc.run_id}")
        if not _has_package_file(desc, entry):
            raise _refuse(f"recipe {recipe} has no package file in run {desc.run_id}")
        chosen.append(entry)
    pair_release, pair_channel = _resolve_pair(desc, release, channel)
    report = compare_pins(declared_pins, desc.layers)
    if report.refusals and not allow_pin_mismatch:
        raise _refuse(
            f"the host build does not match the pins the kas file declares: {describe_refusal(report)}. "
            f"Pass {OVERRIDE_FLAG} to publish it anyway"
        )
    warnings = list(desc.warnings)
    if local_head is not None and report.delivered is not None and report.delivered != local_head:
        warnings.append(f"the host built meta-avocado {report.delivered} but the local checkout is at {local_head}")
    return RemotePlan(
        release=pair_release,
        channel=pair_channel,
        entries=tuple(chosen),
        observed_layers=report.observed,
        pin_override_used=bool(report.refusals),
        warnings=tuple(warnings),
    )
