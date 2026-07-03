"""bakar changelog subcommand - diff two pinned workspace states.

Compares two pinned workspace states and emits Added/Removed/Modified
sections. Each positional argument is auto-detected as:

- A ``.xml`` file path -> manifest XML (NXP/TI pin map via
  :func:`bakar.workspace.parse_manifest_pins`).
- A JSON file containing a ``repos`` top-level key -> kas lockfile (BYO/bbsetup
  pin map via :func:`bakar.pin_state.parse_kas_lockfile`).
- Anything else -> git ref (the ref's tree is read for a manifest XML or kas
  lockfile via ``git show <ref>:<path>``).

Modified layers list a commit count and a ``git log --oneline <from>..<to>``
excerpt from the checked-out source directory. Unchanged layers are omitted.

Pin-key reconciliation: manifest keys carry a leading path component
(``"sources/meta-imx"``); the bare name used for display and source-dir
lookup is the last component. This mirrors the reconciliation in
:mod:`bakar.commands.drift`.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated

import typer

import bakar.commands._app as _state
from bakar import pin_state
from bakar.commands._app import app, console
from bakar.commands._helpers import WorkspaceOption, _dispatch_bsp, _resolve_workspace
from bakar.config import BSPSpec, resolve
from bakar.workspace import parse_manifest_pins

# ---------------------------------------------------------------------------
# Pin-input detection
# ---------------------------------------------------------------------------


def _is_manifest_xml(path: Path) -> bool:
    """Return True when *path* has a ``.xml`` extension."""
    return path.suffix.lower() == ".xml"


def _is_kas_lockfile(path: Path) -> bool:
    """Return True when *path* is a JSON file containing a top-level ``repos`` key."""
    if not path.is_file():
        return False
    try:
        raw = json.loads(path.read_text())
        return isinstance(raw, dict) and "repos" in raw
    except OSError, json.JSONDecodeError, ValueError:
        return False


def _strip_path_prefix(key: str) -> str:
    """Strip the leading path component from a manifest pin key.

    Manifest pins use keys like ``"sources/meta-imx"``; the last component is
    the bare name matching the checkout directory.
    """
    return key.split("/", 1)[-1]


def _normalize_pins(raw: dict[str, str], *, is_manifest: bool) -> dict[str, str]:
    """Return ``{bare_name: sha}`` from a raw pins dict.

    Manifest pins carry a path prefix; lockfile pins use bare names already.
    """
    if is_manifest:
        return {_strip_path_prefix(k): v for k, v in raw.items()}
    return dict(raw)


def _read_pins_from_file(path: Path) -> tuple[dict[str, str], bool]:
    """Return ``(pins, is_manifest)`` for a file path.

    Raises :exc:`typer.Exit` (code 2) on any parse failure.
    """
    if _is_manifest_xml(path):
        raw_list = parse_manifest_pins(path)
        if not raw_list and not path.is_file():
            console.print(f"[red]File not found:[/] {path}")
            raise typer.Exit(code=2)
        return dict(raw_list), True

    if _is_kas_lockfile(path):
        try:
            return pin_state.parse_kas_lockfile(path), False
        except ValueError as exc:
            console.print(f"[red]Cannot read kas lockfile:[/] {exc}")
            raise typer.Exit(code=2) from None

    console.print(f"[red]Cannot determine pin format for:[/] {path}")
    console.print("Expected a .xml manifest or a JSON file with a top-level 'repos' key.")
    raise typer.Exit(code=2)


def _read_pins_from_git_ref(ref: str, bsp_root: Path) -> tuple[dict[str, str], bool]:
    """Return ``(pins, is_manifest)`` by reading a manifest or lockfile at a git ref.

    Searches common paths under *bsp_root* for a manifest XML or kas lockfile
    tracked in git. Falls back to an empty dict (no pins) rather than raising.

    Strategy:
    1. Try ``git show <ref>:.repo/manifests/<last-manifest-xml>`` for NXP/TI.
    2. Try ``git show <ref>:kas.lock`` for BYO/bbsetup.
    3. Try ``git ls-tree <ref> --name-only`` to find a ``.xml`` or ``kas.lock``.
    """
    # For NXP/TI, .repo/manifests is its own git repo (not tracked by bsp_root).
    # Run git commands inside .repo/manifests to list and read manifest XMLs.
    manifests_dir = bsp_root / ".repo" / "manifests"
    candidates: list[tuple[Path, str, bool]] = []  # (git_cwd, git_path, is_manifest)

    if manifests_dir.is_dir():
        try:
            ls = subprocess.run(
                ["git", "-C", str(manifests_dir), "ls-tree", "--name-only", ref, "--"],
                capture_output=True,
                text=True,
                check=False,
            )
            if ls.returncode == 0:
                candidates.extend(
                    (manifests_dir, fname, True) for fname in ls.stdout.splitlines() if fname.endswith(".xml")
                )
        except OSError:
            pass

    # BYO/bbsetup: kas.lock at the bsp_root
    candidates.append((bsp_root, "kas.lock", False))

    for git_cwd, git_path, is_manifest in candidates:
        try:
            out = subprocess.run(
                ["git", "-C", str(git_cwd), "show", f"{ref}:{git_path}"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            continue
        if out.returncode != 0:
            continue

        content = out.stdout
        if is_manifest:
            # Write to a temp file and parse via parse_manifest_pins
            with tempfile.NamedTemporaryFile(suffix=".xml", mode="w", delete=False) as tmp:
                tmp.write(content)
                tmp_path = Path(tmp.name)
            try:
                pins = dict(parse_manifest_pins(tmp_path))
            finally:
                tmp_path.unlink(missing_ok=True)
            if pins:
                return pins, True
        else:
            # kas lockfile JSON
            try:
                raw = json.loads(content)
                if isinstance(raw, dict) and "repos" in raw:
                    pins_out: dict[str, str] = {}
                    repos = raw["repos"]
                    if isinstance(repos, dict):
                        for name, entry in repos.items():
                            if isinstance(entry, dict):
                                commit = entry.get("commit")
                                if isinstance(commit, str) and commit:
                                    pins_out[name] = commit
                    return pins_out, False
            except json.JSONDecodeError, ValueError:
                continue

    console.print(f"[red]Cannot read pins from git ref:[/] {ref!r}")
    console.print("No manifest XML or kas lockfile found at that ref under the workspace.")
    raise typer.Exit(code=2)


def _resolve_pins(arg: str, bsp_root: Path) -> tuple[dict[str, str], bool]:
    """Return ``(pins, is_manifest)`` for a positional ``<from>``/``<to>`` argument.

    Dispatches on:
    - ``.xml`` extension -> manifest file
    - JSON file with ``repos`` key -> kas lockfile
    - otherwise -> git ref
    """
    path = Path(arg)

    # Explicit file path checks
    if path.suffix.lower() == ".xml":
        return _read_pins_from_file(path)
    if path.is_file():
        return _read_pins_from_file(path)

    # Git ref path
    return _read_pins_from_git_ref(arg, bsp_root)


# ---------------------------------------------------------------------------
# Commit log excerpt
# ---------------------------------------------------------------------------


def _git_log_oneline(checkout: Path, from_sha: str, to_sha: str, *, max_lines: int = 10) -> list[str]:
    """Return up to *max_lines* lines of ``git log --oneline <from>..<to>``."""
    if not checkout.is_dir():
        return []
    try:
        out = subprocess.run(
            ["git", "-C", str(checkout), "log", "--oneline", f"{from_sha}..{to_sha}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if out.returncode != 0:
        return []
    lines = [line for line in out.stdout.splitlines() if line.strip()]
    return lines[:max_lines]


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@app.command("changelog")
def changelog(
    from_state: Annotated[
        str,
        typer.Argument(help="From-state: manifest XML path, kas lockfile path, or git ref."),
    ],
    to_state: Annotated[
        str,
        typer.Argument(help="To-state: manifest XML path, kas lockfile path, or git ref."),
    ],
    workspace: WorkspaceOption = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", "-f", help="Manifest filename (NXP/TI) used to dispatch BSP family."),
    ] = None,
    fmt: Annotated[
        str,
        typer.Option("--format", help="Output format: text (default) or md (markdown)."),
    ] = "text",
) -> None:
    """Generate release notes between two pinned workspace states.

    Each positional argument is auto-detected:

    - A ``.xml`` file -> manifest XML pin map (NXP/TI).
    - A JSON file with a ``repos`` key -> kas lockfile pin map (BYO/bbsetup).
    - Anything else -> a git ref (read manifest or lockfile content from that ref).

    Outputs Added (only in ``<to>``), Removed (only in ``<from>``), and
    Modified (SHA changed) sections. Unchanged layers are omitted. For
    Modified layers, lists the commit count and a ``git log --oneline`` excerpt
    from the checked-out source directory.

    Markdown output (``--format md``) starts with a heading naming the
    from/to states.
    """
    family, _bsp = _dispatch_bsp(manifest)
    ws = _resolve_workspace(workspace, family=family)
    cfg = resolve(
        workspace=ws,
        bsp_family=family,
        spec=BSPSpec(manifest=manifest),
        user_config=_state._USER_CONFIG,
    )

    bsp_root = cfg.bsp_root

    # Resolve pins for both states
    from_pins_raw, from_is_manifest = _resolve_pins(from_state, bsp_root)
    to_pins_raw, to_is_manifest = _resolve_pins(to_state, bsp_root)

    from_pins = _normalize_pins(from_pins_raw, is_manifest=from_is_manifest)
    to_pins = _normalize_pins(to_pins_raw, is_manifest=to_is_manifest)

    # Compute set operations
    from_names = set(from_pins)
    to_names = set(to_pins)

    added = sorted(to_names - from_names)
    removed = sorted(from_names - to_names)
    common = from_names & to_names
    modified = sorted(name for name in common if from_pins[name] != to_pins[name])

    # Source checkout roots to look up commit logs
    source_roots = [bsp_root / "sources", bsp_root / "layers"]

    def _find_checkout(name: str) -> Path | None:
        for root in source_roots:
            cand = root / name
            if cand.is_dir():
                return cand
        return None

    if fmt not in ("text", "md"):
        console.print(f"[red]Unknown format:[/] {fmt!r}. Valid values: text, md")
        raise typer.Exit(code=2)

    # Render
    if fmt == "md":
        _render_markdown(from_state, to_state, added, removed, modified, from_pins, to_pins, _find_checkout)
    else:
        _render_text(added, removed, modified, from_pins, to_pins, _find_checkout)

    raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_text(
    added: list[str],
    removed: list[str],
    modified: list[str],
    from_pins: dict[str, str],
    to_pins: dict[str, str],
    find_checkout: object,
) -> None:
    """Render plain-text Added/Removed/Modified sections."""
    if added:
        console.print("Added:")
        for name in added:
            sha = to_pins[name]
            console.print(f"  + {name} ({sha[:8]})")

    if removed:
        console.print("Removed:")
        for name in removed:
            sha = from_pins[name]
            console.print(f"  - {name} ({sha[:8]})")

    if modified:
        console.print("Modified:")
        for name in modified:
            old_sha = from_pins[name]
            new_sha = to_pins[name]
            checkout = find_checkout(name)  # type: ignore[operator]
            count = pin_state.commit_distance(checkout, old_sha, new_sha) if checkout else None
            count_str = f" ({count} commit{'s' if count != 1 else ''})" if count is not None else ""
            console.print(f"  ~ {name}: {old_sha[:8]}..{new_sha[:8]}{count_str}")
            if checkout:
                lines = _git_log_oneline(checkout, old_sha, new_sha)
                for line in lines:
                    console.print(f"      {line}")

    if not added and not removed and not modified:
        console.print("No changes between the two states.")


def _render_markdown(
    from_state: str,
    to_state: str,
    added: list[str],
    removed: list[str],
    modified: list[str],
    from_pins: dict[str, str],
    to_pins: dict[str, str],
    find_checkout: object,
) -> None:
    """Render markdown output with a heading naming from/to states."""
    console.print(f"## Changelog: {from_state} -> {to_state}")
    console.print("")

    if added:
        console.print("### Added")
        console.print("")
        for name in added:
            sha = to_pins[name]
            console.print(f"- **{name}** ({sha[:8]})")
        console.print("")

    if removed:
        console.print("### Removed")
        console.print("")
        for name in removed:
            sha = from_pins[name]
            console.print(f"- **{name}** ({sha[:8]})")
        console.print("")

    if modified:
        console.print("### Modified")
        console.print("")
        for name in modified:
            old_sha = from_pins[name]
            new_sha = to_pins[name]
            checkout = find_checkout(name)  # type: ignore[operator]
            count = pin_state.commit_distance(checkout, old_sha, new_sha) if checkout else None
            count_str = f" ({count} commit{'s' if count != 1 else ''})" if count is not None else ""
            console.print(f"- **{name}**: `{old_sha[:8]}..{new_sha[:8]}`{count_str}")
            if checkout:
                lines = _git_log_oneline(checkout, old_sha, new_sha)
                if lines:
                    console.print("")
                    for line in lines:
                        console.print(f"  - `{line}`")
        console.print("")

    if not added and not removed and not modified:
        console.print("_No changes between the two states._")
        console.print("")
