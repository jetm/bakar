"""bakar init subcommand - interactive workspace creation wizard."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Literal

import questionary
import typer

from bakar.bsp_model import get_model
from bakar.commands._app import app, console
from bakar.commands._prompts import _ask
from bakar.workspace_config import write_workspace_config

_FAMILIES = ["nxp", "ti", "bbsetup", "generic"]


def _scaffold_workspace(
    path: Path,
    family: Literal["nxp", "ti", "bbsetup", "generic"],
    settings: dict[str, str],
) -> None:
    """Scaffold a workspace directory layout and write ``.bakar.toml``.

    Pure function with no questionary interaction, so the four scaffold paths
    are unit-testable without mocking TTY prompts.

    - ``nxp`` / ``ti``: create the ``<path>/<family>/`` subdirectory, then
      write ``.bakar.toml`` with a ``[defaults.<family>]`` section.
    - ``bbsetup``: write a comment-only ``.bakar.toml`` marker (no
      ``[defaults]`` section) and create no subdirectories. ``bitbake-setup
      init`` drives its own interactive setup.
    - ``generic``: write ``.bakar.toml`` with a ``[defaults.generic]`` section
      and create no subdirectories.

    Raises ``FileExistsError`` when ``<path>/.bakar.toml`` already exists so the
    wizard can catch it and abort without clobbering an existing workspace.
    """
    marker = path / ".bakar.toml"
    if marker.exists():
        raise FileExistsError(str(marker))

    path.mkdir(parents=True, exist_ok=True)

    if family in ("nxp", "ti"):
        (path / family).mkdir(parents=True, exist_ok=True)
        write_workspace_config(path, family, settings)
    elif family == "bbsetup":
        marker.write_text("# bakar workspace root.\n")
    else:
        write_workspace_config(path, "generic", settings)


def _family_settings(
    family: str,
    manifest: str | None,
    machine: str | None,
    distro: str | None,
    image: str | None,
    kas_yaml: str | None,
) -> dict[str, str]:
    """Validate ``family`` and build its workspace settings from flags and BSP defaults."""
    if family not in _FAMILIES:
        console.print(f"[red]unknown family:[/] {family!r} - must be one of {_FAMILIES}")
        raise typer.Exit(1)

    settings: dict[str, str] = {}
    if family in ("nxp", "ti"):
        model = get_model(family)
        settings["manifest"] = manifest or model.default_manifest or ""
        settings["machine"] = machine or model.default_machine or ""
        settings["distro"] = distro or model.default_distro or ""
        settings["image"] = image or model.default_image or ""
    elif family == "generic":
        settings["kas_yaml"] = kas_yaml or "kas-generic.yml"
        settings["machine"] = machine or "qemux86-64"
    # bbsetup: no settings needed.
    return settings


def _init_non_interactive(
    family: str,
    workspace: Path | None,
    manifest: str | None,
    machine: str | None,
    distro: str | None,
    image: str | None,
    kas_yaml: str | None,
) -> None:
    """Run non-interactive init: validate family, build settings, scaffold, report."""
    settings = _family_settings(family, manifest, machine, distro, image, kas_yaml)
    path = workspace or Path(".")

    try:
        _scaffold_workspace(path, family, settings)
    except FileExistsError as exc:
        console.print(f"[red]workspace already initialized:[/] {exc} already exists")
        raise typer.Exit(1) from exc

    console.print(f"[green]workspace scaffolded[/] at {path}")
    if family in ("nxp", "ti", "generic"):
        console.print(f"Next: [bold]bakar sync --workspace {path}[/]")


def _init_interactive(no_sync: bool, workspace: Path | None) -> None:
    """Run the interactive wizard: TTY check, workspace, scaffold if new, features, optional sync.

    Ctrl-C at any prompt prints ``Aborted.`` and exits 1, leaving only what was
    already scaffolded.
    """
    if not sys.stdin.isatty():
        console.print(
            "[red]bakar init requires an interactive terminal[/] - stdin is not a TTY. "
            "Use --family or --feature to enable non-interactive mode."
        )
        raise typer.Exit(1)

    try:
        _init_interactive_steps(no_sync, workspace)
    except typer.Abort:
        console.print("[red]Aborted.[/]")
        raise typer.Exit(1) from None


def _prompt_family_settings(family_str: str) -> dict[str, str]:
    settings: dict[str, str] = {}
    if family_str in ("nxp", "ti"):
        model = get_model(family_str)
        settings["manifest"] = _ask(questionary.text("Manifest:", default=model.default_manifest))
        settings["machine"] = _ask(questionary.text("Machine:", default=model.default_machine))
        settings["distro"] = _ask(questionary.text("Distro:", default=model.default_distro))
        settings["image"] = _ask(questionary.text("Image:", default=model.default_image))
    elif family_str == "generic":
        settings["kas_yaml"] = _ask(questionary.text("kas YAML filename:", default="kas-generic.yml"))
        settings["machine"] = _ask(questionary.text("Machine:", default="qemux86-64"))
    # bbsetup: no family-specific prompts.
    return settings


def _init_interactive_steps(no_sync: bool, workspace: Path | None) -> None:
    from bakar.commands._init_checklist import run_feature_step

    if workspace is not None:
        path = workspace
    else:
        path = Path(_ask(questionary.path("Workspace directory:", default="."))).expanduser()

    scaffolded = False
    if (path / ".bakar.toml").exists():
        console.print(f"workspace already initialized at {path}")
    else:
        family_str: str = _ask(questionary.select("BSP family:", choices=["nxp", "ti", "bbsetup", "generic"]))
        settings = _prompt_family_settings(family_str)
        try:
            _scaffold_workspace(path, family_str, settings)
        except FileExistsError as exc:
            console.print(f"[red]workspace already initialized:[/] {exc} already exists")
            raise typer.Exit(1) from exc
        scaffolded = True
        console.print(f"[green]workspace scaffolded[/] at {path}")

    run_feature_step(path)

    if not scaffolded:
        return

    if no_sync:
        run_sync = False
    else:
        console.print("Downloading sources can take a while")
        run_sync = _ask(questionary.confirm("Run `bakar sync` now?", default=False))

    if run_sync:
        from bakar.commands.sync import sync

        sync(workspace=path)
        _install_buildtools_after_sync(path)
        return

    if family_str in ("nxp", "ti", "generic"):
        console.print(f"Next: [bold]bakar sync --workspace {path}[/]")
    else:
        console.print(
            "Next: run [bold]bitbake-setup init[/] from inside the workspace to populate config/config-upstream.json"
        )
    _hint_buildtools(path)


def _workspace_host_mode(path: Path) -> bool:
    from bakar.commands._init_requirements import proposed_config

    cfg, _ = proposed_config(path, {}, ())
    return bool(cfg.host_mode)


def _hint_buildtools(path: Path) -> None:
    """Point at ``bakar setup`` when a host-mode workspace has no buildtools yet."""
    from bakar import buildtools

    if _workspace_host_mode(path) and not buildtools.detect_buildtools().present:
        console.print(
            "To install the buildtools toolchain, run [bold]bakar setup[/] from the workspace after `bakar sync`."
        )


def _install_buildtools_after_sync(path: Path) -> None:
    """Offer the buildtools install actions once sync has fetched oe-core.

    Only the ``host-preflight`` actions are kept: the rest of the host plan was
    already reviewed and applied by the feature step.
    """
    if not _workspace_host_mode(path):
        return

    from bakar.commands import _app as _state
    from bakar.setup import plan as setup_plan
    from bakar.setup import runner
    from bakar.setup.plan import SetupPlan
    from bakar.setup.profile import HostProfile

    plan = setup_plan.build(HostProfile.detect(), workspace=path, user_config=_state._USER_CONFIG)
    kept = [a for a in plan.actions if a.check_name == "host-preflight"]
    if not kept:
        return
    console.print("Buildtools toolchain:")
    for action in kept:
        console.print(f"  - {action.describe()}", markup=False)
    if not _ask(questionary.confirm("Install the buildtools toolchain?", default=False)):
        return
    runner.apply_plan(SetupPlan(actions=kept), confirmed=True)


@app.command("init")
def init(
    family: Annotated[
        str | None,
        typer.Option("--family", "-f", help="BSP family (nxp/ti/bbsetup/generic); enables non-interactive mode"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace directory (default: current directory)"),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", help="Manifest filename (nxp/ti only)"),
    ] = None,
    machine: Annotated[
        str | None,
        typer.Option("--machine", help="Machine name"),
    ] = None,
    distro: Annotated[
        str | None,
        typer.Option("--distro", help="Distro (nxp/ti only)"),
    ] = None,
    image: Annotated[
        str | None,
        typer.Option("--image", help="Image (nxp/ti only)"),
    ] = None,
    kas_yaml: Annotated[
        str | None,
        typer.Option("--kas-yaml", help="KAS YAML filename (generic only)"),
    ] = None,
    no_sync: Annotated[
        bool,
        typer.Option("--no-sync", help="Skip sync after scaffolding (interactive mode only)"),
    ] = False,
    feature: Annotated[
        list[str] | None,
        typer.Option("--feature", help="Feature id to enable (repeatable); enables non-interactive mode"),
    ] = None,
    disable: Annotated[
        list[str] | None,
        typer.Option("--disable", help="Feature id to turn off (repeatable); enables non-interactive mode"),
    ] = None,
    set_values: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="KEY=VALUE for a selected feature: a settings key or <feature-id>.<input> (repeatable)",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Apply a plan that needs sudo without prompting (needs passwordless sudo)"),
    ] = False,
    explain: Annotated[
        str | None,
        typer.Option("--explain", help="Explain one feature id: requirements, fixes and guide (read-only)"),
    ] = None,
) -> None:
    """Scaffold a new bakar workspace and choose its features.

    Without ``--family``, ``--feature`` or ``--disable``: interactive wizard
    using questionary prompts. Requires a TTY on stdin.

    With ``--family`` alone: non-interactive scaffold - no TTY required, no
    user config written, no host change. Settings come from the provided
    flags, falling back to BSP model defaults for nxp/ti. Sync is never run.

    With ``--feature`` or ``--disable``: non-interactive feature step. The
    workspace is scaffolded first when ``--family`` is given and it is not yet
    initialized; otherwise it must already be initialized.
    """
    features = list(feature or [])
    disables = list(disable or [])
    if explain is not None:
        others = (family, manifest, machine, distro, image, kas_yaml, features, disables, set_values)
        if any(others) or no_sync or yes:
            console.print("[red]--explain takes one feature id and combines only with --workspace[/]")
            raise typer.Exit(1)
        from bakar.commands._init_explain import run_explain

        run_explain(explain, workspace or Path("."))
        return
    if not features and not disables:
        if set_values or (yes and family is None):
            console.print("[red]--set and --yes apply to the feature step[/] - pass --feature or --disable with them")
            raise typer.Exit(1)
        if family is not None:
            _init_non_interactive(family, workspace, manifest, machine, distro, image, kas_yaml)
        else:
            _init_interactive(no_sync, workspace)
        return

    from bakar.commands._init_features import run_noninteractive

    path = workspace or Path(".")
    scaffold = None
    if not (path / ".bakar.toml").exists():
        if family is None:
            console.print("[red]no workspace here[/] - pass --family to create one before selecting features")
            raise typer.Exit(1)
        settings = _family_settings(family, manifest, machine, distro, image, kas_yaml)

        def scaffold() -> None:
            _scaffold_workspace(path, family, settings)
            console.print(f"[green]workspace scaffolded[/] at {path}")

    run_noninteractive(
        workspace=path,
        features=features,
        disables=disables,
        set_items=list(set_values or []),
        yes=yes,
        scaffold=scaffold,
    )
