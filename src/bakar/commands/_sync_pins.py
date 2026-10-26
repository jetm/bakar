"""The kas form of ``bakar sync``: move a release workspace's shared clones onto their pins.

``bakar sync <kas file>`` is the only way to reach the pin sync; without the
positional, ``sync`` keeps its manifest form untouched. The module holds only
the command-level flow (refusals, workspace resolution, the dry run, the exit
code); probing, the script and its parsing live in ``steps.pin_probe`` and
``steps.pin_sync``. A ``--revert`` entry point belongs beside
:func:`run_pin_sync` and reuses :func:`_resolve_target` and :func:`_emit`.

Exit codes: 0 every existing clone ends at its pin, 1 a clone was left alone or
failed or the sync was refused, 2 a usage error. Every usage error returns
before anything is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bakar.commands._app import console

if TYPE_CHECKING:
    from bakar.steps.repo_reuse import PinnedRepo, RepoFacts

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_USAGE = 2

NOT_META_AVOCADO_MSG = "the pin sync supports meta-avocado release workspaces only"


@dataclass(frozen=True, kw_only=True)
class SyncPinsRequest:
    """What ``bakar sync`` was given, as far as the kas form cares."""

    kas_yaml: str | None
    on: str | None
    workspace: Path | None
    dry_run: bool
    # Names of the manifest-form flags the caller set (``--machine``, ...).
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Target:
    """Where a sync runs and which repositories it covers."""

    workspace: Path
    host: str | None
    entries: list[PinnedRepo]


def _emit(line: str) -> None:
    # Names and paths come from kas files, so none of it may be read as Rich markup.
    console.print(line, markup=False, highlight=False, soft_wrap=True)


def _fail(message: str) -> int:
    console.print(message, style="red", markup=False, highlight=False, soft_wrap=True)
    return EXIT_USAGE


def _plain(markup_text: str) -> str:
    """Undo the Rich ``\\[`` escape the shared build messages carry."""
    return markup_text.replace("\\[", "[")


def refuse_request(req: SyncPinsRequest) -> str | None:
    """Return why ``req`` is not a valid kas-form invocation, or None."""
    if req.kas_yaml is None:
        return "--on needs a kas file: bakar sync <kas file> --on HOST" if req.on is not None else None
    if req.conflicts:
        return f"a kas file cannot be combined with {', '.join(req.conflicts)}"
    if req.on is not None:
        if req.on.startswith("-"):
            return f"--on host {req.on!r} begins with '-'"
        if req.workspace is not None:
            return (
                "-w cannot be combined with --on: the release maps to the workspace on the host, "
                "so naming one here would be ignored"
            )
    return None


def _meta_avocado_target(kas_arg: str) -> tuple[Path, Path] | str:
    """Return ``(kas file, meta-avocado checkout)``, or the refusal message."""
    from bakar.bsp_detect import is_meta_avocado_yaml, meta_avocado_checkout
    from bakar.commands._workspace import split_kas_yaml_arg

    if ":" in kas_arg:
        return "the pin sync takes one kas file, not main.yml:overlay.yml"
    main_yaml, _extras = split_kas_yaml_arg(kas_arg)
    if main_yaml is None or not is_meta_avocado_yaml(main_yaml):
        return NOT_META_AVOCADO_MSG
    checkout = meta_avocado_checkout(main_yaml)
    if checkout is None:
        return f"cannot sync {main_yaml}: no meta-avocado checkout holds it"
    return main_yaml, checkout


def _resolve_local(req: SyncPinsRequest, main_yaml: Path, checkout: Path) -> _Target | int:
    from bakar.commands._workspace import _resolve_workspace
    from bakar.release_workspace import ReleaseRoutingError
    from bakar.steps import kas_pins
    from bakar.steps.repo_reuse import pinned_entries

    workspace = _resolve_workspace(req.workspace, kas_yaml=main_yaml, family="generic")
    if not workspace.is_dir():
        return _fail(f"workspace {workspace} does not exist")
    try:
        _release, repos = kas_pins.probe_pins(main_yaml, checkout)
    except ReleaseRoutingError as exc:
        return _fail(f"Cannot read the pins of {main_yaml}: {exc}")
    return _Target(workspace, None, pinned_entries(repos, workspace))


def _resolve_host(host: str, main_yaml: Path, checkout: Path) -> _Target | int:
    """Resolve the host workspace exactly as ``bakar build --on`` does, then check it."""
    from bakar.commands.build import _NO_ROUTING_MSG, _dispatch_user_config
    from bakar.release_workspace import ReleaseRoutingError, map_release
    from bakar.steps import kas_pins, remote_dispatch
    from bakar.steps.repo_reuse import pinned_entries

    user_config = _dispatch_user_config()
    if user_config is None:
        return EXIT_USAGE
    if not (user_config.workspaces_template or user_config.workspaces_releases):
        return _fail(f"{_plain(_NO_ROUTING_MSG)}: it names the release workspace on {host}.")
    try:
        release, repos = kas_pins.probe_pins(main_yaml, checkout)
        routed = map_release(release, user_config.workspaces_template, user_config.workspaces_releases)
    except ReleaseRoutingError as exc:
        return _fail(f"Cannot route {main_yaml} to a release workspace: {exc}")
    if routed is None:
        return _fail(
            f"{_plain(_NO_ROUTING_MSG)} (or a [workspaces.releases] entry for {release!r}): "
            f"it names the release workspace on {host}."
        )
    try:
        remote_dispatch.check_remote_workspace(host, str(routed), release)
    except remote_dispatch.DispatchError as exc:
        _emit(f"cannot sync on {host}: {exc}")
        return EXIT_USAGE
    return _Target(routed, host, pinned_entries(repos, routed))


def _resolve_target(req: SyncPinsRequest) -> _Target | int:
    """Validate ``req`` and resolve its workspace and entries; an int is the exit code."""
    problem = refuse_request(req)
    if problem is not None:
        return _fail(problem)
    if req.kas_yaml is None:
        return EXIT_USAGE
    found = _meta_avocado_target(req.kas_yaml)
    if isinstance(found, str):
        return _fail(found)
    main_yaml, checkout = found
    if req.on is not None:
        return _resolve_host(req.on, main_yaml, checkout)
    return _resolve_local(req, main_yaml, checkout)


def dry_run_lines(entries: list[PinnedRepo], facts: list[RepoFacts], rejected: dict[int, str]) -> list[str]:
    """One line per pinned repository saying what a sync would do with it."""
    lines: list[str] = []
    for index, (entry, fact) in enumerate(zip(entries, facts, strict=True)):
        pin = entry.pin
        if pin is None:
            continue
        if index in rejected:
            verdict = f"cannot be synced ({rejected[index]})"
        elif not fact.exists:
            verdict = "missing"
        elif fact.head is not None and fact.head.startswith(pin):
            verdict = "already at its pin"
        elif fact.clean is not True:
            verdict = "left alone because of local changes" if fact.clean is False else "left alone, state unreadable"
        else:
            verdict = f"would move to {pin[:12]}"
        lines.append(f"{entry.name}: {verdict}")
    return lines


def _dry_run(target: _Target) -> int:
    from bakar.steps import pin_probe, pin_sync

    try:
        if target.host is None:
            facts = pin_probe.probe_local(target.entries)
        else:
            facts = pin_probe.probe_host(target.host, target.entries)
    except pin_probe.PinProbeIncomplete as exc:
        _emit(f"cannot inspect the clones: {exc}")
        return EXIT_INCOMPLETE
    rejected = pin_sync.validate_sync_entries(target.entries)
    for line in dry_run_lines(target.entries, facts, rejected):
        _emit(line)
    return EXIT_OK


def _sync(target: _Target) -> int:
    from bakar.steps import pin_sync

    rejected = pin_sync.validate_sync_entries(target.entries)
    script = pin_sync.sync_script(target.workspace, target.entries, rejected)
    if target.host is None:
        rc, stdout, stderr = pin_sync.run_local(script)
    else:
        rc, stdout, stderr = pin_sync.run_host(target.host, script)
    report = pin_sync.parse_sync(stdout, target.entries, rejected, rc)
    for line in pin_sync.render_sync(report, target.entries):
        _emit(line)
    if not report.complete and stderr.strip():
        _emit(f"sync script stderr: {stderr.strip()[:500]}")
    settled = all(row.status in ("MOVED", "AT_PIN", "MISSING") for row in report.rows)
    return EXIT_OK if report.complete and settled else EXIT_INCOMPLETE


def run_pin_sync(req: SyncPinsRequest) -> int:
    """Run the kas form of ``bakar sync`` and return its exit code."""
    target = _resolve_target(req)
    if isinstance(target, int):
        return target
    if req.dry_run:
        return _dry_run(target)
    return _sync(target)
