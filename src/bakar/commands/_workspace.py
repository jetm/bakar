"""Workspace resolution and BSP dispatch for bakar subcommands.

Split out of ``_helpers.py`` (which re-exports everything here so existing
``from bakar.commands._helpers import ...`` call sites keep working). Every
symbol used from outside this module is re-exported at ``_helpers`` -
import from there unless you are inside ``commands/`` and want the direct
path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

import typer

from bakar.bsp_detect import detect_bsp_from_yaml, detect_kas_workspace, is_bbsetup_workspace
from bakar.bsp_model import BspModel, detect_bsp_family, get_model

# ---------------------------------------------------------------------------
# Workspace detection
# ---------------------------------------------------------------------------


_WORKSPACE_HELP = "Workspace root; auto-detected if omitted"

# The invoking cwd captured before ``_enter_workspace`` chdirs into a ``-w``
# workspace. ``_bsp_from_cwd`` reads it so family auto-detection reflects where
# the user actually stood (e.g. ``<ws>/ti``) rather than the post-chdir workspace
# root. Reset on every command invocation (the callback always fires), so it never
# leaks a stale cwd into a later ``-w``-less command.
_INVOCATION: dict[str, Path] = {}


def invoking_cwd() -> Path:
    """The cwd captured before ``_enter_workspace`` chdir'd into a ``-w`` workspace.

    Falls back to the live cwd when no ``-w`` was given (the callback pops the
    key on every invocation), so callers get where the user actually stood.
    """
    return _INVOCATION.get("cwd", Path.cwd())


def logical_path(physical: Path) -> Path:
    """Map a physical path back to the logical path the shell is standing in.

    ``os.getcwd()`` and :meth:`Path.resolve` both flatten symlinks. That is
    right for anything used locally and wrong for anything sent to another
    machine, because a resolved path is only guaranteed to exist on the node
    whose filesystem layout produced it.

    The cluster this matters for mounts one shared workspace at the same
    home-relative path on every node, while on the node that owns the storage
    that path is a symlink onto the storage volume. Resolving therefore yields a
    path that exists on exactly one machine, and ``--on`` mirrors the tree to the
    remote at the SAME absolute path before ``cd``-ing to it - so a resolved path
    fails there with ``mkdir ... No such file or directory``.

    ``PWD`` is the shell's own record of the logical route taken, so it is the
    only available source for the unresolved spelling. It is shell-maintained
    rather than kernel-maintained, so it is trusted only when it is absolute AND
    still resolves to a prefix of ``physical``; a relative, stale or unrelated
    value returns ``physical`` unchanged. Inventing a logical path that exists
    nowhere would be worse than a physical one that at least exists locally.
    """
    pwd = os.environ.get("PWD")
    if not pwd:
        return physical
    logical = Path(pwd)
    if not logical.is_absolute():
        return physical
    try:
        anchor = Path(os.path.realpath(logical))
    except OSError:
        return physical
    if not anchor.is_dir():
        return physical
    if physical == anchor:
        return logical
    try:
        relative = physical.relative_to(anchor)
    except ValueError:
        return physical
    return logical / relative


def _enter_workspace(workspace: Path | None) -> Path | None:
    """Resolve, validate, and chdir into an explicit ``-w``/``--workspace`` path.

    Returns ``None`` unchanged (no chdir) so commands without ``-w`` keep their
    CWD-based behavior. Otherwise resolves ``workspace`` to an absolute path,
    ``chdir``s into it, and returns it so a relative positional argument
    resolves against the workspace instead of the original CWD. A missing path
    or a non-directory raises :class:`typer.BadParameter`, which Typer renders
    as exit 2 naming the option.
    """
    _INVOCATION.pop("cwd", None)
    if workspace is None:
        return None
    resolved = workspace.expanduser().resolve()
    if not resolved.is_dir():
        raise typer.BadParameter(
            f"workspace does not exist or is not a directory: {resolved}",
            param_hint="--workspace/-w",
        )
    _INVOCATION["cwd"] = Path.cwd()
    os.chdir(resolved)
    return resolved


def _workspace_callback(value: Path | None) -> Path | None:
    """Typer parameter callback: chdir into ``value`` before the command body runs."""
    return _enter_workspace(value)


WorkspaceOption = Annotated[
    Path | None,
    typer.Option("--workspace", "-w", callback=_workspace_callback, help=_WORKSPACE_HELP, is_eager=True),
]


def _bbsetup_workspace(workspace: Path | None) -> Path | None:
    """Return the setup dir for an initialized bitbake-setup workspace, else None.

    With an explicit ``-w`` the path is checked as-is. Without it, the cwd and
    its parents are walked (mirroring ``_workspace_from_cwd``) so the command
    works from a subdirectory of the workspace.
    """

    if workspace is not None:
        return workspace.resolve() if is_bbsetup_workspace(workspace) else None
    cur = Path.cwd().resolve()
    for cand in (cur, *cur.parents):
        if is_bbsetup_workspace(cand):
            return cand
    return None


def _uninitialized_bbsetup_dir(workspace: Path | None) -> Path | None:
    """Return a dir carrying the bitbake-setup signature but not yet initialized.

    A directory with ``config/config-upstream.json`` looks like a bitbake-setup
    workspace; if it lacks ``build/init-build-env`` it has not been initialized
    (``bitbake-setup init`` writes that file). Returns the first such directory
    found (the given workspace, or walking up from cwd), or None when no
    bitbake-setup signature is present or the workspace is fully initialized.
    """
    if workspace is not None:
        cands: tuple[Path, ...] = (workspace.resolve(),)
    else:
        cur = Path.cwd().resolve()
        cands = (cur, *cur.parents)
    for cand in cands:
        if (cand / "config" / "config-upstream.json").exists():
            return None if is_bbsetup_workspace(cand) else cand
    return None


def _find_workspace_from_cwd() -> Path | None:
    """Walk up from CWD to find the BSP workspace root, or None if none found.

    Checks in order:
    1. A .bakar.toml marker file in the candidate directory.
    2. An nxp/ or ti/ subdirectory in the candidate directory.
    3. A bitbake-setup workspace (config/config-upstream.json + build/init-build-env).

    Non-raising counterpart of :func:`_workspace_from_cwd`, for callers that
    treat "not in a workspace" as a skip rather than an error.
    """
    cur = Path.cwd().resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".bakar.toml").is_file():
            return candidate
        if (candidate / "nxp").is_dir() or (candidate / "ti").is_dir():
            return candidate
        if is_bbsetup_workspace(candidate):
            return candidate
    return None


def _workspace_from_cwd() -> Path:
    """Walk up from CWD to find the BSP workspace root, or exit with a message."""
    found = _find_workspace_from_cwd()
    if found is not None:
        return found

    from bakar.commands import console

    console.print(
        "[red]Not inside a BSP workspace[/] (no .bakar.toml or nxp/ / ti/ found). "
        "cd to the workspace root, pass --workspace, or - for generic kas YAMLs - run "
        "`bakar build <kas.yml>` from anywhere."
    )
    raise typer.Exit(code=2)


def _resolve_workspace(
    workspace: Path | None,
    *,
    kas_yaml: Path | None = None,
    family: Literal["nxp", "ti", "generic", "qcom"] | None = None,
) -> Path:
    """Resolve the workspace path with a BYO+generic carve-out.

    Generic mode (``bakar build my.yml`` where ``my.yml`` does not
    target an NXP/TI SoM) does not own a workspace subtree - the
    overlay symlink and per-run state land next to the user's YAML.
    Skip the cwd walk in that case so generic builds work from any
    directory.
    """
    if workspace is not None:
        return workspace
    if family == "generic" and kas_yaml is not None:
        # For meta-avocado YAMLs this returns the parent of the
        # meta-avocado/ dir (e.g. sources/). For all other generic
        # YAMLs it returns yaml.parent - same as the old behaviour.
        return detect_kas_workspace(kas_yaml)
    return _workspace_from_cwd()


# Ordered manifest-family namespaces bakar manages under the workspace root.
# ``_family_from_workspace_contents`` iterates this list, so a future
# repo-manifest family is one entry here rather than a new code path.
_MANIFEST_FAMILIES: tuple[Literal["nxp", "ti", "qcom"], ...] = ("nxp", "ti", "qcom")


def _bsp_from_cwd(workspace: Path) -> Literal["nxp", "ti", "qcom"] | None:
    """Detect BSP family from the current working directory.

    Returns ``"nxp"``, ``"ti"``, or ``"qcom"`` if cwd is inside
    ``workspace/nxp/``, ``workspace/ti/``, or ``workspace/qcom/``; otherwise
    ``None``. Under an explicit ``-w`` the ``_enter_workspace`` callback has
    already chdir'd into the workspace root, so the pre-chdir invoking cwd
    (captured in ``_INVOCATION``) is used instead of the live cwd; without
    ``-w`` the live cwd is used exactly as before.
    """
    cwd = _INVOCATION.get("cwd", Path.cwd()).resolve()
    try:
        rel = cwd.relative_to(workspace.resolve())
    except ValueError:
        return None
    parts = rel.parts
    if not parts:
        return None
    if parts[0] == "nxp":
        return "nxp"
    if parts[0] == "ti":
        return "ti"
    if parts[0] == "qcom":
        return "qcom"
    return None


def _family_from_workspace_contents(workspace: Path) -> Literal["nxp", "ti", "qcom"] | None:
    """Detect the manifest family from the workspace tree, cwd-independently.

    Fallback for ``_bsp_from_cwd`` when the invoking cwd carries no family
    signal (e.g. ``bakar monitor -w <ws>`` run from an unrelated directory).
    For each family in :data:`_MANIFEST_FAMILIES`, a ``<workspace>/<family>/``
    subtree carrying a ``.repo`` dir (``repo sync`` output) or a ``build-*``
    build directory (qcom's ``build-<distro>``) identifies that family. nxp/ti
    use a plain ``build`` dir, so the ``build-*`` glob is a qcom-only signal in
    practice; applying it to every family keeps the probe a single loop.
    Returns the first match, or ``None`` when the workspace holds no family.
    """
    for family in _MANIFEST_FAMILIES:
        subdir = workspace / family
        if (subdir / ".repo").is_dir() or any(subdir.glob("build-*")):
            return family
    return None


# ---------------------------------------------------------------------------
# BSP dispatch
# ---------------------------------------------------------------------------


def _dispatch_bsp(manifest_arg: str | None) -> tuple[Literal["nxp", "ti"], BspModel]:
    """Detect the BSP family from the manifest filename and return ``(family, model)``.

    Inspects ``--manifest`` first, then ``BAKAR_MANIFEST``, then falls
    back to the NXP default. Refuses unrecognized shapes with a
    typer.Exit(2) and a hint pointing at the versioning references.
    """
    from bakar.commands import console
    from bakar.config import DEFAULT_NXP_MANIFEST

    pre = manifest_arg or os.environ.get("BAKAR_MANIFEST") or DEFAULT_NXP_MANIFEST
    family = detect_bsp_family(Path(pre), config_file=None)
    if family == "unknown":
        console.print(
            "[red]Unrecognized manifest shape:[/red] "
            f"{pre!r} matches neither NXP (imx-A.B.C-X.Y.Z.xml) nor TI "
            "(processor-sdk-...-config_var<N>.txt / arago-*.txt). "
            "Check the manifest filename format.",
            markup=True,
        )
        raise typer.Exit(code=2)
    return family, get_model(family)


def _dispatch_from_yaml(yaml_path: Path) -> tuple[Literal["nxp", "ti", "generic"], BspModel | None]:
    """Detect the BSP family from a kas YAML and return ``(family, model)``.

    Used by the BYO ``bakar build my.yml`` path. Inspects the YAML's
    ``machine:`` and ``repos:`` blocks via
    :func:`bakar.bsp_detect.detect_bsp_from_yaml`. Returns the
    matching :class:`BspModel` for NXP/TI and ``None`` for generic
    builds (no BspModel applies; the caller layers
    ``bakar-tuning-generic.yml`` and skips vendor-specific pipeline
    steps). Refuses ``"unknown"`` shapes (empty / unparseable YAMLs)
    with a typer.Exit(2).
    """
    from bakar.commands import console

    if not yaml_path.is_file():
        console.print(f"[red]kas YAML not found:[/red] {yaml_path}")
        raise typer.Exit(code=2)
    family = detect_bsp_from_yaml(yaml_path)
    if family == "unknown":
        console.print(
            f"[red]Could not parse {yaml_path} as a kas build.[/red] "
            "The YAML must declare at least a machine: value, a repos: block, "
            "or a header.includes list. See kas's documentation for the schema.",
            markup=True,
        )
        raise typer.Exit(code=2)
    if family == "generic":
        return ("generic", None)
    return (family, get_model(family))


def split_kas_yaml_arg(raw: str | Path | None) -> tuple[Path | None, list[Path]]:
    """Split a colon-joined kas YAML arg into (head, extras), validating each segment.

    Mirrors kas config.py:53-54: splits on ':', resolves each segment to an
    absolute path, checks it exists. Returns (None, []) for None input.
    Exits with code 2 naming any missing segment.
    """
    if raw is None:
        return None, []
    from bakar.commands._app import console

    parts = str(raw).split(":")
    resolved: list[Path] = []
    for part in parts:
        p = Path(part).resolve()
        if not p.is_file():
            console.print(f"[red]kas YAML not found:[/red] {p}")
            raise typer.Exit(code=2)
        resolved.append(p)
    return resolved[0], resolved[1:]


def _normalize_dispatch(
    kas_yaml: Path | None,
    manifest: str | None,
) -> tuple[str, BspModel | None, Path | None, str | None]:
    """Normalize workspace dispatch args and return ``(family, bsp, kas_yaml, manifest)``.

    Call this instead of ``_dispatch_bsp``/``_dispatch_from_yaml`` directly.
    Handles three cases:
    - Positional ``kas_yaml`` provided: dispatches via :func:`_dispatch_from_yaml`.
    - ``-f <path>.yml`` provided: promotes to ``kas_yaml`` and clears ``manifest``,
      then dispatches via :func:`_dispatch_from_yaml`.  This lets users write
      ``bakar inspect busybox -f meta-avocado/kas/machine/qemux86-64.yml`` and
      have the YAML path flow correctly through to ``config.resolve()``.
    - ``-f <manifest.xml>`` or default: dispatches via :func:`_dispatch_bsp`.

    Returns the *normalized* ``kas_yaml`` and ``manifest`` values alongside
    ``family`` and ``bsp`` so the caller passes correct values to
    ``_resolve_workspace()`` and ``config.resolve()``.
    """
    from bakar.commands._app import console

    # Promote -f <yaml> to the positional kas_yaml so the path flows downstream.
    if manifest is not None and manifest.endswith((".yml", ".yaml")):
        if kas_yaml is not None:
            console.print("[red]choose either a positional kas YAML or --manifest, not both[/]")
            raise typer.Exit(code=2)
        kas_yaml = Path(manifest)
        manifest = None

    # Standard mutual-exclusion guard.
    if kas_yaml is not None and manifest is not None:
        console.print("[red]choose either a positional kas YAML or --manifest, not both[/]")
        raise typer.Exit(code=2)

    if kas_yaml is not None:
        family, bsp = _dispatch_from_yaml(kas_yaml)
    else:
        family, bsp = _dispatch_bsp(manifest)

    return family, bsp, kas_yaml, manifest
