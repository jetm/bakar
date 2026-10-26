"""The kas form of ``bakar sync``: move a release workspace's shared clones onto their pins.

``bakar sync <kas file>`` is the only way to reach the pin sync; without the
positional, ``sync`` keeps its manifest form untouched. The module holds only
the command-level flow (refusals, workspace resolution, the dry run, the exit
code); probing, the script and its parsing live in ``steps.pin_probe`` and
``steps.pin_sync``. The ``--revert`` entry point, :func:`_revert`, does not
resolve a kas file: it derives the workspace from the record path itself
(``<workspace>/.bakar/sync-revert/<record>``) and prints through :func:`_emit`.

Exit codes: 0 every existing clone ends at its pin, 1 a clone was left alone or
failed, the sync was refused, or the host's release workspace could not be
checked, 2 a usage error. Every usage error returns before anything is written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from bakar.commands._app import console

if TYPE_CHECKING:
    from bakar.steps.repo_reuse import PinnedRepo, RepoFacts, RepoState

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_USAGE = 2

NOT_META_AVOCADO_MSG = "the pin sync supports meta-avocado release workspaces only"

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")


def printable(text: str) -> str:
    """Replace ASCII control characters with ``?`` so a kas-file value cannot drive the terminal."""
    return _CONTROL_CHARS.sub("?", text)


@dataclass(frozen=True, kw_only=True)
class SyncPinsRequest:
    """What ``bakar sync`` was given, as far as the kas form cares."""

    kas_yaml: str | None
    on: str | None
    workspace: Path | None
    dry_run: bool
    # Names of the manifest-form flags the caller set (``--machine``, ...).
    conflicts: tuple[str, ...] = ()
    # ``--revert FILE``: a record written by an earlier sync, read where the revert runs.
    revert: str | None = None


@dataclass(frozen=True)
class _Target:
    """Where a sync runs and which repositories it covers."""

    workspace: Path
    host: str | None
    entries: list[PinnedRepo]
    # Whether the build pairs a diverged openembedded-core with a frozen bitbake (host mode).
    pair_core_with_bitbake: bool = True


def _emit(line: str) -> None:
    # Names and paths come from kas files, so none of it may be read as Rich markup
    # or reach the terminal as a control sequence.
    console.print(printable(line), markup=False, highlight=False, soft_wrap=True)


def _fail(message: str) -> int:
    console.print(message, style="red", markup=False, highlight=False, soft_wrap=True)
    return EXIT_USAGE


def _plain(markup_text: str) -> str:
    """Undo the Rich ``\\[`` escape the shared build messages carry."""
    return markup_text.replace("\\[", "[")


def _refuse_revert(req: SyncPinsRequest) -> str | None:
    """Return why ``req`` is not a valid ``--revert`` invocation, or None."""
    clashing = list(req.conflicts)
    if req.kas_yaml is not None:
        clashing.insert(0, "a kas file")
    if req.dry_run:
        clashing.append("--dry-run")
    if req.workspace is not None:
        clashing.append("-w")
    if clashing:
        return f"--revert cannot be combined with {', '.join(clashing)}"
    if req.on is not None and req.on.startswith("-"):
        return f"--on host {req.on!r} begins with '-'"
    return None


def refuse_request(req: SyncPinsRequest) -> str | None:
    """Return why ``req`` is not a valid kas-form invocation, or None."""
    if req.revert is not None:
        return _refuse_revert(req)
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
    return _Target(workspace, None, pinned_entries(repos, workspace), _local_host_mode(workspace, main_yaml))


def _local_host_mode(workspace: Path, main_yaml: Path) -> bool:
    """Whether a local ``bakar build`` of ``main_yaml`` would run in host mode.

    The build pairs a diverged openembedded-core with a frozen bitbake only in
    host mode, so the re-check and the dry run resolve the mode the same way.
    """
    import bakar.commands._app as _state
    from bakar.commands._helpers import resolve_global_config
    from bakar.config import ResolveRequest

    request = ResolveRequest(
        workspace=workspace, bsp_family="generic", kas_yaml=main_yaml, user_config=_state._USER_CONFIG
    )
    return resolve_global_config(request).host_mode


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
        return EXIT_INCOMPLETE
    # The host's own build mode (its config, its BAKAR_CONTAINER) is not visible
    # from here, so pair as a host-mode build does; a container build there may
    # still refuse a diverged openembedded-core this check lets through.
    return _Target(routed, host, pinned_entries(repos, routed), pair_core_with_bitbake=True)


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


def _dry_run_verdicts(
    entries: list[PinnedRepo], facts: list[RepoFacts], rejected: dict[int, str]
) -> list[tuple[str, bool]]:
    """``(line, settled)`` per entry; unsettled is what the real sync reports as LEFT or FAIL."""
    verdicts: list[tuple[str, bool]] = []
    for index, (entry, fact) in enumerate(zip(entries, facts, strict=True)):
        pin = entry.pin
        if pin is None:
            # The sync prints no row for it; the re-check reports it as a blocker.
            verdicts.append((f"{entry.name}: no commit pin, sync leaves it alone and a build refuses it", True))
            continue
        settled = False
        # The same order as the script: an unreadable state, then the pin, then the
        # pin's length, then local changes, so a clone off an abbreviated pin fails for
        # its length even when dirty.
        full = _FULL_SHA.fullmatch(pin) is not None
        if index in rejected:
            verdict = f"cannot be synced ({rejected[index]})"
        elif not fact.exists:
            verdict, settled = "missing", True
        elif fact.clean is None:
            verdict = "left alone, state unreadable"
        elif fact.head is not None and fact.head.lower().startswith(pin.lower()):
            # The script asks git to resolve an abbreviated pin, which a prefix test cannot do.
            at = (
                "already at its pin" if full else "HEAD starts with its abbreviated pin, which a dry run cannot resolve"
            )
            verdict, settled = at + (" (has local changes)" if not fact.clean else ""), True
        elif not full:
            verdict = "cannot be synced (pin is not a full 40-character commit id)"
        elif not fact.clean:
            verdict = "left alone because of local changes"
        else:
            verdict, settled = f"would move to {pin[:12]}", True
        verdicts.append((f"{entry.name}: {verdict}", settled))
    return verdicts


def dry_run_lines(entries: list[PinnedRepo], facts: list[RepoFacts], rejected: dict[int, str]) -> list[str]:
    """One line per pinned repository saying what a sync would do with it."""
    return [line for line, _settled in _dry_run_verdicts(entries, facts, rejected)]


def _probe(target: _Target) -> list[RepoFacts]:
    from bakar.steps import pin_probe

    if target.host is None:
        return pin_probe.probe_local(target.entries)
    return pin_probe.probe_host(target.host, target.entries)


def predicted_facts(entries: list[PinnedRepo], facts: list[RepoFacts], rejected: dict[int, str]) -> list[RepoFacts]:
    """``facts`` as they would read after the sync: each clone it moves sits clean at its pin."""
    predicted: list[RepoFacts] = []
    for index, (entry, fact) in enumerate(zip(entries, facts, strict=True)):
        pin = entry.pin
        moves = (
            index not in rejected
            and pin is not None
            and _FULL_SHA.fullmatch(pin) is not None
            and fact.exists
            and fact.clean is True
            and fact.head is not None
            and not fact.head.lower().startswith(pin.lower())
        )
        predicted.append(replace(fact, head=pin, clean=True, pin_present=True) if moves else fact)
    return predicted


def _dry_run(target: _Target) -> int:
    from bakar.steps import pin_probe, pin_sync, repo_reuse

    try:
        facts = _probe(target)
    except pin_probe.PinProbeIncomplete as exc:
        _emit(f"cannot inspect the clones: {exc}")
        return EXIT_INCOMPLETE
    rejected = pin_sync.validate_sync_entries(target.entries)
    verdicts = _dry_run_verdicts(target.entries, facts, rejected)
    for line, _settled in verdicts:
        _emit(line)
    after = predicted_facts(target.entries, facts, rejected)
    states = repo_reuse.classify_facts(target.entries, after, pair_core_with_bitbake=target.pair_core_with_bitbake)
    remaining = _blocker_lines(states, one="repository would still block", many="repositories would still block")
    for line in remaining:
        _emit(line)
    # The real sync exits 1 on any LEFT or FAIL row, so the prediction does too.
    settled = all(ok for _line, ok in verdicts)
    return EXIT_OK if settled and not remaining else EXIT_INCOMPLETE


def _sync(target: _Target) -> int:
    from bakar.steps import pin_sync

    rejected = pin_sync.validate_sync_entries(target.entries)
    script = pin_sync.sync_script(target.workspace, target.entries, rejected)
    if target.host is None:
        rc, stdout, stderr = pin_sync.run_local(script)
    else:
        rc, stdout, stderr = pin_sync.run_host(target.host, script)
    report = pin_sync.parse_sync(stdout, target.entries, rejected, rc)
    lines = pin_sync.render_sync(report, target.entries, workspace=target.workspace, host=target.host)
    settled = all(row.status in ("MOVED", "AT_PIN", "MISSING") for row in report.rows)
    code = EXIT_OK if report.complete and settled else EXIT_INCOMPLETE
    # The completion line stays last, so what still blocks a build goes ahead of it.
    for line in lines[:-1]:
        _emit(line)
    if report.complete:
        remaining = _blockers_after_sync(target)
        if remaining:
            code = EXIT_INCOMPLETE
            for line in remaining:
                _emit(line)
    _emit(lines[-1])
    if not report.complete and stderr.strip():
        _emit(f"sync script stderr: {stderr.strip()[:500]}")
    return code


def _blocker_lines(states: list[RepoState], *, one: str, many: str) -> list[str]:
    """The heading and one line per blocking state; empty when nothing blocks."""
    from bakar.steps import repo_reuse

    blockers = repo_reuse.blocking(states)
    if not blockers:
        return []
    noun = one if len(blockers) == 1 else many
    return [
        f"after the sync {len(blockers)} {noun} a build, and bakar sync cannot clear it:",
        *repo_reuse.render_blockers(states),
    ]


def _blockers_after_sync(target: _Target) -> list[str]:
    """Lines naming what still stops a build after the sync; empty when nothing does.

    A clone sync leaves alone (dirty, no usable pin, a dirty side checkout) can
    still make the build refuse, and the rows above only say where HEAD is.
    """
    from bakar.steps import pin_probe, repo_reuse

    try:
        facts = _probe(target)
    except pin_probe.PinProbeIncomplete as exc:
        return [f"cannot re-check the clones after the sync: {exc}"]
    states = repo_reuse.classify_facts(target.entries, facts, pair_core_with_bitbake=target.pair_core_with_bitbake)
    return _blocker_lines(states, one="repository still blocks", many="repositories still block")


def _revert_workspace(record: str, host: str | None) -> tuple[str, Path] | str:
    """Return ``(record path, workspace)``, or the refusal when ``record`` is not under ``.bakar/sync-revert/``."""
    import os.path
    from pathlib import PurePosixPath

    if host is None:
        path = Path(os.path.abspath(record))
    else:
        # A relative path would resolve against the host login directory, a
        # workspace nobody checked, so a host record must be named in full.
        if not PurePosixPath(record).is_absolute():
            return "--revert FILE on a host must be an absolute path"
        # A host path: taken as given, only normalised lexically.
        path = Path(PurePosixPath(os.path.normpath(record)))
    parent = path.parent
    if parent.name != "sync-revert" or parent.parent.name != ".bakar" or path.name in ("", ".", ".."):
        return f"{record} is not inside <workspace>/.bakar/sync-revert/"
    return str(path), parent.parent.parent


def _revert(req: SyncPinsRequest) -> int:
    from bakar.steps import pin_sync

    if req.revert is None:
        return EXIT_USAGE
    problem = refuse_request(req)
    if problem is not None:
        return _fail(problem)
    found = _revert_workspace(req.revert, req.on)
    if isinstance(found, str):
        return _fail(found)
    record, workspace = found
    script = pin_sync.revert_script(workspace, record)
    if req.on is None:
        rc, stdout, stderr = pin_sync.run_local(script)
    else:
        rc, stdout, stderr = pin_sync.run_host(req.on, script)
    report = pin_sync.parse_revert(stdout, rc)
    for line in pin_sync.render_revert(report):
        _emit(line)
    if not report.complete and stderr.strip():
        _emit(f"revert script stderr: {stderr.strip()[:500]}")
    settled = all(status in ("RESTORED", "DETACHED") for _n, status, _d in report.rows)
    return EXIT_OK if report.complete and settled else EXIT_INCOMPLETE


def run_pin_sync(req: SyncPinsRequest) -> int:
    """Run the kas form of ``bakar sync`` (or its ``--revert``) and return its exit code."""
    if req.revert is not None:
        return _revert(req)
    target = _resolve_target(req)
    if isinstance(target, int):
        return target
    if req.dry_run:
        return _dry_run(target)
    return _sync(target)
