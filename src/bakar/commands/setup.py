"""bakar setup - once-per-machine host preparation.

Profiles the host, computes the remediations for the host-environment ``doctor``
checks, prints an auditable plan plus the verbatim privileged script, and
delegates application to :mod:`bakar.setup.runner`. ``--dry-run`` prints the
profile and the generated script and mutates nothing - no action runs, no file
is written, no sudo is invoked.
"""

from __future__ import annotations

from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.setup import plan as setup_plan
from bakar.setup import runner as setup_runner
from bakar.setup.profile import HostProfile
from bakar.setup.script import render_script


def _print_profile(profile: HostProfile) -> None:
    """Print the host profile bakar.setup detected (read-only)."""
    console.print("[bold]Host profile[/]")
    console.print(f"  cpu count        : {profile.cpu_count}")
    console.print(f"  memory available : {profile.mem_available_gb:.1f} GB")
    console.print(f"  disk free ($HOME): {profile.disk_free_gb:.1f} GB")
    console.print(f"  distro           : {profile.distro_id or 'unknown'}")
    console.print(f"  package manager  : {profile.pkg_manager or 'unknown'}")
    console.print(f"  docker installed : {profile.docker_installed}")
    console.print(f"  in docker group  : {profile.in_docker_group}")


def _print_plan(plan: setup_plan.SetupPlan) -> str:
    """Print the plan's actions and advisories; return the rendered script.

    The returned text is the verbatim privileged ``bakar-host-setup.sh`` built
    from every ``needs_root`` operation across the planned actions - rendered,
    not written, so callers can print it without touching disk.
    """
    if plan.actions:
        console.print("[bold]Planned actions[/]")
        for action in plan.actions:
            console.print(f"  - {action.describe()}")
    else:
        console.print("[green]Host already prepared[/] - no actions to apply.")

    if plan.advisories:
        console.print("[bold]Advisories[/] (reported, never applied)")
        for note in plan.advisories:
            console.print(f"  - {note}")

    privileged_ops = [op for action in plan.actions for op in action.operations() if op.needs_root]
    return render_script(privileged_ops)


@app.command()
def setup(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Print the host profile and generated script; apply nothing"),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirm gate (requires passwordless sudo for privileged steps)"),
    ] = False,
    git_email: Annotated[
        str | None,
        typer.Option("--git-email", help="Global git user.email to set (omit to skip git identity)"),
    ] = None,
    git_name: Annotated[
        str | None,
        typer.Option("--git-name", help="Global git user.name to set (omit to skip git identity)"),
    ] = None,
) -> None:
    """Prepare this machine for ``bakar build`` (run once per host).

    Profiles the host, maps the failing host-environment ``doctor`` checks to
    remediation actions, and applies them: unprivileged actions run inline,
    privileged actions go into a single ``bakar-host-setup.sh`` run under one
    confirmed ``sudo``. ``--dry-run`` prints the profile and the verbatim script
    and changes nothing.
    """
    profile = HostProfile.detect()
    plan = setup_plan.build(profile, git_email=git_email, git_name=git_name, user_config=_state._USER_CONFIG)

    _print_profile(profile)
    script = _print_plan(plan)

    console.print("[bold]Privileged script[/] (runs under one sudo)")
    console.print(script)

    if dry_run:
        console.print("[yellow]Dry run[/] - nothing was applied.")
        return

    setup_runner.apply_plan(plan, assume_yes=yes, console=console)
