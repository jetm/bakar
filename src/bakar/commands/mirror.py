"""bakar mirror subcommand - host-side premirror tarball seeder.

Turns an upstream git URL into a BitBake-compatible mirror tarball entirely
host-side - no kas-container round-trip and no manifest/kas-YAML argument.

The pipeline is:

1. ``git clone --bare --mirror <url> <tempdir>/<project>``
2. read the last committer date via ``git log --all -1 --format=%cD``
3. ``tar -czf <dest> --owner oe:0 --group oe:0 --mtime <date> .`` from the clone
4. remove the temporary clone directory

The committer-date ``--mtime`` makes the tarball byte-stable across re-runs of
the same revision, and ``--owner oe:0 --group oe:0`` matches the ownership
BitBake expects in a ``git2_*`` premirror tarball.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import typer

import bakar.commands._app as _state
from bakar.commands._app import app, console
from bakar.commands._helpers import _find_workspace_from_cwd


def mirror_tarball_name(url: str) -> str:
    """Return BitBake's mirror-tarball filename for *url*.

    Produces ``git2_<netloc><path>.tar.gz`` where every ``/`` and ``:`` in the
    URL's network location and path is normalized to ``.``. For example
    ``https://github.com/openembedded/meta-openembedded.git`` becomes
    ``git2_github.com.openembedded.meta-openembedded.git.tar.gz``.

    The scheme (``https://``) is dropped; only ``netloc + path`` participate,
    matching BitBake's ``git2_`` mirror-tarball naming.
    """
    parsed = urlparse(url)
    base = parsed.netloc + parsed.path
    normalized = base.replace("/", ".").replace(":", ".")
    return f"git2_{normalized}.tar.gz"


def resolve_output_dir(output_dir: Path | None, dl_dir: str | None) -> Path:
    """Resolve the directory the mirror tarball is written to.

    Precedence, highest to lowest:

    1. ``output_dir`` - the ``--output-dir`` flag, when supplied.
    2. ``dl_dir`` - the configured ``DL_DIR``, but only when it is a non-empty
       string. ``cfg.dl_dir`` is frequently ``None`` (an unset user-config
       override), so ``None`` or ``""`` falls through.
    3. The current directory.
    """
    if output_dir is not None:
        return output_dir
    if dl_dir:
        return Path(dl_dir)
    return Path.cwd()


def _project_name(url: str) -> str:
    """Return the bare-clone directory name derived from *url*.

    Uses the last path segment with any ``.git`` suffix preserved, falling
    back to ``mirror`` when the URL has no usable path component.
    """
    name = Path(urlparse(url).path).name
    return name or "mirror"


@app.command()
def mirror(
    git_url: Annotated[
        str,
        typer.Argument(help="Upstream git URL to seed a premirror tarball from."),
    ],
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            "-o",
            help="Directory to write the tarball to. Defaults to the configured DL_DIR, else the current directory.",
        ),
    ] = None,
) -> None:
    """Seed a BitBake premirror ``git2_*.tar.gz`` tarball from a git URL.

    Clones the repository bare-and-mirrored into a temporary directory, reads
    the last committer date, and packs a byte-stable tarball owned by ``oe:0``.
    Runs entirely on the host; no kas-container is involved.
    """
    for tool in ("git", "tar"):
        if shutil.which(tool) is None:
            console.print(f"[red]{tool} not found on PATH[/]; install {tool} to use bakar mirror.")
            raise typer.Exit(code=1)

    in_workspace = _find_workspace_from_cwd() is not None
    dl_dir = _state._USER_CONFIG.dl_dir if (_state._USER_CONFIG is not None and in_workspace) else None
    dest_dir = resolve_output_dir(output_dir, dl_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / mirror_tarball_name(git_url)

    tmp = Path(tempfile.mkdtemp(prefix="bakar-mirror-"))
    try:
        clone_dir = tmp / _project_name(git_url)
        clone = subprocess.run(
            ["git", "clone", "--bare", "--mirror", git_url, str(clone_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        if clone.returncode != 0:
            console.print(f"[red]git clone failed[/]\n{clone.stderr.strip()}")
            raise typer.Exit(code=clone.returncode)

        committed = subprocess.run(
            ["git", "-C", str(clone_dir), "log", "--all", "-1", "--format=%cD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if committed.returncode != 0:
            console.print(f"[red]reading committer date failed[/]\n{committed.stderr.strip()}")
            raise typer.Exit(code=committed.returncode)
        mtime = committed.stdout.strip()

        tar = subprocess.run(
            [
                "tar",
                "-czf",
                str(dest),
                "--owner",
                "oe:0",
                "--group",
                "oe:0",
                "--mtime",
                mtime,
                ".",
            ],
            cwd=str(clone_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        if tar.returncode != 0:
            console.print(f"[red]tar failed[/]\n{tar.stderr.strip()}")
            raise typer.Exit(code=tar.returncode)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    console.print(f"[green]wrote[/] {dest}")
