"""Refresh the delta feed from a build another host ran.

:func:`plan_remote_refresh` decides whether a described build may be published.
It is pure: the description, the pins the local kas file declares and the local
checkout head are arguments, so every refusal is testable without a host or a
file tree, and a refusal raises before anything is pulled or written.

:func:`run_refresh_remote` is the orchestration around it. The refresh lock comes
first, so a second refresh is refused before the host is contacted. It builds
nothing: the host's own build data is described, the described files are pulled
into a private staging directory, and the shared publishing tail
(:func:`bakar.commands._feed_refresh.publish_delta`) swaps the delta in.
"""

from __future__ import annotations

import dataclasses
import shlex
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from bakar import feed, feed_describe, feed_lock, feed_ops
from bakar.bsp_detect import meta_avocado_checkout
from bakar.commands._feed_refresh import (
    RefreshFailedError,
    RefreshRequest,
    RefreshResult,
    check_no_mirror_markers,
    check_stage_root,
    publish_delta,
    refresh_lock,
    rpm_files,
)
from bakar.feed_pins import compare_pins, describe_refusal
from bakar.gitutil import run_git
from bakar.release_workspace import ReleaseRoutingError
from bakar.steps import kas_pins, remote_dispatch
from bakar.steps.remote_dispatch import CaptureTimedOut, preflight_or_report
from bakar.steps.remote_pull import PullRefused, make_staging, pull_files, validate_description

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from bakar.feed_delta import RecipeEntry
    from bakar.feed_describe import BuildDescription

    type Describe = Callable[[str, str, Sequence[str]], BuildDescription]

_UNTOUCHED = "the previous delta is untouched"
OVERRIDE_FLAG = "--allow-pin-mismatch"
# Hashing a large deploy tree on the host outlasts the default probe timeout.
DESCRIBE_TIMEOUT_SECONDS = 300.0


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
    pair = (release if release is not None else desc.release, channel if channel is not None else desc.channel)
    if not pair[0] and not pair[1]:
        raise _refuse("the build declares no release and channel; request one with --release and --channel")
    for kind, value in zip(("release", "channel"), pair, strict=True):
        if not value:
            raise _refuse(f"the build declares no {kind}; request one with --{kind}")
        # The host's document is untrusted and these values name directories under the delta root.
        if value in (".", "..") or any(ch in value for ch in "/\\\0"):
            raise _refuse(f"{kind} {value!r} is not a single directory name")
    return pair


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


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "no output")


def _ssh_describe(host: str, run_id: str, recipes: Sequence[str]) -> BuildDescription:
    """Run ``bakar feed describe`` on ``host`` and parse its answer, or refuse naming the host."""
    argv = ["bakar", "feed", "describe", run_id]
    for recipe in recipes:
        argv += ["--recipe", recipe]
    result = remote_dispatch._ssh_bash_capture(host, shlex.join(argv) + "\n", timeout=DESCRIBE_TIMEOUT_SECONDS)
    if isinstance(result, CaptureTimedOut):
        raise _refuse(f"describing run {run_id} on {host} timed out after {result.timeout:g}s")
    stderr = result.stderr or ""
    if result.returncode != 0:
        # An older bakar has no such verb; no --yes overrides that, so it is named apart.
        if "no such command" in stderr.lower():
            raise _refuse(
                f"{host} runs a bakar that lacks `bakar feed describe`; install the same bakar on {host} "
                "as on this node"
            )
        raise _refuse(f"describing run {run_id} on {host} failed (exit {result.returncode}): {_first_line(stderr)}")
    try:
        return feed_describe.parse(result.stdout)
    except feed_describe.DescriptionError as exc:
        raise _refuse(f"{host} returned an unreadable description of run {run_id}: {exc}") from exc


def _local_head(checkout: Path) -> str | None:
    """HEAD of the meta-avocado checkout, or None when git cannot say."""
    out = run_git(["git", "-C", str(checkout), "rev-parse", "HEAD"])
    head = out.stdout.strip() if out is not None and out.returncode == 0 else ""
    return head or None


def _declared_pins(kas_yaml: Path, checkout: Path) -> Mapping[str, object]:
    try:
        return kas_pins.probe_pins(kas_yaml, checkout)[1]
    except ReleaseRoutingError as exc:
        raise _refuse(f"cannot read the pins {kas_yaml} declares: {exc}") from exc


def run_refresh_remote(
    req: RefreshRequest,
    *,
    host: str,
    run_id: str,
    allow_pin_mismatch: bool,
    assume_yes: bool,
    requested_release: str | None = None,
    requested_channel: str | None = None,
    describe: Describe | None = None,
    puller: Callable[..., None] | None = None,
) -> RefreshResult:
    """Replace the delta with the packages of ``run_id`` built on ``host``.

    Order is the contract. The lock and the local guards come before any host
    contact, the pins and the pair are judged before any file is copied, and the
    delta is swapped in last, so a refusal or failure at any step leaves the
    previous delta in place and the staging directory removed. ``req.release``
    and ``req.channel`` are the local defaults; only ``requested_release`` and
    ``requested_channel``, the values the caller explicitly asked for, are held
    against the pair the build declares, and the declared pair replaces them.
    """
    recipes = tuple(dict.fromkeys(req.recipes))
    if not recipes:
        raise RefreshFailedError("name at least one recipe to pull; nothing was pulled")
    try:
        feed_lock.validate_timeout()
    except ValueError as exc:
        raise RefreshFailedError(f"{exc}; nothing was pulled") from exc
    describe_host = describe or _ssh_describe
    with refresh_lock(req.feed_root):
        # No live-build check: a remote refresh reads no local build output.
        for root in (req.delta_root, req.work_root):
            check_stage_root(root, feed_root=req.feed_root, sync_stage_root=req.sync_stage_root)
        scripts = feed_ops.scripts_dir(req.kas_yaml)
        if scripts is None:
            raise RefreshFailedError(f"meta-avocado scripts not found for {req.kas_yaml}; nothing was pulled")
        checkout = meta_avocado_checkout(req.kas_yaml)
        if checkout is None:
            raise _refuse(f"no meta-avocado checkout holds {req.kas_yaml}, so its pins cannot be read")
        declared = _declared_pins(req.kas_yaml, checkout)
        if not preflight_or_report(host, assume_yes=assume_yes):
            raise _refuse(f"the host {host} was refused by the preflight")
        desc = describe_host(host, run_id, recipes)
        plan = plan_remote_refresh(
            desc,
            recipes=recipes,
            release=requested_release,
            channel=requested_channel,
            declared_pins=declared,
            local_head=_local_head(checkout),
            allow_pin_mismatch=allow_pin_mismatch,
        )
        req = dataclasses.replace(req, release=plan.release, channel=plan.channel)
        for root in (req.delta_root, req.work_root):
            check_no_mirror_markers(feed.channel_root(root, release=req.release, channel=req.channel))
        try:
            validate_description(desc)
            req.work_root.parent.mkdir(parents=True, exist_ok=True)
            staging = make_staging(req.work_root.parent)
        except (PullRefused, OSError) as exc:
            raise _refuse(str(exc)) from exc
        try:
            try:
                (puller or pull_files)(host, desc, staging, describe_again=lambda: describe_host(host, run_id, recipes))
            except PullRefused as exc:
                raise _refuse(str(exc)) from exc
            deploy = staging / "rpm"
            sync, channel_dir = publish_delta(
                req,
                deploy=deploy,
                scripts=scripts,
                own_names=frozenset(pkg for entry in plan.entries for pkg in entry.packages),
                rpms=rpm_files(deploy),
                entries=list(plan.entries),
                recipes=recipes,
                run_id=run_id,
                source={
                    "host": host,
                    "run_id": run_id,
                    "pulled_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "observed_layers": dict(plan.observed_layers),
                    "pin_override": plan.pin_override_used,
                },
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return RefreshResult(
        channel_dir=channel_dir,
        run_id=run_id,
        recipes=recipes,
        sync=sync,
        entries=list(plan.entries),
        warnings=plan.warnings,
    )
