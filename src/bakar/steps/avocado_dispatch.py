"""Dispatch a meta-avocado build to a release workspace on another host.

The general ``--on`` path mirrors a whole workspace with rsync. A meta-avocado
build does not: the remote host already holds a workspace for the build's
release, so only the meta-avocado checkout's state travels - as a commit pushed
to ``refs/bakar/<name>/<commit12>`` of the remote clone and checked out as a
linked worktree at ``<worktree_root>/<name>``. The remote build then runs from
that worktree with ``BAKAR_REPO_POLICY=reuse``, so the shared side checkouts in
the workspace are classified and reused rather than moved.

Every check that can refuse runs before the first remote write, and nothing
here deletes anything on the remote.
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape

from bakar.release_workspace import (
    DEFAULT_BUILD_DIR_TEMPLATE,
    REPO_POLICY_ENV,
    REPO_POLICY_REUSE,
    ReleaseRoutingError,
    parse_repo_policy,
    worktree_build_dir_name,
)
from bakar.steps import layer_delivery, layer_delivery_remote
from bakar.steps import remote_dispatch as rd

if TYPE_CHECKING:
    from bakar.bsp_detect import LinkedWorktree

console = rd.console

# Answered only by a bakar that carries release-workspace dispatch: an older one
# exits non-zero on the unknown key, and it would also ignore BAKAR_REPO_POLICY.
_CAPABILITY_KEY = "dispatch.snapshot_untracked_cap_mb"
# How many untracked paths the plan names before summarising the rest.
_PLAN_UNTRACKED_SHOWN = 20


class KasArgError(Exception):
    """A kas argument segment cannot be named relative to the delivered checkout."""


def _relative_to_checkout(segment: str, checkout: Path, cwd: Path) -> str:
    """Return ``segment`` as a path relative to ``checkout``, resolving it from ``cwd``.

    The lexical form is tried first because ``checkout`` arrives as the logical
    (unresolved) path; the resolved form catches a segment that reaches the same
    file through a different symlink route.
    """
    raw = Path(segment)
    candidate = raw if raw.is_absolute() else cwd / raw
    lexical = Path(os.path.normpath(candidate))
    try:
        return lexical.relative_to(Path(os.path.normpath(checkout))).as_posix()
    except ValueError:
        pass
    try:
        return Path(os.path.realpath(candidate)).relative_to(Path(os.path.realpath(checkout))).as_posix()
    except ValueError:
        raise KasArgError(
            f"kas file {segment!r} is outside the meta-avocado checkout {checkout}, "
            "so it would not exist in the delivered worktree"
        ) from None


def rewrite_kas_arg(kas_arg: str, checkout: Path, cwd: Path) -> str:
    """Rewrite a colon-joined kas argument to paths relative to ``checkout``."""
    return ":".join(_relative_to_checkout(segment, checkout, cwd) for segment in kas_arg.split(":"))


def strip_workspace_option(args: list[str]) -> list[str]:
    """Drop ``-w``/``--workspace`` and its value: the remote cwd decides the workspace.

    Short tokens go through :func:`remote_dispatch.strip_short_option`, so the
    ``w`` of a cluster such as ``-kw /ws`` is removed with its value while a
    ``w`` inside another option's value (``-tweston``) is not an option at all.
    """
    result: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--workspace":
            skip_next = True
            continue
        if arg.startswith("--workspace="):
            continue
        kept, skip_next = rd.strip_short_option(arg, "w")
        if kept is not None:
            result.append(kept)
    return result


def build_remote_argv(local_args: list[str], kas_arg: str, rewritten_kas: str) -> list[str]:
    """Return the forwarded argv with dispatch options and ``-w`` removed and the kas token rewritten."""
    argv = strip_workspace_option(rd.strip_dispatch_options(local_args))
    try:
        index = argv.index(kas_arg)
    except ValueError:
        raise KasArgError(f"kas argument {kas_arg!r} not found in the forwarded command line") from None
    argv[index] = rewritten_kas
    return argv


def remote_env(build_dir_template: str | None) -> dict[str, str]:
    """Forwarded ``BAKAR_*``/``KAS_*`` variables for the remote build.

    ``KAS_WORK_DIR`` names a local directory, and the remote workspace is chosen
    by where the delivered worktree sits, so it is dropped.
    """
    env = {k: v for k, v in os.environ.items() if k.startswith(("BAKAR_", "KAS_")) and k != "KAS_WORK_DIR"}
    env[REPO_POLICY_ENV] = REPO_POLICY_REUSE
    # Always sent: the remote's own config must not pick another directory than
    # the one the plan names and the remote run is scoped to.
    env["BAKAR_WORKTREE_BUILD_DIR"] = build_dir_template or DEFAULT_BUILD_DIR_TEMPLATE
    return env


def _check_remote_capability(host: str) -> bool:
    """Refuse a host whose bakar predates release-workspace dispatch, whatever ``--yes`` says.

    The version check is overridable, and an older remote bakar runs plain kas:
    it ignores ``BAKAR_REPO_POLICY=reuse`` and moves the workspace's shared
    clones to their pins. Asking for a setting only this change added is a
    read-only probe that such a bakar fails.
    """
    result = rd._ssh_bash_capture(host, f"bakar settings get {shlex.quote(_CAPABILITY_KEY)}\n")
    if result.returncode == 0:
        return True
    if result.returncode == 255:
        console.print(f"[red]could not probe the bakar on {host}[/] (ssh exit 255).")
        if result.stderr.strip():
            console.print(result.stderr.strip(), markup=False)
        return False
    # Any other exit is ambiguous: an older bakar rejects the unknown key, but a
    # current one with a broken config fails the same probe, so the remote's own
    # words are shown instead of asserting which it was.
    stderr = "\n".join(result.stderr.strip().splitlines()[:5])
    console.print(
        f"[red]could not confirm that the bakar on {host} supports release-workspace dispatch[/] "
        f"(exit {result.returncode}): {escape(stderr) if stderr else '(no stderr)'}; if that bakar predates this "
        f"feature, update it (git pull + uv tool install --force). Without it {REPO_POLICY_ENV} would be ignored "
        "and the workspace's shared clones moved off their pins; --yes does not override this."
    )
    return False


def dispatch_avocado_build(  # noqa: PLR0913 - fixed dispatch signature consumed by the build command
    host: str,
    *,
    workspace: Path,
    checkout: Path,
    worktree: LinkedWorktree | None,
    kas_yaml: Path,
    kas_arg: str,
    local_args: list[str],
    release: str,
    sccache_dist: bool,
    assume_yes: bool,
    worktree_root: Path | None,
    build_dir_template: str | None,
    cap_mb: int,
    invoking_cwd: Path | None = None,
) -> int:
    """Deliver ``checkout`` to the ``release`` workspace on ``host`` and build there.

    ``checkout`` must already be the logical (unresolved) path: it is recorded
    in the remote marker as the delivery's source. ``invoking_cwd`` is where
    relative kas segments are resolved from (default: the current directory).
    Returns the remote build's exit code, 130 on Ctrl-C, or ``_DISPATCH_LOST_EXIT``
    when the log stream is lost while the build keeps running.
    """
    # Everything up to the preflight is local, so a request that cannot be
    # dispatched is refused before the host is contacted at all.
    try:
        # The remote is always sent `reuse`, but a typo here would build with
        # `kas` locally, so it is refused rather than overridden silently.
        parse_repo_policy(os.environ.get(REPO_POLICY_ENV))
    except ReleaseRoutingError as exc:
        console.print(f"[red]cannot dispatch:[/] {exc}")
        return 2
    if worktree_root is not None and not worktree_root.is_absolute():
        console.print(
            f"[red]cannot dispatch:[/] '\\[dispatch] worktree_root' must be an absolute path on the host, "
            f"got {escape(repr(str(worktree_root)))}"
        )
        return 2

    cwd = invoking_cwd if invoking_cwd is not None else Path.cwd()
    try:
        remote_argv = build_remote_argv(local_args, kas_arg, rewrite_kas_arg(kas_arg, checkout, cwd))
    except KasArgError as exc:
        console.print(f"[red]cannot dispatch:[/] {exc}")
        return 2

    try:
        name = layer_delivery.delivered_name(checkout, worktree)
        build_dir = worktree_build_dir_name(kas_yaml.stem, name, build_dir_template)
        commit, snapshot = layer_delivery.snapshot_commit(checkout, cap_mb=cap_mb)
        untracked = layer_delivery.snapshot_untracked_paths(checkout) if snapshot else []
    except ReleaseRoutingError as exc:
        console.print(f"[red]cannot dispatch:[/] {exc}")
        return 2
    except layer_delivery.DeliveryError as exc:
        console.print(f"[red]cannot deliver {checkout}:[/] {exc}")
        return 1

    if not rd.preflight_or_report(host, assume_yes=assume_yes):
        return 1
    if not _check_remote_capability(host):
        return 1
    try:
        rd.check_remote_workspace(host, str(workspace), release)
    except rd.DispatchError as exc:
        console.print(f"[red]remote workspace check failed:[/] {exc}")
        return 1

    root = worktree_root if worktree_root is not None else workspace / ".bakar" / "worktrees"
    target = root / name
    remote_repo = f"{workspace}/meta-avocado"
    plan = rd.DispatchPlan(
        writes=[
            f"worktree {target} (created, or moved to the delivered commit), with its git admin directory "
            f"{remote_repo}/.git/worktrees/{name} holding the delivery marker "
            f"{layer_delivery_remote.MARKER_FILENAME}",
            f"bookkeeping files in {root} (created if missing): .{name}.lock, .{name}.pending, .{name}.build.lock",
            f"ref refs/bakar/{name}/{commit[:12]} in {remote_repo}",
            f"build directory {workspace}/{build_dir}",
        ]
    )
    rd.print_dispatch_plan(host, plan)
    console.print(f"  snapshot of uncommitted changes: {'yes' if snapshot else 'no'}")
    if untracked:
        console.print("  untracked paths the snapshot carries:")
        for path in untracked[:_PLAN_UNTRACKED_SHOWN]:
            console.print(f"    {path}", markup=False)
        if len(untracked) > _PLAN_UNTRACKED_SHOWN:
            console.print(f"    and {len(untracked) - _PLAN_UNTRACKED_SHOWN} more")

    try:
        push = layer_delivery.push_argv(checkout, host, remote_repo, commit, name)
    except layer_delivery.DeliveryError as exc:
        console.print(f"[red]cannot deliver {checkout}:[/] {exc}")
        return 1
    push_rc = subprocess.run(push, check=False).returncode
    if push_rc != 0:
        console.print(f"[red]git push failed (exit {push_rc})[/]; remote build not started.")
        if layer_delivery.is_shallow(checkout):
            console.print(
                f"{checkout} is a shallow checkout, which the host rejects when it lacks the boundary commit. "
                f"Run `ssh {host} git -C {remote_repo} fetch` and retry.",
                markup=False,
            )
        return push_rc

    script = layer_delivery_remote.ensure_worktree_script(
        workspace=str(workspace),
        worktree_root=str(root),
        name=name,
        commit=commit,
        snapshot=snapshot,
        source_host=socket.gethostname(),
        source_path=os.path.realpath(checkout),
        # Markers written before the canonical spelling carry the logical path,
        # which differs from it when the checkout is reached through a symlink.
        source_path_aliases=(str(checkout),),
    )
    ensured = rd._ssh_bash_capture(host, script)
    if ensured.returncode != 0:
        console.print(
            f"[red]could not place the worktree {target} on {host}:[/] "
            f"{layer_delivery_remote.describe_exit(ensured.returncode)}"
        )
        for text in (ensured.stdout.strip(), ensured.stderr.strip()):
            if text:
                console.print(text, markup=False)
        return ensured.returncode

    remote_script = rd.build_remote_script(
        remote_argv,
        target,
        remote_env(build_dir_template),
        sccache_off=not sccache_dist,
        guard=layer_delivery_remote.build_guard(worktree_root=str(root), name=name, commit=commit),
    )
    # Run ids are looked up under this dispatch's build directory: the release
    # workspace holds the runs of every worktree, and a concurrent one would
    # otherwise be named.
    return rd.run_remote_build(host, remote_script, workspace / build_dir)
