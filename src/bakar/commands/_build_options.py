"""Named ``Annotated`` aliases for ``bakar build``'s option surface.

Only the annotation expressions live here; the parameters, their order and
their defaults stay in ``build()``. Typer renders one CLI flag per parameter,
so the alias set is a relocation of text and never a change of arity.

Every alias is defined at MODULE level, deliberately. Typer resolves a
command's annotations at runtime through ``inspect.signature(eval_str=True)``,
so a name hidden behind ``if TYPE_CHECKING:`` is unresolvable there and the
command dies with ``NameError: name '<alias>' is not defined`` while the
signature is being resolved. ``TC001`` is suppressed for this package in
``pyproject.toml`` for exactly that reason - the lint is correct about the
import being type-only in appearance, and wrong about it being type-only in
fact.

The failure is a ``NameError``, not the ``RuntimeError: Type not yet
supported`` an earlier version of this docstring named. That RuntimeError is
real but unrelated: Typer raises it for a RESOLVED annotation it has no branch
for, which is what a dataclass-typed parameter produces, and the guarded-alias
case never reaches that code. Someone who hits the NameError and greps for the
RuntimeError finds nothing and concludes this suppression is unrelated to their
symptom - which is the one thing this note exists to prevent.

``--preset`` has no alias here on purpose. Its annotation carries
``autocompletion=_preset_completer``, which lives in ``_build_flavors``, and
that module ends with a back-import of ``build``. Reaching the completer from
this module would close a ``build -> _build_options -> _build_flavors ->
build`` cycle that breaks outright when ``_build_options`` is imported first:
``_build_flavors`` would drive ``build.py`` to import names this module has
not bound yet. ``--preset`` therefore keeps its annotation inline in
``build.py``, where the completer is already a resolved name.

``workspace`` has no alias here either - ``_helpers.WorkspaceOption`` already
is one, shared with every other subcommand that takes a workspace.
"""

from __future__ import annotations

from typing import Annotated

import typer

KasYamlArgument = Annotated[
    str | None,
    typer.Argument(
        help="Optional kas YAML (BYO form). Colon-separated overlays are supported: "
        "main.yml:overlay.yml. When set, sync/setup-env/gen-kas are skipped.",
    ),
]

MachineOption = Annotated[str | None, typer.Option("--machine", "-m", help="e.g. imx8mp-var-dart, am62x-var-som")]

DistroOption = Annotated[str | None, typer.Option("--distro", "-d", help="e.g. fsl-imx-xwayland, arago")]

ImageOption = Annotated[
    str | None,
    typer.Option("--image", "-i", help="e.g. core-image-minimal, var-thin-image"),
]

TargetOption = Annotated[
    str | None,
    typer.Option(
        "--target",
        "-t",
        help="kas target override (kas build --target <TARGET>), e.g. avocado-complete; "
        "unset builds the YAML's own target",
    ),
]

ManifestOption = Annotated[
    str | None,
    typer.Option(
        "--manifest",
        "-f",
        help="manifest filename (NXP imx-*.xml or TI processor-sdk-*-config_var<N>.txt)",
    ),
]

BranchOption = Annotated[
    str | None,
    typer.Option(
        "--branch",
        "-b",
        help="branch override; inferred from manifest filename when omitted",
    ),
]

SkipSyncOption = Annotated[
    bool, typer.Option("--skip-sync", help="Skip sync (repo init+sync for NXP, oe-layertool for TI)")
]

DryRunOption = Annotated[
    bool, typer.Option("--dry-run", "-n", help="Regenerate YAML and exit before invoking kas/kas-container build")
]

KeepGoingOption = Annotated[
    bool,
    typer.Option(
        "--keep-going",
        "-k",
        help="Pass -k to bitbake: continue building other targets when one fails",
    ),
]

CleanOption = Annotated[
    bool,
    typer.Option(
        "--clean",
        help="Remove <bsp>/build/ before running the pipeline (forces a from-scratch build).",
    ),
]

ShowLayersOption = Annotated[
    bool,
    typer.Option("--show-layers", help="Print layer git hashes before build."),
]

SstateMirrorOption = Annotated[
    str | None,
    typer.Option("--sstate-mirror", help="HTTP sstate/downloads mirror URL; enables the shared-cache overlay"),
]

DryRunScriptOption = Annotated[
    str | None,
    typer.Option(
        "--dry-run-script",
        help="Write a runnable bash script reproducing this build to PATH, or to stdout when PATH is '-'. "
        "Does not build. The existing --dry-run/-n preview behavior is unchanged.",
    ),
]

OnOption = Annotated[
    str | None,
    typer.Option(
        "--on",
        help="Dispatch the build to a remote host (ssh alias or user@ip) instead of building "
        "locally: mirror the working tree with rsync, run the build there, stream logs, and "
        "surface the remote run-id. Unset builds locally.",
    ),
]

YesOption = Annotated[
    bool,
    typer.Option(
        "--yes",
        "-y",
        help="Skip the rsync --delete confirmation prompt for --on dispatch (non-interactive).",
    ),
]

FeedOption = Annotated[
    bool,
    typer.Option(
        "--feed",
        help="On build success, stage the RPMs into the local package feed and rewrite its "
        "index. A failed build never syncs, and neither does --dry-run.",
    ),
]

FeedReleaseOption = Annotated[
    str,
    typer.Option("--feed-release", help="Feed release directory for --feed (see `bakar feed sync`)."),
]

FeedChannelOption = Annotated[
    str,
    typer.Option("--feed-channel", help="Feed channel directory for --feed (see `bakar feed sync`)."),
]

CveOption = Annotated[
    bool,
    typer.Option(
        "--cve",
        help="On build success, run avocado-cve-report to correlate the runtime packages with "
        "unpatched CVEs. Needs kas/feature/cve-check.yml stacked onto the build; skips with a "
        "note when the build carries no cve-check results. Neither a failed build nor --dry-run "
        "produces a report.",
    ),
]

SbomOption = Annotated[
    bool,
    typer.Option(
        "--sbom",
        help="On build success, filter the per-image SPDX document into a publishable inventory "
        "under deploy/avocado-sbom. Needs kas/feature/sbom.yml stacked onto the build and a "
        "meta-avocado checkout carrying the publication filter; refuses up front when the filter "
        "is absent, because the unfiltered document carries vulnerability data.",
    ),
]
