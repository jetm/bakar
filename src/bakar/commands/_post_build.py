"""Post-build work for ``bakar build``: the CVE report, the SBOM filter, the feed sync.

Each flag contributes a frozen request dataclass, a resolver that runs before the
build and returns None when the flag must not run, and the step ``_finish_build``
drives once the build has succeeded.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import typer

from bakar import cve_report, feed_ops, feed_preflight, sbom_publish
from bakar.commands._app import console
from bakar.diagnostics import Status
from bakar.steps import kas_build as step_kas

# Runtime, not TYPE_CHECKING-guarded. ``_CveRequest.kas_ctx`` is annotated with
# it, and guarding the name makes ``typing.get_type_hints`` on that dataclass
# raise NameError - a narrowing against the pre-split ``build.py``, which
# imported it unguarded. The guard also defers nothing, since ``step_kas`` above
# is the same module at runtime. TC001 is ignored for this package precisely so
# this import can stay here.
from bakar.steps.kas_build import KasBuildContext

# The recipe in meta-avocado-sbom. EXCLUDE_FROM_WORLD, so naming it explicitly
# is the only way to reach it.
_CVE_REPORT_TARGET = "avocado-cve-report"


@dataclass(frozen=True)
class _CveRequest:
    """What ``--cve`` needs to run: the build's kas context and its overlays.

    The overlays are carried separately because ``run_build`` layers them from
    its keyword argument and never reads ``KasBuildContext.extra_overlays``. A
    report run derived from the context alone would therefore build against the
    bare YAML - and ``kas/feature/cve-check.yml``, which is what puts
    ``meta-avocado-sbom`` in bblayers, is normally stacked with colon syntax and
    arrives as exactly one of these overlays. Dropping them makes
    ``avocado-cve-report`` an unknown target on the one invocation the flag is
    for.
    """

    kas_ctx: KasBuildContext
    extra_overlays: list[Path]


def _resolve_cve_request(*, cve: bool, dry_run: bool) -> bool:
    """Report whether ``--cve`` should run after this build.

    The dry-run filter lives here rather than at the rc check for the same
    reason ``--feed``'s does: ``run_build`` prints its preview and returns 0, so
    rc reads a dry run as a success. Producing a report then would summarise
    whatever a PREVIOUS build left in ``CVE_CHECK_DIR`` - a valid-looking
    document describing a package set this invocation never wrote, from a
    command documented to exit before invoking kas.

    Unlike ``--feed`` there is no prerequisite probe to run up front. The
    prerequisite is cve-check data, which does not exist until the build has
    run, and the cost of finding out late is bounded: the check is a ``glob``
    and the build's artifacts are already on disk either way. ``--feed``'s
    pre-build gate exists because a missing ``createrepo_c`` costs a whole
    build's wall clock to learn, which does not apply here.
    """
    if not cve:
        return False
    if dry_run:
        console.print("[yellow]--dry-run: skipping --cve[/] (no build ran, so there is nothing to report on).")
        return False
    return True


def _generate_cve_report(cfg, request: _CveRequest, machine: str) -> None:
    """Run ``avocado-cve-report`` against the finished build.

    Skips rather than fails when the build carries no cve-check results. That is
    the ordinary shape of a build without ``kas/feature/cve-check.yml`` stacked,
    and the recipe would answer it with a ``bb.fatal`` after a full kas startup -
    a minute spent learning what a ``glob`` already established.

    A failure in the run itself does not fail the build, matching ``_sync_feed``:
    the build succeeded and its artifacts are on disk, and turning a post-step
    failure into a non-zero exit discards that over something the user can
    repeat with the one command named in the message.
    """
    cve_dir = cve_report.cve_data_dir(cfg, machine)
    if not cve_report.has_cve_data(cve_dir):
        console.print(
            f"[yellow]--cve: no cve-check results in {cve_dir}[/], so there is nothing to report on. "
            "Stack `kas/feature/cve-check.yml` onto the build and run it again."
        )
        return

    # dry_run is forced off rather than inherited: the caller already filtered a
    # dry run out, so a True here could only be stale - and would print a preview
    # while this function reported a report as produced.
    rc = step_kas.run_build(
        replace(request.kas_ctx, target=_CVE_REPORT_TARGET, dry_run=False),
        extra_overlays=request.extra_overlays,
    )
    if rc != 0:
        console.print(
            f"[yellow]build succeeded but the CVE report was not produced[/] "
            f"({_CVE_REPORT_TARGET} exited {rc}). "
            f"Re-run `bakar build {cfg.kas_yaml} -t {_CVE_REPORT_TARGET}` to see why."
        )
        return

    console.print(f"CVE report: {cve_report.report_path(cfg, machine)}")


@dataclass(frozen=True)
class _SbomRequest:
    """What ``--sbom`` needs to run: the workspace holding meta-avocado-sbom."""

    workspace: Path


def _resolve_sbom_request(cfg, *, sbom: bool, dry_run: bool) -> _SbomRequest | None:
    """Return the SBOM request for this build, or None when ``--sbom`` must not run.

    The missing-prerequisite case EXITS rather than returning None, which is the
    one place this diverges from ``--cve``. A checkout without the filter can
    never produce a publishable document, and that is knowable in a stat now
    versus a whole build's wall clock at the end - the same argument ``--feed``
    makes about ``createrepo_c``.

    Skipping instead would be worse than either: the per-image document exists
    whether or not the filter does, it carries vulnerability data (measured: 868
    ``security_*`` nodes and 303 CVE identifiers), and a silent skip leaves the
    user believing an SBOM step ran.
    """
    if not sbom:
        return None
    if dry_run:
        console.print("[yellow]--dry-run: skipping --sbom[/] (no build ran, so there is no document to filter).")
        return None

    lib = sbom_publish.sbom_lib_dir(cfg.workspace)
    if not sbom_publish.has_filter(lib):
        console.print(
            f"[red]--sbom cannot run: no publication filter at {lib}[/]. The per-image SPDX document "
            "carries vulnerability data and must be filtered before it can be published, and this "
            "meta-avocado checkout does not carry the filter that does it."
        )
        console.print("Update meta-avocado, or drop --sbom to build without producing a publishable inventory.")
        raise typer.Exit(code=2)

    return _SbomRequest(workspace=cfg.workspace)


def _filter_image_sbom(cfg, request: _SbomRequest) -> list[Path]:
    """Filter the build's per-image SPDX into a publishable inventory.

    Does not fail the build on any outcome, matching ``_sync_feed`` and
    ``_generate_cve_report``: the build succeeded and its artifacts are on disk.

    The independent leak check after the filter runs is not redundant with the
    filter's own ``--check``. It answers a narrower question at the moment that
    matters - is THIS file safe to publish - and it fails closed on a document it
    cannot read, because an unparseable file is not a file with no CVEs in it.
    """
    images = sbom_publish.images_dir(cfg)
    documents = sbom_publish.find_image_sboms(images)
    if not documents:
        console.print(
            f"[yellow]--sbom: no per-image SBOM under {images}[/]. A distro build only emits one when "
            "the image recipe's do_build is reached; check that avocado-distro depends on it."
        )
        return []

    out_dir = cfg.resolved_tmpdir / "deploy" / "avocado-sbom"
    cmd, env = sbom_publish.filter_command(sbom_publish.sbom_lib_dir(request.workspace), images, out_dir)
    try:
        # No shell: filter_command is typed ``-> tuple[list[str], dict[str, str]]``
        # and returns an argv LIST, which subprocess.run executes directly rather
        # than through /bin/sh. argv[0] is always the literal "python3"
        # (sbom_publish.py builds a sys.executable ternary and then overwrites it
        # unconditionally, so the ternary is dead), argv[1:3] are constants, and
        # the two caller-derived paths are separate argv members that cannot open
        # a second command however they are spelled. The rule fires on any
        # non-literal first argument and does not distinguish list from shell.
        #
        # ARGV is not the whole surface, and the rule does not look at the rest.
        # filter_command also derives env["PYTHONPATH"] from request.workspace, so
        # the MODULE this runs comes from a caller-supplied directory: executing
        # the workspace's own publication filter is the design, not an oversight.
        # That is a trust decision about the workspace, not an injection - the
        # operator who passes --workspace can already run python directly - but it
        # is the part a reader auditing "does anything caller-controlled reach
        # execution here" needs, and argv-list form says nothing about it.
        #
        # The directive must stay on the line directly above the call: opengrep
        # only associates it with the line it precedes, so moving the prose
        # between the two silently un-suppresses the finding.
        # nosemgrep: python.django.security.injection.command.subprocess-injection.subprocess-injection
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    except OSError as exc:
        console.print(f"[yellow]build succeeded but the SBOM was not filtered:[/] {exc}")
        return []

    if result.returncode != 0:
        console.print(
            f"[yellow]build succeeded but the SBOM filter exited {result.returncode}[/]: "
            f"{(result.stderr or '').strip().splitlines()[-1] if (result.stderr or '').strip() else 'no output'}"
        )
        return []

    filtered = sbom_publish.find_image_sboms(out_dir)
    leaks = [reason for document in filtered for reason in sbom_publish.vulnerability_leaks(document)]
    if leaks:
        console.print("[red]--sbom: the filtered document is not publishable.[/] It still carries:")
        for reason in leaks:
            console.print(f"  {reason}")
        console.print("Do NOT publish it. This is a filter defect or a document shape it did not anticipate.")
        return []

    for document in filtered:
        console.print(f"SBOM (publishable): {document}")
    return filtered


@dataclass(frozen=True)
class _FeedRequest:
    """What ``--feed`` needs to run: the build's YAML and where to render it."""

    kas_yaml: Path
    release: str
    channel: str


def _resolve_feed_request(
    cfg,
    *,
    feed: bool,
    dry_run: bool,
    release: str,
    channel: str,
) -> _FeedRequest | None:
    """Return the feed request for this build, or None when --feed must not run.

    Two reasons it returns None with the flag set. A dry run is one: ``run_build``
    prints its preview and returns 0, so rc alone reads it as a success, and
    syncing would stage whatever a PREVIOUS build happened to leave in the deploy
    tree and repin every client onto a fresh snapshot of stale RPMs - from a
    command documented to exit before invoking kas.

    The other is a failed prerequisite. The checks run HERE, before the build,
    rather than where ``bakar feed sync`` runs them, because this path's whole
    economics differ: a missing ``createrepo_c`` discovered after a multi-hour
    build costs that build's wall clock to learn, while the same probe costs
    milliseconds now. A blocking result refuses the whole command rather than
    downgrading to a warning - the user asked for a feed, and finding out at the
    end that they cannot have one is the outcome being avoided.
    """
    if not feed:
        return None
    if dry_run:
        console.print("[yellow]--dry-run: skipping --feed[/] (no build ran, so there is nothing new to stage).")
        return None

    # release/channel are passed so the codename cross-check runs here too: a
    # --feed-release that disagrees with the build's DISTRO_CODENAME renders into
    # a channel no client resolves, and that is worth catching before the build
    # rather than after it.
    results = feed_ops.preflight_results(cfg, cfg.kas_yaml, release=release, channel=channel)
    if feed_preflight.blocking(results):
        console.print("[red]--feed cannot run: the feed prerequisites are not met.[/]")
        for result in results:
            if result.status is Status.PASS:
                continue
            console.print(f"FAIL {result.name}: {result.message}")
            if result.fix_hint:
                console.print(f"     -> {result.fix_hint}")
        console.print("Fix the above, or drop --feed to build without touching the feed.")
        raise typer.Exit(code=2)

    return _FeedRequest(kas_yaml=cfg.kas_yaml, release=release, channel=channel)


def _sync_feed(cfg, request: _FeedRequest, *, sboms: list[Path] | None = None) -> None:
    """Stage the finished build into the feed, then rewrite ``targets.json``.

    Failures here do not fail the build. The build itself succeeded and its
    artifacts are on disk; turning a feed problem into a non-zero build exit
    would discard hours of work over a step the user can repeat with
    ``bakar feed sync``. The reason is printed with that hint instead.

    The except clause is deliberately broad. The paragraph above states an
    absolute - no feed problem fails the build - and a tuple of the failures
    currently anticipated does not implement it: ``parse_repo_map`` reads the map
    with no encoding, so one non-UTF-8 byte raises ``UnicodeDecodeError`` (a
    ``ValueError``, not an ``OSError``) and a signature drift in the feed layer
    raises ``TypeError``. Either would escape a narrow tuple and reach the user
    as a traceback AFTER "build succeeded" has printed, which is the one outcome
    this path exists to prevent.
    """
    try:
        result = feed_ops.sync_then_index(
            cfg,
            request.kas_yaml,
            release=request.release,
            channel=request.channel,
            sboms=sboms,
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring: the contract is absolute
        console.print(f"[yellow]build succeeded but the feed was not updated:[/] {feed_ops.describe_failure(exc)}")
        console.print(
            f"Re-run `bakar feed sync --release {request.release} --channel {request.channel} "
            f"{request.kas_yaml}` once the cause is fixed; the build output is untouched."
        )
        return
    console.print(f"feed: snapshot {result['snapshot']} pinned for {', '.join(result['machines']) or '(none)'}")
    if result["unstaged"]:
        console.print(f"declared but not built: {', '.join(result['unstaged'])}")
