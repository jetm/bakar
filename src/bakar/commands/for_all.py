"""bakar for-all subcommand - run a shell command in every source repo."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import (
    _dispatch_bsp,
    _dispatch_from_yaml,
    _resolve_workspace,
)
from bakar.config import resolve
from bakar.layers import discover_source_repos


def _git_head(path: Path) -> str:
    """Return the full HEAD commit hash, or an empty string on failure.

    Mirrors the git-failure-tolerant style in ``layers.py``: a repo with no
    commits or a broken ``git`` invocation yields an empty ``BAKAR_REPO_COMMIT``
    rather than aborting the whole ``for-all`` run.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


@app.command("for-all")
def for_all(
    command: Annotated[
        str,
        typer.Argument(help="Shell command to run once in each discovered source repo."),
    ],
    kas_yaml: Annotated[
        Path | None,
        typer.Argument(
            exists=False,
            help="Optional kas YAML; when supplied, BSP family is inferred from it instead of --manifest.",
        ),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename used to dispatch BSP family"),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace root override"),
    ] = None,
) -> None:
    """Run a shell command in every discovered source repo (parity with `kas for-all-repos`).

    Visits every repo even when one invocation fails; exits non-zero if any
    invocation exited non-zero, zero only when all succeeded. Each invocation
    sees ``BAKAR_REPO_NAME``, ``BAKAR_REPO_PATH``, and ``BAKAR_REPO_COMMIT``
    in its environment.
    """
    if kas_yaml is not None and manifest is not None:
        console.print("[red]choose either a positional kas YAML or --manifest, not both[/]")
        raise typer.Exit(code=2)

    if kas_yaml is not None:
        family, _bsp = _dispatch_from_yaml(kas_yaml)
    else:
        family, _bsp = _dispatch_bsp(manifest)
    ws = _resolve_workspace(workspace, kas_yaml=kas_yaml, family=family)
    cfg = resolve(
        workspace=ws, bsp_family=family, manifest=manifest, kas_yaml=kas_yaml, user_config=_state._USER_CONFIG
    )

    repos = discover_source_repos(cfg)
    if not repos:
        console.print("[red]no source repos found[/]; run `bakar build` or `bakar sync` first")
        raise typer.Exit(code=1)

    failed = False
    for name, path in repos:
        console.print(f"[bold cyan]=== {name} ({path}) ===[/]")
        env = {
            **os.environ,
            "BAKAR_REPO_NAME": name,
            "BAKAR_REPO_PATH": str(path),
            "BAKAR_REPO_COMMIT": _git_head(path),
        }
        # shell=True is intentional: the user owns the command (parity with
        # `kas for-all-repos`), so pipes, globs, and `&&` work as in a shell.
        try:
            result = subprocess.run(command, shell=True, cwd=path, env=env)  # noqa: S602
        except OSError as exc:
            # A repo dir removed between discovery and exec (or a missing shell)
            # counts as that repo failing - keep visiting the rest, per the
            # documented "visits every repo even when one invocation fails".
            console.print(f"[red]failed to run in {name}: {exc}[/]")
            failed = True
            continue
        if result.returncode != 0:
            failed = True

    raise typer.Exit(code=1 if failed else 0)
