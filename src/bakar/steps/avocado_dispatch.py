"""Dispatch a meta-avocado build to a release workspace on another host.

The general ``--on`` path mirrors a whole workspace with rsync. A meta-avocado
build does not: the remote host already holds a workspace for the build's
release, so only the meta-avocado checkout's state travels - as a commit pushed
to ``refs/bakar/<name>/<commit12>`` of the remote clone and checked out as a
linked worktree at ``<worktree_root>/<name>/meta-avocado``. The remote build then runs from
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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape

from bakar.gitutil import run_git
from bakar.release_workspace import (
    DEFAULT_BUILD_DIR_TEMPLATE,
    REPO_POLICY_ENV,
    REPO_POLICY_REUSE,
    ReleaseRoutingError,
    is_valid_delivered_name,
    parse_repo_policy,
    worktree_build_dir_name,
)
from bakar.steps import kas_pins, layer_delivery, layer_delivery_remote, pin_probe, repo_reuse
from bakar.steps import remote_dispatch as rd

if TYPE_CHECKING:
    from collections.abc import Callable

    from bakar.bsp_detect import LayerRepo, LinkedWorktree

console = rd.console

# Answered only by a bakar that carries release-workspace dispatch: an older one
# exits non-zero on the unknown key, and it would also ignore BAKAR_REPO_POLICY.
_CAPABILITY_KEY = "dispatch.snapshot_untracked_cap_mb"
# Answered only by a bakar that carries layer-repository dispatch, for the same reason.
_LAYER_CAPABILITY_KEY = "dispatch.layer_repo_dir"
_META_AVOCADO_LABEL = "meta-avocado"
_LAYER_LABEL = "layer repository"
_PIN_CHECK_ON_HOST_NOTE = "the shared-clone pin check runs on the host when the build starts"
# How many untracked paths the plan names before summarising the rest.
_PLAN_UNTRACKED_SHOWN = 20
# Bounds the delivery push so a dead link ends it through the keepalive instead of hanging.
_PUSH_SSH_COMMAND = shlex.join(["ssh", *rd.SSH_BOUND_OPTS, "-o", "BatchMode=yes"])


def _sync_repair(kas_yaml: Path, host: str) -> str:
    """The ``bakar sync`` line a refusal tells the user to run, quoted for a shell."""
    return shlex.join(["bakar", "sync", str(kas_yaml), "--on", host])


_GIT_CONFIG_TIMEOUT_SECONDS = 10


def _user_ssh_configured(checkout: Path, env: dict[str, str]) -> bool:
    """True when the user already chose a git ssh command, or when we cannot tell.

    ``GIT_SSH_COMMAND`` / ``GIT_SSH`` in ``env`` and a non-empty
    ``core.sshCommand`` both count. A timeout, a missing git or any exit other
    than "key absent" (status 1, no output) is UNKNOWN and also counts: replacing
    a command we could not read would override the user's choice.
    """
    if env.get("GIT_SSH_COMMAND") or env.get("GIT_SSH"):
        return True
    result = run_git(
        ["git", "-C", str(checkout), "config", "--get", "core.sshCommand"], timeout=_GIT_CONFIG_TIMEOUT_SECONDS
    )
    if result is None:
        return True
    if result.returncode == 0:
        return bool(result.stdout.strip())
    return result.returncode != 1 or bool(result.stdout.strip())


class KasArgError(Exception):
    """A kas argument segment cannot be named relative to the delivered checkout."""


def _relative_to_checkout(segment: str, checkout: Path, cwd: Path, label: str = _META_AVOCADO_LABEL) -> str:
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
            f"kas file {segment!r} is outside the {label} checkout {checkout}, "
            "so it would not exist in the delivered worktree"
        ) from None


def rewrite_kas_arg(kas_arg: str, checkout: Path, cwd: Path, label: str = _META_AVOCADO_LABEL) -> str:
    """Rewrite a colon-joined kas argument to paths relative to ``checkout``."""
    return ":".join(_relative_to_checkout(segment, checkout, cwd, label) for segment in kas_arg.split(":"))


def with_workspace_option(argv: list[str], workspace: Path) -> list[str]:
    """Return ``argv`` with ``--workspace <workspace>`` added, before a ``--`` terminator when there is one."""
    option = ["--workspace", str(workspace)]
    if "--" in argv:
        at = argv.index("--")
        return [*argv[:at], *option, *argv[at:]]
    return [*argv, *option]


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


def remote_env(build_dir_template: str | None, worktree_value: str, *, kas_arg: str, workspace: str) -> dict[str, str]:
    """Forwarded ``BAKAR_*``/``KAS_*`` variables for the remote build.

    ``KAS_WORK_DIR`` names a local directory, and the remote workspace is chosen
    by where the delivered worktree sits, so it is dropped.

    ``BAKAR_DISPATCH_KAS`` (the kas argument as rewritten for the host) and
    ``BAKAR_DISPATCH_WORKSPACE`` come from the dispatch itself, never from the
    caller's environment, so the host run records where its kas file and
    workspace are.
    """
    env = {k: v for k, v in os.environ.items() if k.startswith(("BAKAR_", "KAS_")) and k != "KAS_WORK_DIR"}
    env[REPO_POLICY_ENV] = REPO_POLICY_REUSE
    # Always sent: the remote's own config must not pick another directory than
    # the one the plan names and the remote run is scoped to.
    # ``{worktree}`` is filled in here: the host derives it from the checkout
    # directory, which is meta-avocado for every meta-avocado delivery and the
    # layer name for every delivery of one layer, and would key all of them to one
    # build directory. The value holds only [A-Za-z0-9._-], so no brace.
    env["BAKAR_WORKTREE_BUILD_DIR"] = (build_dir_template or DEFAULT_BUILD_DIR_TEMPLATE).replace(
        "{worktree}", worktree_value
    )
    env["BAKAR_DISPATCH_KAS"] = kas_arg
    env["BAKAR_DISPATCH_WORKSPACE"] = workspace
    return env


def host_pairs_core_with_bitbake(remote_argv: list[str], env: dict[str, str]) -> bool:
    """Whether the host build will run in host mode, the only mode that pairs oe-core with bitbake.

    Applies the host's own precedence (``config.resolve``) to what dispatch
    forwards: ``--container`` beats ``--host``, which beats ``BAKAR_CONTAINER``
    (``1/true/yes/on`` truthy, any other non-blank value falsy), which beats
    the host-mode default. A host whose own workspace or user config selects
    the container is invisible here; the in-build check still catches it.
    """
    if "--container" in remote_argv:
        return False
    if "--host" in remote_argv:
        return True
    toggle = env.get("BAKAR_CONTAINER", "").strip()
    return not toggle or toggle.lower() not in ("1", "true", "yes", "on")


def _check_remote_capability(host: str, capability_key: str) -> bool:
    """Refuse a host whose bakar predates release-workspace dispatch, whatever ``--yes`` says.

    The version check is overridable, and an older remote bakar runs plain kas:
    it ignores ``BAKAR_REPO_POLICY=reuse`` and moves the workspace's shared
    clones to their pins. Asking for a setting only this change added is a
    read-only probe that such a bakar fails.
    """
    result = rd._ssh_bash_capture(host, f"bakar settings get {shlex.quote(capability_key)}\n")
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
    shown = escape(stderr) if stderr else "(no stderr)"
    if capability_key == _LAYER_CAPABILITY_KEY:
        console.print(
            f"[red]could not confirm that the bakar on {host} supports layer-repository dispatch[/] "
            f"(exit {result.returncode}): {shown}; it does not know {capability_key}. An older bakar would build a "
            "layer entry with the kas policy and move the workspace's shared clones off their pins; update bakar on "
            "the host (git pull + uv tool install --force). --yes does not override this."
        )
        return False
    console.print(
        f"[red]could not confirm that the bakar on {host} supports release-workspace dispatch[/] "
        f"(exit {result.returncode}): {shown}; if that bakar predates this "
        f"feature, update it (git pull + uv tool install --force). Without it {REPO_POLICY_ENV} would be ignored "
        "and the workspace's shared clones moved off their pins; --yes does not override this."
    )
    return False


@dataclass(frozen=True, kw_only=True, slots=True)
class AvocadoDispatchRequest:
    """One meta-avocado checkout's build, its release workspace, and the delivery settings for the host."""

    workspace: Path
    checkout: Path
    worktree: LinkedWorktree | None
    kas_yaml: Path
    kas_arg: str
    local_args: list[str]
    release: str
    worktree_root: Path | None
    build_dir_template: str | None
    cap_mb: int
    invoking_cwd: Path | None = None
    # ``None`` means "read them from the kas file"; ``{}`` is a real value (no pinned repositories).
    pinned_repos: dict | None = None
    workspace_source: str | None = None
    # Names the checkout in a refusal about a kas segment lying outside it.
    checkout_label: str = _META_AVOCADO_LABEL


@dataclass(frozen=True, kw_only=True, slots=True)
class DispatchTarget:
    """Where a dispatch's commit lands on the host and what the plan says about it.

    Everything that differs between delivering the meta-avocado checkout and
    delivering another repository is here; the core in :func:`_dispatch` holds the
    refusals, the plan, the push, the lock/marker script and the build guard once.
    """

    # Repository the commit is pushed to and the worktree is added to. ``None`` is
    # the workspace's ``meta-avocado`` clone, the same convention as
    # ``layer_delivery_remote.DeliveryTarget.repo_git``.
    repo_git: str | None
    # Directory holding the delivered worktree and its ``.<name>.*`` bookkeeping files.
    worktree_root: Path
    delivered_name: str
    # Build directory name inside the workspace.
    build_dir: str
    # What ``{worktree}`` in the build-directory template is filled with before it
    # is sent to the host, so the host's own derivation from the checkout directory
    # never decides the directory.
    worktree_value: str
    # The worktree's directory below ``<worktree_root>/<delivered_name>``.
    checkout_dirname: str = layer_delivery_remote.CHECKOUT_DIRNAME
    # The ``workspace:`` line of the plan.
    workspace_line: str
    # Setting the host's bakar must answer, or ``None`` to skip the capability probe.
    capability_key: str | None
    # Script that makes ``repo_git`` exist on the host, or ``None`` when it already does.
    # It runs after the host checks and before the push.
    ensure_repo_script: str | None
    # A layer's repository is bare and the remote build gets its workspace as an option,
    # because the delivered worktree has no main checkout beside the workspace.
    forward_workspace: bool = False
    # Plan line saying the host checks the shared clones itself at build start; when set,
    # the dispatch does not probe them from here.
    pin_check_note: str | None = None
    # The plan is printed ahead of a workspace refusal in a dry run.
    plan_before_refusal: bool = False


def _remote_repo(target: DispatchTarget, workspace: Path) -> str:
    """The host path of the repository the commit is pushed to."""
    return target.repo_git if target.repo_git is not None else f"{workspace}/meta-avocado"


def _admin_dir(target: DispatchTarget, remote_repo: str) -> str:
    """The git directory of ``remote_repo``: the repository itself when bare, else its ``.git``."""
    return remote_repo if target.repo_git is not None else f"{remote_repo}/.git"


def _layer_target(request: AvocadoDispatchRequest, layer: LayerRepo, layer_repo_dir: Path | None) -> DispatchTarget:
    """The target that delivers a layer repository into its own bare repository on the host."""
    # The name becomes a directory and a repository file name on the host.
    if not is_valid_delivered_name(layer.name):
        raise ReleaseRoutingError(f"layer repository name {layer.name!r} must match [A-Za-z0-9._-]+")
    # The host recognises a meta-avocado checkout by a directory of that name, so a
    # layer worktree named after the layer must not take it.
    if layer.name == layer_delivery_remote.CHECKOUT_DIRNAME:
        raise ReleaseRoutingError(
            f"layer repository name {layer.name!r} is the meta-avocado checkout directory name; "
            "the host would take the layer for meta-avocado"
        )
    name = layer_delivery.layer_delivery_name(layer)
    # The host's own derivation sees only the checkout directory (the layer name for
    # every delivery of this layer), so the template is sent with this value already
    # in place: two deliveries of one layer, two layers under one name and a layer
    # beside a meta-avocado file of the same stem each keep their own directory.
    worktree_value = f"{layer.name}-{name}"
    base = request.worktree_root if request.worktree_root is not None else request.workspace / ".bakar" / "worktrees"
    repos_dir = layer_repo_dir if layer_repo_dir is not None else request.workspace / ".bakar" / "layers"
    bare = f"{repos_dir}/{layer.name}.git"
    return DispatchTarget(
        repo_git=bare,
        worktree_root=base / layer.name,
        delivered_name=name,
        build_dir=worktree_build_dir_name(request.kas_yaml.stem, worktree_value, request.build_dir_template),
        worktree_value=worktree_value,
        checkout_dirname=layer.name,
        workspace_line=f"{request.workspace} ({request.workspace_source or 'release routing'})",
        capability_key=_LAYER_CAPABILITY_KEY,
        ensure_repo_script=layer_delivery_remote.ensure_bare_repo_script(bare),
        forward_workspace=True,
        pin_check_note=_PIN_CHECK_ON_HOST_NOTE,
        plan_before_refusal=True,
    )


def _meta_avocado_target(request: AvocadoDispatchRequest) -> DispatchTarget:
    """The target that delivers the meta-avocado checkout into the release workspace's own clone."""
    name = layer_delivery.delivered_name(request.checkout, request.worktree)
    root = request.worktree_root if request.worktree_root is not None else request.workspace / ".bakar" / "worktrees"
    return DispatchTarget(
        repo_git=None,
        worktree_root=root,
        delivered_name=name,
        build_dir=worktree_build_dir_name(request.kas_yaml.stem, name, request.build_dir_template),
        worktree_value=name,
        workspace_line=f"{request.workspace} ({request.workspace_source or 'release routing'})",
        capability_key=_CAPABILITY_KEY,
        ensure_repo_script=None,
    )


def dispatch_avocado_build(host: str, request: AvocadoDispatchRequest, flags: rd.DispatchFlags) -> int:
    """Deliver ``request.checkout`` to the ``request.release`` workspace on ``host`` and build there.

    ``checkout`` must already be the logical (unresolved) path: it is recorded
    in the remote marker as the delivery's source. ``invoking_cwd`` is where
    relative kas segments are resolved from (default: the current directory).
    Returns the remote build's exit code, 130 on Ctrl-C, or ``_DISPATCH_LOST_EXIT``
    when the log stream is lost while the build keeps running.
    """
    return _dispatch(host, request, flags, _meta_avocado_target)


def dispatch_layer_build(
    host: str,
    request: AvocadoDispatchRequest,
    layer: LayerRepo,
    layer_repo_dir: Path | None,
    flags: rd.DispatchFlags,
) -> int:
    """Deliver the layer repository ``layer`` to the ``request.release`` workspace on ``host`` and build there.

    ``request`` is the same description :func:`dispatch_avocado_build` takes, with
    ``workspace`` the routed workspace and ``checkout`` the layer's logical checkout;
    ``worktree`` and ``pinned_repos`` are not used. ``layer_repo_dir`` is the
    ``[dispatch] layer_repo_dir`` setting: the host directory holding the bare
    repositories, or ``None`` for ``<workspace>/.bakar/layers``. The commit goes to
    ``<layer_repo_dir>/<layer name>.git``, never to the workspace's meta-avocado
    clone, and the host's shared clones are checked by the host's own build.
    Returns what :func:`dispatch_avocado_build` returns.
    """
    layer_request = replace(request, checkout_label=_LAYER_LABEL, pinned_repos={})
    return _dispatch(host, layer_request, flags, lambda req: _layer_target(req, layer, layer_repo_dir))


def _dispatch(
    host: str,
    request: AvocadoDispatchRequest,
    flags: rd.DispatchFlags,
    make_target: Callable[[AvocadoDispatchRequest], DispatchTarget],
) -> int:
    """Deliver ``request.checkout`` to the host as ``make_target`` describes and build there.

    ``make_target`` runs where the delivered name is resolved, so an unusable name
    is refused before the host is contacted.
    """
    workspace, checkout = request.workspace, request.checkout
    kas_yaml, kas_arg, local_args = request.kas_yaml, request.kas_arg, request.local_args
    release, invoking_cwd = request.release, request.invoking_cwd
    worktree_root, build_dir_template, cap_mb = request.worktree_root, request.build_dir_template, request.cap_mb
    sccache_dist, assume_yes = flags.sccache_dist, flags.assume_yes
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
        remote_kas = rewrite_kas_arg(kas_arg, checkout, cwd, request.checkout_label)
        remote_argv = build_remote_argv(local_args, kas_arg, remote_kas)
    except KasArgError as exc:
        console.print(f"[red]cannot dispatch:[/] {exc}")
        return 2

    try:
        target = make_target(request)
        name, build_dir, root = target.delivered_name, target.build_dir, target.worktree_root
        remote_repo = _remote_repo(target, workspace)
        if target.forward_workspace:
            remote_argv = with_workspace_option(remote_argv, workspace)
        if flags.dry_run:
            # Same refusal checks as snapshot_commit, but no git object is written.
            preview = layer_delivery.snapshot_preview(checkout, cap_mb=cap_mb)
            commit, snapshot, untracked = preview.head, preview.dirty, preview.untracked
            ref_name = (
                f"refs/bakar/{name}/<snapshot of HEAD {commit[:12]}, computed at dispatch>"
                if snapshot
                else f"refs/bakar/{name}/{commit[:12]}"
            )
        else:
            commit, snapshot = layer_delivery.snapshot_commit(checkout, cap_mb=cap_mb)
            untracked = layer_delivery.snapshot_untracked_paths(checkout) if snapshot else []
            ref_name = f"refs/bakar/{name}/{commit[:12]}"
    except ReleaseRoutingError as exc:
        console.print(f"[red]cannot dispatch:[/] {exc}")
        return 2
    except layer_delivery.DeliveryError as exc:
        console.print(f"[red]cannot deliver {checkout}:[/] {exc}")
        return 1

    target_dir = root / name / target.checkout_dirname
    writes = [
        f"worktree {target_dir} (created, or moved to the delivered commit), with its git admin directory "
        f"an entry under {_admin_dir(target, remote_repo)}/worktrees holding the delivery marker "
        f"{layer_delivery_remote.MARKER_FILENAME}",
        f"bookkeeping files in {root} (created if missing): .{name}.lock, .{name}.pending, .{name}.build.lock",
        f"ref {ref_name} in {remote_repo}",
        f"build directory {workspace}/{build_dir}",
    ]
    if target.ensure_repo_script is not None:
        writes.insert(0, f"bare repository {remote_repo} (created if absent)")
    plan = rd.DispatchPlan(workspace=target.workspace_line, writes=writes)

    def show_plan(side_repos: list[repo_reuse.RepoState]) -> None:
        rd.print_dispatch_plan(host, plan)
        if target.pin_check_note is not None:
            console.print(f"  {target.pin_check_note}", markup=False)
        if side_repos:
            console.print("  side checkouts the host build will use:")
            for st in side_repos:
                console.print(
                    f"    {repo_reuse.printable(st.name)}: {repo_reuse.printable(st.side_path)}",
                    markup=False,
                    soft_wrap=True,
                )
        console.print(f"  snapshot of uncommitted changes: {'yes' if snapshot else 'no'}")
        if untracked:
            console.print("  untracked paths the snapshot carries:")
            for path in untracked[:_PLAN_UNTRACKED_SHOWN]:
                console.print(f"    {path}", markup=False)
            if len(untracked) > _PLAN_UNTRACKED_SHOWN:
                console.print(f"    and {len(untracked) - _PLAN_UNTRACKED_SHOWN} more")

    if not rd.preflight_or_report(host, assume_yes=assume_yes):
        return 1
    if target.capability_key is not None and not _check_remote_capability(host, target.capability_key):
        return 1
    try:
        rd.check_remote_workspace(host, str(workspace), release)
    except rd.DispatchError as exc:
        if flags.dry_run and target.plan_before_refusal:
            show_plan([])
        console.print(f"[red]remote workspace check failed:[/] {exc}")
        return 1

    # Classified on the host by the build's own rules, before anything is written
    # there; --yes does not reach this check. A target with a pin-check note leaves
    # the classification to the host's own build.
    entries: list = []
    if target.pin_check_note is None:
        repos = request.pinned_repos
        if repos is None:
            try:
                repos = kas_pins.read_merged_config(kas_yaml, checkout=checkout).get("repos") or {}
            except ReleaseRoutingError as exc:
                console.print(f"[red]cannot dispatch:[/] {exc}")
                return 2
        entries = repo_reuse.pinned_entries(repos, workspace)
    states: list[repo_reuse.RepoState] = []
    blocked = False
    if entries:
        try:
            facts = pin_probe.probe_host(host, entries)
        except pin_probe.PinProbeIncomplete as exc:
            console.print(
                f"[red]the shared-clone pin check on {host} did not complete:[/] {escape(repo_reuse.printable(exc))}"
            )
            return 1
        states = repo_reuse.classify_facts(
            entries,
            facts,
            pair_core_with_bitbake=host_pairs_core_with_bitbake(
                remote_argv,
                remote_env(build_dir_template, target.worktree_value, kas_arg=remote_kas, workspace=str(workspace)),
            ),
        )
        blocked = bool(repo_reuse.blocking(states))
        # A dry run still prints the table before it refuses, so the refusal waits for it.
        if blocked and not flags.dry_run:
            console.print(
                repo_reuse.render_refusal(states, repair=_sync_repair(kas_yaml, host)), markup=False, soft_wrap=True
            )
            return 1
    show_plan([st for st in states if st.state == "side" and st.blocking is None])

    if flags.dry_run:
        if states:
            console.print("  shared-clone pin states on the host:")
            for line in repo_reuse.render_pin_table(states):
                console.print(f"    {line}", markup=False)
        # Every read-only check has run; nothing below this point is reachable in a preview.
        if blocked:
            console.print(
                repo_reuse.render_refusal(states, repair=_sync_repair(kas_yaml, host)), markup=False, soft_wrap=True
            )
            return 1
        console.print(f"dry run: nothing was pushed or created on {host}")
        return 0

    if target.ensure_repo_script is not None:
        created = rd._ssh_bash_capture(host, target.ensure_repo_script)
        if created.returncode != 0:
            console.print(
                f"[red]could not prepare the repository {escape(remote_repo)} on {host}:[/] "
                f"{layer_delivery_remote.describe_exit(created.returncode)}"
            )
            for text in (created.stdout.strip(), created.stderr.strip()):
                if text:
                    console.print(text, markup=False)
            return created.returncode

    try:
        push = layer_delivery.push_argv(checkout, host, remote_repo, commit, name)
    except layer_delivery.DeliveryError as exc:
        console.print(f"[red]cannot deliver {checkout}:[/] {exc}")
        return 1
    push_env = os.environ.copy()
    if not _user_ssh_configured(checkout, push_env):
        push_env["GIT_SSH_COMMAND"] = _PUSH_SSH_COMMAND
    # ``push`` is an argv list run without a shell, and ``preflight_or_report``
    # above already refused a host that begins with ``-``, so the host cannot
    # parse as an ssh or git option.
    # The directive must stay on the line directly above the call.
    # nosemgrep: python.django.security.injection.command.subprocess-injection.subprocess-injection
    push_rc = subprocess.run(push, check=False, env=push_env).returncode
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
        layer_delivery_remote.DeliveryTarget(
            workspace=str(workspace),
            worktree_root=str(root),
            repo_git=target.repo_git,
            checkout_dirname=target.checkout_dirname,
        ),
        layer_delivery_remote.MarkerFields(
            source_host=socket.gethostname(),
            source_path=os.path.realpath(checkout),
            name=name,
            commit=commit,
            snapshot=snapshot,
        ),
        # Markers written before the canonical spelling carry the logical path,
        # which differs from it when the checkout is reached through a symlink.
        source_path_aliases=(str(checkout),),
    )
    ensured = rd._ssh_bash_capture(host, script, timeout=rd.DELIVERY_SCRIPT_TIMEOUT_SECONDS)
    if isinstance(ensured, rd.CaptureTimedOut):
        # Only the local ssh was killed. The host answered the preflight, so
        # "could not reach" would be false, and the script may still be running.
        console.print(
            f"[red]the delivery script did not finish within {ensured.timeout:g}s on {host}:[/] "
            f"{host} was reachable, but the script may still be running there, so retrying right away "
            f"can hit the delivery lock. Wait a few minutes, or inspect {host} (the worktree under "
            f"{escape(str(root))}), before retrying."
        )
        return ensured.returncode
    if ensured.returncode != 0:
        console.print(
            f"[red]could not place the worktree {target_dir} on {host}:[/] "
            f"{layer_delivery_remote.describe_exit(ensured.returncode)}"
        )
        for text in (ensured.stdout.strip(), ensured.stderr.strip()):
            if text:
                console.print(text, markup=False)
        return ensured.returncode

    remote_script = rd.build_remote_script(
        rd.RemoteScriptSpec(
            remote_argv=remote_argv,
            cwd=target_dir,
            env_vars=remote_env(
                build_dir_template, target.worktree_value, kas_arg=remote_kas, workspace=str(workspace)
            ),
            sccache_off=not sccache_dist,
            guard=layer_delivery_remote.build_guard(
                worktree_root=str(root), name=name, commit=commit, checkout_dirname=target.checkout_dirname
            ),
            detach=flags.detach,
        )
    )
    # Run ids are looked up under this dispatch's build directory: the release
    # workspace holds the runs of every worktree, and a concurrent one would
    # otherwise be named.
    return rd.run_remote_build(
        host, remote_script, workspace / build_dir, detach=flags.detach, hint_lines=flags.hint_lines
    )
