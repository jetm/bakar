"""Regenerate the kas YAML and run the kas build.

The YAML generator lives in :mod:`bakar.kas`; this step wraps it
plus the build invocation, and layers in
the static tuning overlay (``overlays/bakar-tuning-<bsp>.yml``)
on top of whatever kas YAML the caller passes in.

Two execution modes share every code path here. Host mode invokes ``kas``
directly and is the default; container mode invokes ``kas-container`` and is
reached only through an explicit opt-in (``--container``, ``BAKAR_CONTAINER``,
or a ``[build] container`` toggle). The mode is carried on
``BuildConfig.host_mode`` and selects only the executable name plus the
container-specific extras noted below - path translation into ``/work``, the
runtime label used by ``bakar stop``, and the ``KAS_RUNTIME_ARGS`` handling.
Everything else - overlays, parallelism, PTY, UI parsing, telemetry - is
mode-independent.

A pseudo-TTY is allocated for the kas-container subprocess so that
``kas-container``'s ``[ -t 1 ]`` check passes and it attaches ``-t -i`` on
the ``docker run`` call. That enables bitbake's knotty interactive UI
inside the container, which emits footer lines including
``Currently N running tasks (X of Y)`` and per-task lines like
``N: PF do_task - elapsed (pid P)`` several times per second.
These are parsed by :mod:`bakar.steps.build_ui` into a Rich Live
display. The PTY also means bitbake's stdout is line-flushed rather
than block-buffered, so ``bakar log`` and the progress bar stay
responsive during long compile phases.

The live UI never re-displays the in-container recipe-log path
(``/work/.../log.do_<task>``); raw kas.log lines are streamed through
unchanged. Container-to-host recipe-log path translation lives in
:func:`bakar.triage.translate_container_path` and is applied only by
``bakar triage`` when it surfaces the failing recipe log. No host-path
rewrite is needed here.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from rich.markup import escape

from bakar import build_scope, build_stop, hashserv, prserv, sccache_server, task_timings
from bakar.config import GENERATED_BUILD_YAML, BuildConfig
from bakar.diagnostics import (
    BUILDTOOLS_DIR_ENV,
    detect_buildtools,
    is_path_on_nfs,  # noqa: F401 - re-exported; kas_lock.clear_stale_bitbake_locks patches it via kas_build.is_path_on_nfs
    probe_cluster,  # noqa: F401 - re-exported; kas_overlay._derive_parallelism_plan patches it via kas_build.probe_cluster
    resolve_oe_core_release_key,
)
from bakar.kas import KasGenOptions, write_yaml
from bakar.observability import RunLogger
from bakar.output_mode import OutputMode
from bakar.psi import PSI_DIMS, apply_autocalibration, read_psi_avg10
from bakar.steps.build_ui import BuildUIState
from bakar.steps.kas_graph_capture import (
    GRAPH_ARTIFACTS,  # noqa: F401 - re-exported for external/test imports
    GRAPH_CAPTURE_IDLE_TIMEOUT_S,  # noqa: F401 - re-exported for external/test imports
    GRAPH_CAPTURE_POLL_S,  # noqa: F401 - re-exported for external/test imports
    GRAPH_CAPTURE_TIMEOUT_S,  # noqa: F401 - re-exported for external/test imports
    GRAPH_MARKER_NAME,  # noqa: F401 - re-exported for external/test imports
    _capture_dependency_graph,
    _resolve_capture_target,  # noqa: F401 - re-exported; tests monkeypatch kas_build._resolve_capture_target
    _wait_for_cooker_idle,  # noqa: F401 - re-exported; tests monkeypatch kas_build._wait_for_cooker_idle
    graph_capture_command,  # noqa: F401 - re-exported for external/test imports
)
from bakar.steps.kas_lock import (
    LockHeldByPeerError,
    _lock_holder_has_activity,  # noqa: F401 - re-exported; kas_graph_capture patches it via kas_build._lock_holder_has_activity
    _lock_refusal_message,
    clear_stale_bitbake_locks,
    lock_owner_marker,
)
from bakar.steps.kas_overlay import (
    _MOLD_LAYER_NAME,
    _OVERLAY_DIR_RELPATH,
    _derive_parallelism_plan,
    _inject_literal_ccache,  # noqa: F401 - re-exported for external/test imports
    _inject_literal_mold,  # noqa: F401 - re-exported for external/test imports
    _inject_literal_parallelism,  # noqa: F401 - re-exported for external/test imports
    _inject_literal_sccache,  # noqa: F401 - re-exported for external/test imports
    _inject_local_tmpdir,  # noqa: F401 - re-exported for external/test imports
    _resolve_parallelism,  # noqa: F401 - re-exported for external/test imports
    materialize_cache_classify_layer,
    materialize_host_layer,
    materialize_layer,
    materialize_overlay,
    materialize_sccache_layer,
)
from bakar.steps.kas_pty import (
    _PLAIN_STATUS_INTERVAL,  # noqa: F401 - re-exported; _PlainFrameController._loop patches it via kas_build._PLAIN_STATUS_INTERVAL
    _build_fail_reason,
    _PlainFrameController,  # noqa: F401 - re-exported for external/test imports
    _print_cache_summary,
    _PtyCtx,
    _PtyOutcome,
    _run_pty_with_ui,
)
from bakar.triage import translate_container_path, write_error_report

if TYPE_CHECKING:
    from typing import IO

    from bakar.bsp_model import BspModel
    from bakar.config import BuildConfig
    from bakar.observability import RunLogger


def _setup_meta_avocado_build_dir(cfg: BuildConfig) -> None:
    """Create the build directory for Avocado OS builds.

    Idempotent: safe to call on every build invocation.
    """
    cfg.bsp_root.mkdir(parents=True, exist_ok=True)


def _write_meta_avocado_wrapper(cfg: BuildConfig, kas_yaml: Path) -> Path:
    """Write a wrapper YAML that includes the machine YAML via repo reference.

    The wrapper is the single top-level file fed to ``kas dump``. It declares
    the entry YAML's own repo as a local repo so kas can resolve the
    ``repo: <name>`` include. The overlay is passed separately as
    the second colon-joined argument to ``kas dump`` (both wrapper and
    overlay live in ``bsp_root``, which shares the same git root, so
    the same-repo check passes).

    The entry YAML is usually inside meta-avocado itself, but it need not be: a
    build can start from a repo beside it that pulls the public config in with a
    cross-repo ``include``. That indirection is not a preference - kas rejects
    concatenating config files from two different repositories outright, so a
    config living in another repo cannot be appended to a meta-avocado one and
    has to be the entry point instead.

    Returns the wrapper path (``bsp_root/avocado-wrapper.yml``).
    """
    abs_yaml = kas_yaml.resolve()
    repo_dir: Path | None = None
    for parent in [abs_yaml, *abs_yaml.parents]:
        if parent.name == "meta-avocado":
            repo_dir = parent
            break
    if repo_dir is None:
        # Look for the entry YAML in a repo checked out beside meta-avocado.
        # Anchored on meta-avocado's own parent rather than on cfg.workspace,
        # because the repos are not always directly under it - a manifest
        # checkout nests them one level down, in sources/.
        meta = cfg.workspace / "meta-avocado"
        if meta.is_dir():
            siblings = meta.resolve().parent
            for parent in abs_yaml.parents:
                if parent.parent == siblings:
                    repo_dir = parent
                    break
    if repo_dir is None:
        raise RuntimeError(f"kas YAML {kas_yaml} is not inside a meta-avocado repository")
    repo_name = repo_dir.name
    yaml_in_repo = abs_yaml.relative_to(repo_dir)
    wrapper = cfg.bsp_root / "avocado-wrapper.yml"
    wrapper.write_text(
        "header:\n"
        "  version: 16\n"
        "  includes:\n"
        f"    - repo: {repo_name}\n"
        f"      file: {yaml_in_repo.as_posix()}\n"
        "repos:\n"
        f"  {repo_name}:\n"
        f"    path: {repo_name}\n",
        encoding="utf-8",
    )
    return wrapper


def _strip_branch_from_dump(dump_path: Path) -> None:
    """Remove ``branch:`` from repos that have a pinned ``commit:``.

    When both are present, kas validates that ``origin/<branch>`` contains
    the commit after a remote fetch inside the container. If the remote was
    rebased the hash is no longer reachable from the branch, failing the
    build even though the commit is locally present. Keeping only ``commit:``
    avoids that validation without changing the checkout target.
    """
    data = yaml.safe_load(dump_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        return
    changed = False
    for repo in data["repos"].values():
        if isinstance(repo, dict) and repo.get("commit") and "branch" in repo:
            del repo["branch"]
            changed = True
    if changed:
        dump_path.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False, indent=4),
            encoding="utf-8",
        )


def _run_kas_dump(
    cfg: BuildConfig,
    wrapper: Path,
    overlay_rel: Path,
    extra_overlay_rels: list[Path] | None = None,
) -> Path:
    """Run ``kas dump`` on wrapper + overlay and write the resolved output.

    The overlay is the second colon-joined argument; both wrapper and
    overlay live in ``bsp_root`` (same git root as the peridio workspace),
    so kas's same-repo check passes. Runs with ``KAS_WORK_DIR=cfg.workspace``
    so ``path: meta-avocado`` and sibling repos resolve against ``sources/``.

    The dump output is a self-contained YAML: no ``header.includes``, all
    repos pinned by commit, overlay content merged in. The container never
    needs to do include resolution or access overlay files directly.

    Returns the dump file path (``bsp_root/avocado-bakar.yml``).
    """
    env = {**os.environ, "KAS_WORK_DIR": str(cfg.workspace)}
    kas_files = f"{wrapper.name}:{overlay_rel.as_posix()}"
    for extra in extra_overlay_rels or []:
        kas_files += f":{extra.as_posix()}"
    result = subprocess.run(  # pragma: no cover
        ["kas", "dump", kas_files],
        cwd=str(cfg.bsp_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    dump = cfg.bsp_root / GENERATED_BUILD_YAML
    if result.returncode == 0:
        dump.write_text(result.stdout, encoding="utf-8")
        _strip_branch_from_dump(dump)
        return dump

    # Remote branch was rebased: the commit hash in the YAML is no longer
    # reachable from origin/<branch> even though it is present locally.
    # kas validates against the remote tracking ref, so the checkout step
    # fails. Retry skipping that validation - all repos are locally present.
    _git_state_markers = ("does not contain commit", "no such remote ref")
    if any(m in result.stderr for m in _git_state_markers):
        retry = subprocess.run(  # pragma: no cover
            ["kas", "dump", "--skip", "repos_checkout", kas_files],
            cwd=str(cfg.bsp_root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if retry.returncode == 0:
            dump.write_text(retry.stdout, encoding="utf-8")
            _strip_branch_from_dump(dump)
            return dump
        raise RuntimeError(f"kas dump --skip repos_checkout failed (exit {retry.returncode}):\n{retry.stderr}")

    raise RuntimeError(f"kas dump failed (exit {result.returncode}):\n{result.stderr}")


def _resolve_user_yaml(cfg: BuildConfig, kas_yaml: Path) -> Path:
    """Return ``kas_yaml`` as a path relative to ``cfg.bsp_root``.

    kas-container's bind mount only covers ``KAS_WORK_DIR`` (=
    ``bsp_root``), so a YAML living outside that subtree cannot be
    read from inside the container. Reject those inputs with a clear
    error rather than letting kas-container fail with an opaque
    "config file not found" message.

    meta-avocado exception: the YAML lives inside the ``meta-avocado``
    source tree, which is accessible from ``bsp_root`` via the
    ``meta-avocado`` symlink created by :func:`_setup_meta_avocado_build_dir`.
    For those builds the relative path is derived via that symlink
    (e.g. ``meta-avocado/kas/machine/qemux86-64.yml``) so kas-container
    can resolve it inside ``/work``.
    """
    abs_path = kas_yaml.resolve()
    try:
        return abs_path.relative_to(cfg.bsp_root)
    except ValueError as exc:
        if cfg.is_meta_avocado:
            # Walk up from the YAML to find the meta-avocado boundary,
            # then express the path via the symlink in bsp_root.
            for parent in [abs_path, *abs_path.parents]:
                if parent.name == "meta-avocado":
                    return Path("meta-avocado") / abs_path.relative_to(parent)
        raise RuntimeError(
            f"kas YAML {abs_path} is outside bsp_root {cfg.bsp_root}; "
            f"copy it under {cfg.bsp_root}/ (e.g. as {cfg.bsp_root}/my-build.yml) and re-run."
        ) from exc


def _ccache_args(
    cfg: BuildConfig,
    *,
    dry_run: bool = False,
    eventlog_path: str | None = None,
    run_id: str | None = None,
) -> list[str]:
    """Return ``['--runtime-args', '<concatenated string>']`` for container builds.

    ``kas-container`` unconditionally resets ``KAS_RUNTIME_ARGS`` to its own
    defaults before its option-parsing loop, so injecting the flag via an env
    var is silently discarded.  The ``--runtime-args`` CLI flag (processed
    after the reset) is the only reliable injection point.  Returns an empty
    list for host-mode builds where no container is involved, and also when
    no runtime arg ends up needed at all (ccache disabled and none of
    hashequiv / sccache-dist / eventlog / run_id apply).

    The returned list, when non-empty, is shaped as exactly two elements:
    ``--runtime-args`` followed by a single concatenated string value.
    kas-container parses ``--runtime-args`` as one string; emitting two
    ``--runtime-args`` pairs would let the second occurrence overwrite the
    first.

    The string contains the workspace ccache bind mount only when
    ``cfg.ccache`` is enabled - mounting and creating that directory for a
    disabled feature would be pure waste, and on bakar's NFS-shared
    ``ccache_dir`` topology a needless mount there is exactly the cost this
    default was changed to avoid. When ``cfg.use_hashequiv`` is True,
    ``--add-host=host.docker.internal:gateway`` is appended so the container
    can reach the hashserv daemon on the host bridge. When ``eventlog_path``
    is provided, ``-e BB_DEFAULT_EVENTLOG=<path>`` is appended so bitbake
    inside the container writes its event log to the run-dir path that is
    bind-mounted under ``/work``. kas-container only forwards a fixed
    env-var allowlist into Docker, so this is the only reliable way to pass
    ``BB_DEFAULT_EVENTLOG`` through.

    Creates the host-side ccache directory when ccache is enabled and absent
    so the Docker bind-mount never targets a missing path. When ``dry_run``
    is True, the directory is not created so a preview invocation has no
    filesystem effect.
    """
    if cfg.host_mode:
        return []
    runtime_args = ""
    if cfg.ccache:
        ccache_host = cfg.effective_ccache_dir
        if not dry_run:
            ccache_host.mkdir(parents=True, exist_ok=True)
        runtime_args = f"-v {ccache_host}:/work/ccache:rw"
    # Always add the host mapping when hashequiv is enabled: _build_env calls
    # ensure_running() after _ccache_args, so the daemon may not be alive yet on
    # the first build. The flag is harmless when the daemon is absent and
    # mandatory when it is running. sccache-dist needs the same route when its
    # scheduler is on the host (localhost), reached via the gateway alias.
    need_host_gateway = cfg.use_hashequiv
    if cfg.use_sccache_dist:
        # The in-container compiler launcher is the host sccache binary, mounted
        # below. sccache reads its scheduler URL and auth token only from the
        # config file (no env override exists for either), so the config must be
        # both readable in the container and name a scheduler the container can
        # reach. kas-container forwards only a fixed env allowlist and drops
        # BAKAR_*, so the two vars sccache *does* read from the environment
        # (SCCACHE_CONF, SCCACHE_DIR) are injected via `-e` here, then whitelisted
        # by the sccache overlay's env block and re-exported into the compile
        # tasks by its bbclass. The config's scheduler_url must be a host LAN
        # address, not localhost (localhost inside the container is the container
        # itself); the doctor check warns when it is not.
        sccache_bin = shutil.which("sccache")
        if sccache_bin is not None:
            # kas runs bitbake with a sanitized PATH (/usr/sbin:/usr/bin:/sbin:/bin,
            # see kas libkas.py) that excludes /usr/local/bin, so mount into /usr/bin
            # or bitbake's HOSTTOOLS check fails to find sccache inside the container.
            runtime_args += f" -v {sccache_bin}:/usr/bin/sccache:ro"
        sccache_conf = Path.home() / ".config" / "sccache" / "config"
        if sccache_conf.is_file():
            # kas sets HOME to a throwaway temp dir, so XDG discovery misses the
            # config; mount it at its own absolute path and point SCCACHE_CONF
            # straight at it so the scheduler URL and token resolve regardless of
            # HOME.
            runtime_args += f" -v {sccache_conf}:{sccache_conf}:ro"
            runtime_args += f" -e BAKAR_SCCACHE_CONF={sccache_conf}"
        # The config's [cache.disk] dir (~/.cache/sccache) is absent and unwritable
        # in the container; without an override sccache fails to start its server
        # and every compile falls back to local. Redirect it under the /work mount.
        runtime_args += " -e BAKAR_SCCACHE_DIR=/work/.sccache-cache"
    if need_host_gateway:
        runtime_args += " --add-host=host.docker.internal:host-gateway"
    if eventlog_path is not None:
        runtime_args += f" -e BB_DEFAULT_EVENTLOG={eventlog_path}"
    if run_id is not None:
        runtime_args += f" --label {build_stop.run_id_label(run_id)}"
    runtime_args = runtime_args.strip()
    if not runtime_args:
        return []
    return ["--runtime-args", runtime_args]


def regenerate_yaml(cfg: BuildConfig, log: RunLogger, *, bsp: BspModel) -> None:
    """Run the topology-only kas YAML generator, writing to ``cfg.default_kas_yaml``."""
    log.step_start("gen_kas", target=cfg.image)
    output = cfg.default_kas_yaml
    opts = KasGenOptions(
        manifest=cfg.manifest_path,
        bblayers=cfg.bblayers_conf if cfg.bblayers_conf.is_file() else None,
        machine=cfg.machine,
        distro=cfg.distro,
        target=cfg.image,
        output=output,
        workspace=cfg.workspace,
        template=bsp.kas_template,
        skip_manifest=(bsp.manifest_kind != "repo-xml"),
    )
    write_yaml(opts)
    log.step_ok("gen_kas", yaml=str(output))
    artifact = f"{cfg.resolved_tmpdir}/deploy/images/{cfg.machine}/{cfg.image}-{cfg.machine}.wic"
    # stderr, not stdout: this is a human-facing announcement, and stdout is
    # reserved for machine-readable payloads a caller can pipe. See
    # ``commands/_app.py`` for the full rationale.
    sys.stderr.write(f"INFO     artifact: {artifact}\n")
    sys.stderr.flush()


@dataclass(slots=True)
class KasBuildContext:
    """Bundles the four per-call parameters shared by every kas step function."""

    cfg: BuildConfig
    log: RunLogger
    kas_yaml: Path
    overlay_source: Path
    keep_going: bool = False
    dry_run: bool = False
    # kas target override (kas build --target <TARGET>); None builds the YAML's
    # own target. Must land before any `-- <bitbake-args>` separator in the argv.
    target: str | None = None
    # Human-output mode for the live build display. RICH drives the Rich Live;
    # PLAIN swaps in a line-oriented, ANSI-free frame controller (set by build.py).
    output_mode: OutputMode = OutputMode.RICH
    # User-supplied overlays from colon syntax (machine.yml:extra.yml:...).
    # Materialized and appended after the bakar tuning overlay in the kas arg.
    extra_overlays: list[Path] = field(default_factory=list)


def _build_kas_arg(
    cfg: BuildConfig,
    kas_yaml: Path,
    overlay_source: Path,
    extra_overlays: list[Path] | None = None,
) -> str:
    """Resolve the kas YAML + overlay colon-arg, handling the meta-avocado wrapper path."""
    # The cache-classify overlay is the one unconditional entry in
    # _tuning_extra_overlays - every build references it, so materialize it
    # unconditionally too, unlike the two gated layers below.
    materialize_cache_classify_layer(cfg)
    # The sccache overlay references the meta-bakar-sccache layer by a relative
    # repos path; materialize it under .bakar/ so kas can resolve and inherit it.
    if cfg.use_sccache_dist:
        materialize_sccache_layer(cfg)
    if cfg.host_mode:
        materialize_host_layer(cfg)
    # The mold overlay references the meta-bakar-mold layer by a relative repos
    # path; materialize it under .bakar/ so kas can resolve and inherit it.
    if cfg.mold:
        materialize_layer(cfg, _MOLD_LAYER_NAME)
    if cfg.is_meta_avocado:
        _setup_meta_avocado_build_dir(cfg)
        overlay_rel = materialize_overlay(cfg, overlay_source, is_main_overlay=True)
        extra_overlay_rels = [materialize_overlay(cfg, p) for p in extra_overlays or []]
        wrapper = _write_meta_avocado_wrapper(cfg, kas_yaml)
        dump = _run_kas_dump(cfg, wrapper, overlay_rel, extra_overlay_rels)
        return str(dump)
    kas_yaml_rel = _resolve_user_yaml(cfg, kas_yaml)
    overlay_rel = materialize_overlay(cfg, overlay_source, is_main_overlay=True)
    if extra_overlays:
        extra_rels = [materialize_overlay(cfg, p) for p in extra_overlays]
        return ":".join([str(kas_yaml_rel), str(overlay_rel), *[str(r) for r in extra_rels]])
    return f"{kas_yaml_rel}:{overlay_rel}"


def _friendly_overlay_path(path: Path, root: Path) -> str:
    """Shorten an overlay path for human-readable logging.

    Workspace-relative when under ``root`` (e.g.
    ``meta-avocado/kas/target/bringup.yml``); basename otherwise, which
    covers bakar's bundled tuning overlays living outside the workspace
    (``bakar-tuning-generic.yml``).
    """
    try:
        return str(Path(path).resolve().relative_to(root))
    except ValueError:
        return Path(path).name


def friendly_overlay_lines(overlays: list[Path], root: Path) -> str:
    """Render an overlay stack as a markup-safe vertical bullet list.

    One overlay per line, each shortened via :func:`_friendly_overlay_path`,
    because the merged chain is long and a single colon-joined line soft-wraps
    mid-path. Shared by the build-start summary and the meta-avocado branch so
    both present the same ordered list.

    The RunLogger console handler renders with ``markup=True``, so the message
    must contain no ``[`` - a ``[/path]`` substring is parsed as a closing
    markup tag and raises ``rich.errors.MarkupError``. Bullets use ``-`` and
    the message carries no brackets.
    """
    return "\n".join(f"    - {_friendly_overlay_path(p, root)}" for p in overlays)


def dry_run_preview_lines(
    cfg: BuildConfig,
    kas_yaml: Path,
    overlay_source: Path,
    extra_overlays: list[Path] | None = None,
    *,
    keep_going: bool = False,
    target: str | None = None,
) -> list[str]:
    """Return structured ``key: value`` preview lines for a dry-run invocation.

    No filesystem side effects. Callers can print the returned list directly.
    meta-avocado kas_arg requires a kas dump subprocess and shows a placeholder
    instead of a fully resolved path.
    """
    exe = "kas" if cfg.host_mode else "kas-container"
    kas_arg = _dry_run_kas_arg(cfg, kas_yaml, overlay_source, extra_overlays)
    cmd = [exe, *_ccache_args(cfg, dry_run=True), "build", kas_arg]
    if target:
        cmd += ["--target", target]
    if keep_going:
        cmd += ["--", "-k"]
    lines: list[str] = [f"command: {' '.join(cmd)}", f"overlay: {kas_arg}"]
    for key, value in _build_env(cfg, ensure_hashserv=False).items():
        if value is not None:
            lines.append(f"env.{key}: {value}")
    return lines


def _dry_run_kas_arg(
    cfg: BuildConfig,
    kas_yaml: Path,
    overlay_source: Path,
    extra_overlays: list[Path] | None = None,
) -> str:
    """Return the kas colon-arg without filesystem side effects.

    Mirrors :func:`dry_run_preview_lines`' arg assembly (``_resolve_user_yaml``
    + ``_OVERLAY_DIR_RELPATH``) rather than calling :func:`_build_kas_arg`,
    which copies the overlay into the tree and runs ``kas dump`` for
    meta-avocado. The emitted arg is byte-identical to the preview path.
    """
    if cfg.is_meta_avocado:
        return "<kas-arg: computed by kas dump at build time>"
    kas_yaml_rel = _resolve_user_yaml(cfg, kas_yaml)
    parts: list[str] = [
        f"{kas_yaml_rel}:{_OVERLAY_DIR_RELPATH / overlay_source.name}",
        *[str(_OVERLAY_DIR_RELPATH / p.name) for p in extra_overlays or []],
    ]
    return ":".join(parts)


def _shell_export_lines(cfg: BuildConfig) -> list[str]:
    """Return ``export KEY="value"`` lines for the build environment.

    Mirrors the env :func:`_build_env` hands to kas-container, with
    ``ensure_hashserv=False`` so generating the script never starts the
    persistent hashserv daemon. Each value is shell-quoted via
    :func:`shlex.quote`, which single-quotes the string so ``$`` is already
    literal and no further escaping is needed.
    """
    lines: list[str] = []
    for key, value in _build_env(cfg, ensure_hashserv=False).items():
        if value is None:
            continue
        quoted = shlex.quote(str(value))
        lines.append(f"export {key}={quoted}")
    return lines


def _sync_step_lines(cfg: BuildConfig, kas_arg: str) -> list[str]:
    """Return the family-correct sync-step command lines.

    Branches on ``cfg.bsp_family``: ``repo init`` + ``repo sync`` for nxp,
    the oe-layertool setup script for ti, and ``kas checkout`` for
    bbsetup/generic (and any other family). The commands match what a real
    sync would invoke (see :mod:`bakar.commands.sync` and
    :func:`bakar.steps.ti_layertool._build_layertool_cmd`).

    The checkout step honours ``cfg.host_mode`` the same way the build step
    does, so a script generated for the default host path stays runnable on a
    machine with no container runtime installed.
    """
    if cfg.bsp_family == "nxp":
        nproc = shlex.quote(os.environ.get("NPROC", str(os.cpu_count() or 8)))
        init = (
            f"repo init -u {shlex.quote(cfg.repo_url)} -b {shlex.quote(cfg.repo_branch)}"
            f" -m {shlex.quote(cfg.manifest)} --config-name"
        )
        sync = f"repo sync -j {nproc} --force-sync --no-clone-bundle"
        nxp_dir = cfg.workspace / "nxp"
        return [f"(cd {shlex.quote(str(nxp_dir))} && {init} && {sync})"]
    if cfg.bsp_family == "ti":
        from bakar.steps.ti_layertool import _build_layertool_cmd

        layertool = " ".join(_build_layertool_cmd(cfg))
        layertool_dir = cfg.workspace / "ti" / "oe-layertool"
        return [f"(cd {shlex.quote(str(layertool_dir))} && {layertool})"]
    exe = "kas" if cfg.host_mode else "kas-container"
    return [f"{exe} checkout {shlex.quote(kas_arg)}"]


def generate_dry_run_script(
    cfg: BuildConfig,
    kas_yaml: Path,
    overlay_source: Path,
    extra_overlays: list[Path] | None = None,
    *,
    keep_going: bool = False,
    target: str | None = None,
    generating_command: str = "bakar build --dry-run-script",
) -> str:
    """Return a runnable bash script reproducing the build invocation.

    The script starts with a ``#!/usr/bin/env bash`` shebang, ``set -euo
    pipefail``, and provenance comments (the generating command and the
    resolved ``cfg.bsp_family``). It exports the same env vars
    :func:`_build_env` produces, runs the family-correct sync step
    (``repo`` for nxp, oe-layertool for ti, ``kas-container checkout`` for
    bbsetup/generic), then the family-agnostic ``kas-container build`` step
    assembled from the same kas colon-arg the preview path shows.

    ``$`` is escaped to ``\\$`` inside emitted env values so the script
    passes ``bash -n`` and the host environment captured at generation time
    is reproduced literally rather than re-expanded at run time. No
    filesystem side effects: unlike :func:`_build_kas_arg`, the overlay is
    referenced by its destination path without copying it.

    Raises:
        ValueError: for meta-avocado workspaces, where the kas colon-arg is
            computed by ``kas dump`` at build time and cannot be represented
            as a static string without filesystem side effects.
    """
    if cfg.is_meta_avocado:
        raise ValueError(
            "bakar cannot generate a dry-run script for meta-avocado workspaces: "
            "the kas colon-arg is computed by 'kas dump' at build time and cannot "
            "be captured statically. Use 'bakar build --dry-run' for a preview instead."
        )
    exe = "kas" if cfg.host_mode else "kas-container"
    kas_arg = _dry_run_kas_arg(cfg, kas_yaml, overlay_source, extra_overlays)
    build_cmd = [exe, *_ccache_args(cfg, dry_run=True), "build", kas_arg]
    if target:
        build_cmd += ["--target", target]
    if keep_going:
        build_cmd += ["--", "-k"]
    build_line = " ".join(shlex.quote(part) if " " in part else part for part in build_cmd)

    lines: list[str] = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f"# Generated by: {generating_command}",
        f"# bsp_family: {cfg.bsp_family}",
        "",
        "# Environment",
        *_shell_export_lines(cfg),
        "",
        "# Sync step",
        *_sync_step_lines(cfg, kas_arg),
        "",
        "# Build step",
        build_line,
        "",
    ]
    return "\n".join(lines)


def run_build(ctx: KasBuildContext, *, extra_overlays: list[Path] | None = None, show_layers: bool = False) -> int:
    """Run `kas build <kas_yaml>:<overlay>` with the measurement harness.

    The executable is `kas` on the host (the default) or `kas-container`
    when the container path is opted into; everything below that choice -
    overlays, PTY, UI, telemetry - is identical in both modes.

    Returns the build exit code. Does not raise - caller decides how to
    react to a nonzero status.

    ``overlay_source`` is the absolute path to the static
    overlay; this function copies it into ``<bsp_root>/.bakar/overlays/``
    so it is reachable from inside the container.

    ``extra_overlays`` are additional kas YAML overlays to layer on top
    (colon-syntax: ``bakar build main.yml:extra.yml``). Each is materialized
    into ``.bakar/overlays/`` alongside the main tuning overlay.
    """
    cfg, log, kas_yaml, overlay_source = ctx.cfg, ctx.log, ctx.kas_yaml, ctx.overlay_source

    if ctx.dry_run:
        for line in dry_run_preview_lines(
            cfg, kas_yaml, overlay_source, extra_overlays, keep_going=ctx.keep_going, target=ctx.target
        ):
            # Preview prose is for a human, so it goes to the diagnostic stream
            # with every other human-facing line (see ``commands/_app.py``);
            # ``bakar build --dry-run > payload`` leaves stdout empty.
            print(line, file=sys.stderr)
        log.step_skip("kas_build", reason="dry-run")
        return 0

    lock_outcome = clear_stale_bitbake_locks(cfg)
    for removed_path in lock_outcome.removed:
        log.warn(f"removed stale bitbake lock: {removed_path} (owning process was gone)")
    if lock_outcome.refusal is not None:
        log.step_fail("kas_build", reason=_lock_refusal_message(lock_outcome.refusal))
        return 1

    build_stop.check_unclean_stop(cfg.bsp_root, log.console)

    if cfg.sstate_mirrors_source == "seed":
        # Announced because of the size of the effect, not for completeness. A
        # native seed measured 65% of a cold build's wall-clock here, so a run
        # that quietly picked one up is not comparable with the run before it -
        # and this project lost a benchmark baseline to exactly that, when a
        # seed appeared mid-campaign and no run's output recorded which side of
        # it that run was on. An explicitly configured SSTATE_MIRRORS is not
        # announced: someone typed it, so nobody is surprised by it.
        log.info(f"sstate: consuming the native seed for this release ({cfg.sstate_mirrors})")

    log.step_start(
        "kas_build",
        yaml=str(kas_yaml),
        overlay=str(overlay_source),
        extra_overlays=[str(p) for p in (extra_overlays or [])],
    )
    cfg.measurements_dir.mkdir(parents=True, exist_ok=True)
    kas_arg = _build_kas_arg(cfg, kas_yaml, overlay_source, extra_overlays)
    if cfg.is_meta_avocado:
        n_overlays = len([kas_yaml, overlay_source, *(extra_overlays or [])])
        log.info(f"kas dump: flattened {n_overlays} overlays -> {_friendly_overlay_path(Path(kas_arg), cfg.workspace)}")

    stop_event = threading.Event()

    # PSI auto-calibration: sample host /proc/pressure peaks during the build so
    # the recommended pressure_max_* can be written afterwards.
    psi_peaks: dict[str, float] = {}
    # Raw time-series samples for post-hoc reporting (bakar insights --pressure),
    # persisted as a sibling file (RunLogger.psi_samples_path) - not folded into
    # the normalized bitbake-events.json artifact. Kept separate from psi_peaks,
    # which only tracks the per-dim max for auto-calibration.
    psi_samples: list[dict[str, Any]] = []
    psi_sampler: threading.Thread | None = None
    if cfg.psi_autocalibrate and read_psi_avg10("cpu") is not None:
        psi_peaks = dict.fromkeys(PSI_DIMS, 0.0)

        def psi_loop() -> None:  # pragma: no cover
            while not stop_event.wait(timeout=5):
                sample: dict[str, Any] = {"ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
                for dim in PSI_DIMS:
                    value = read_psi_avg10(dim)
                    sample[dim] = value
                    if value is not None and value > psi_peaks[dim]:
                        psi_peaks[dim] = value
                psi_samples.append(sample)

        psi_sampler = threading.Thread(target=psi_loop, daemon=True)  # pragma: no cover
        psi_sampler.start()

    # Host-side disk-usage samples for the run's build directory, persisted as
    # a sibling file (RunLogger.disk_samples_path) - see bakar.insights_disk.
    # Sampled on the same 5s cadence as psi_loop above so the two host-side
    # samplers share one cadence rather than inventing an independent one.
    disk_samples: list[dict[str, Any]] = []
    disk_sample_dir = cfg.bsp_root / "build"

    def disk_loop() -> None:  # pragma: no cover
        while not stop_event.wait(timeout=5):
            try:
                used_bytes = shutil.disk_usage(disk_sample_dir).used
            except OSError:
                continue
            disk_samples.append({"time": time.time(), "used_bytes": used_bytes})

    disk_sampler = threading.Thread(target=disk_loop, daemon=True)  # pragma: no cover
    disk_sampler.start()

    cmd: list[str] = []
    exe = "kas" if cfg.host_mode else "kas-container"
    ccache = _ccache_args(cfg, eventlog_path=_container_eventlog_path(cfg, log), run_id=log.run_id)
    cmd += [exe, *ccache, "build", kas_arg]
    if ctx.target:
        cmd += ["--target", ctx.target]
    if ctx.keep_going:
        cmd += ["--", "-k"]

    # Wrap in a transient systemd scope (default on) so the build survives
    # session teardown and runs under a cgroup memory ceiling; no-op when
    # disabled or systemd-run is unavailable. Must stay after the full kas
    # command is assembled and before the launch so proc.pid leads the scoped
    # build's process group.
    cmd = build_scope.wrap_build_command(cmd, cfg, log, unit_suffix="build")

    log.info(f"exec: {' '.join(cmd)}")
    # Baselines are scoped per (workspace, machine, mode): a different
    # project's builds must not train the stuck-task thresholds this one reads.
    timings_path = task_timings.timings_path_for(cfg.bsp_root, cfg.machine, host_mode=cfg.host_mode)
    # ``ui`` is created before the try so the finally block can always read
    # its warn/error counts even if _run_pty_with_ui raises before returning.
    ui = BuildUIState(
        start_monotonic=log.start_monotonic,
        logfile_translator=(None if cfg.host_mode else lambda p: translate_container_path(p, cfg.bsp_root)),
        timings_path=timings_path,
        show_baseline_drift=cfg.show_baseline_drift,
    )
    terminated = False
    rc: int | None = None
    stall_tasks: list[str] | None = None
    outcome: _PtyOutcome | None = None
    try:
        try:
            with lock_owner_marker(cfg, log):
                outcome = _run_pty_with_ui(
                    _PtyCtx(
                        cmd=cmd,
                        cfg=cfg,
                        log=log,
                        ui=ui,
                        stop_event=stop_event,
                        show_layers=show_layers,
                        output_mode=ctx.output_mode,
                        scope_unit=build_scope.unit_from_command(cmd),
                    )
                )
        except LockHeldByPeerError as exc:
            rc = 1
            log.step_fail(
                "kas_build",
                reason=f"bitbake lock claimed by peer host {escape(exc.host)} during acquire; aborting before launch",
            )
            terminated = True
            return rc
        rc, stall_tasks = outcome.rc, outcome.stall_tasks
        if rc == 0:
            deploy = cfg.resolved_tmpdir / "deploy" / "images" / cfg.machine
            log.step_ok("kas_build", deploy_dir=str(deploy), exit_code=rc)
            _autocalibrate_psi(cfg, psi_peaks, log)
        else:
            log.step_fail(
                "kas_build",
                reason=_build_fail_reason(rc, stall_tasks),
                exit_code=rc,
                kas_log=str(log.kas_log_path),
            )
            write_error_report(log.run_dir, cfg, rc)
        # step_ok/step_fail above is the true terminal event for kas_build.
        # Mark terminated before the persistence tail so an exception escaping
        # copy_oe_eventlog_to_run_dir/persist_* cannot make the finally block
        # emit a duplicate terminal step event.
        terminated = True
        # Capture the dependency graph for a build that succeeded, before the
        # persistence tail. Only on rc == 0: a failed build's graph describes
        # what was attempted rather than what ran, and the tree it would be read
        # against may be inconsistent.
        #
        # The capture waits for the build's own cooker to go idle first. It has
        # to: at this point that cooker still holds the lock WITH activity, and
        # clear_stale_bitbake_locks refuses on exactly that - which is what the
        # first real build hit, every time. Never raises, never changes rc.
        if rc == 0:
            _capture_dependency_graph(ctx, log)
        # Normalize the raw bitbake event log into bitbake-events.json for both
        # outcomes. Best-effort: a no-op when bitbake wrote no event log.
        # Belt-and-braces alongside the RunLogger-side never-raises fix (task
        # 1.1): a failure here must not crash the CLI after a completed build.
        persist_run_artifacts(cfg, log, timings_path=timings_path)
    finally:
        warn = ui.warn_count
        err = ui.error_count
        w_label = "warning" if warn == 1 else "warnings"
        e_label = "error" if err == 1 else "errors"
        log.console.print(f"{warn} {w_label}, {err} {e_label}")
        log.console.print(f"[dim]hint: bakar log --run {log.run_dir.name} to follow the full build log[/]")
        # Build-end cache-usage summary, printed at the post-block site (after
        # the live frame closed and its heartbeat joined) so it cannot interleave
        # with a frame. Emits nothing when no cache backend was active.
        if outcome is not None:
            _print_cache_summary(log, outcome.cache_backend, outcome.cache_doc, ctx.output_mode)
        if not terminated:
            # Wrapper crashed before the normal step_ok/step_fail path.  Emit
            # a terminal event anyway so events.jsonl never dead-ends at
            # step_start and `bakar triage` has something to find.
            if rc == 0:
                deploy = cfg.resolved_tmpdir / "deploy" / "images" / cfg.machine
                log.step_ok("kas_build", deploy_dir=str(deploy), exit_code=rc)
            else:
                _report_opaque_failure(log, ui)
                log.step_fail(
                    "kas_build",
                    reason=_build_fail_reason(rc, stall_tasks),
                    exit_code=rc if rc is not None else -1,
                    kas_log=str(log.kas_log_path),
                )
                write_error_report(log.run_dir, cfg, rc if rc is not None else -1)
        stop_event.set()
        if psi_sampler is not None:
            psi_sampler.join(timeout=5)
        log.persist_psi_samples(psi_samples)
        disk_sampler.join(timeout=5)
        log.persist_disk_samples(disk_samples)
    return rc if rc is not None else -1


# How many trailing kas.log lines to surface for an otherwise-silent failure.
_OPAQUE_FAILURE_TAIL_LINES = 15


def _report_opaque_failure(log: RunLogger, ui: BuildUIState) -> None:
    """Print the tail of ``kas.log`` when a failure produced no on-screen diagnosis.

    A launch-time failure - a transient-scope collision, a missing
    ``kas``/``kas-container``, a kas config error - kills the child before
    bitbake emits a single event, so the live UI closes on ``0 warnings, 0
    errors`` and the step_fail carries only ``exit_code=1``. The real message
    lands in ``kas.log``, which a user has no reason to open. Surface its tail
    so the cause is on screen instead.

    No-op when the UI already reported a genuine bitbake failure (a task failure
    or any counted error), so a normal recipe failure is not followed by a
    redundant log dump. Best-effort: a missing or unreadable log stays silent.
    """
    if ui.had_task_failures or ui.error_count:
        return
    try:
        content = log.kas_log_path.read_text(errors="replace")
    except OSError:
        return
    tail = [line for line in content.splitlines() if line.strip()][-_OPAQUE_FAILURE_TAIL_LINES:]
    if not tail:
        return
    log.console.print(f"no bitbake output; last lines of {log.kas_log_path}:")
    for line in tail:
        # markup=False/highlight=False: log text may contain '[' that Rich would
        # otherwise parse as markup and raise MarkupError on.
        log.console.print(f"  {line}", markup=False, highlight=False)


def run_shell_live(ctx: KasBuildContext, command: str) -> int:
    """Run ``kas shell -c <command>`` with the live knotty UI.

    Sister to :func:`run_shell_capture`, but instead of capturing output to
    a file it pumps the child's PTY through :func:`_run_pty_with_ui` so the
    user sees knotty's live progress bar. Used for non-interactive
    ``bakar bitbake`` invocations (anything that is not ``devshell`` or
    ``listtasks``). Runs ``kas`` on the host by default, ``kas-container``
    when the container path is opted into; returns the child's exit code.
    """
    cfg, log, kas_yaml, overlay_source = ctx.cfg, ctx.log, ctx.kas_yaml, ctx.overlay_source
    log.step_start("kas_shell_live", command=command, host_mode=cfg.host_mode)

    lock_outcome = clear_stale_bitbake_locks(cfg)
    for removed_path in lock_outcome.removed:
        log.warn(f"removed stale bitbake lock: {removed_path} (owning process was gone)")
    if lock_outcome.refusal is not None:
        log.step_fail("kas_shell_live", reason=_lock_refusal_message(lock_outcome.refusal))
        return 1

    kas_arg = _build_kas_arg(cfg, kas_yaml, overlay_source, ctx.extra_overlays)
    exe = "kas" if cfg.host_mode else "kas-container"
    cmd = [exe, *_ccache_args(cfg, eventlog_path=_container_eventlog_path(cfg, log)), "shell", kas_arg, "-c", command]
    # A live `bakar bitbake` is a real build; scope it like run_build (no-op when
    # disabled or systemd-run is unavailable).
    cmd = build_scope.wrap_build_command(cmd, cfg, log, unit_suffix="bitbake")

    ui = BuildUIState(
        start_monotonic=log.start_monotonic,
        logfile_translator=(None if cfg.host_mode else lambda p: translate_container_path(p, cfg.bsp_root)),
        timings_path=task_timings.timings_path_for(cfg.bsp_root, cfg.machine, host_mode=cfg.host_mode),
        show_baseline_drift=cfg.show_baseline_drift,
    )
    stop_event = threading.Event()

    # Mirror run_build's terminal-event guarantee: if _run_pty_with_ui raises
    # before returning (e.g. kas-container missing -> FileNotFoundError), the
    # finally still sets stop_event (stopping the heartbeat thread), prints the
    # tally, and emits a step_fail so events.jsonl never dead-ends at step_start
    # and bakar triage has a terminal event to find.
    rc: int | None = None
    completed = False
    terminated = False
    outcome: _PtyOutcome | None = None
    try:
        try:
            with lock_owner_marker(cfg, log):
                outcome = _run_pty_with_ui(
                    _PtyCtx(
                        cmd=cmd,
                        cfg=cfg,
                        log=log,
                        ui=ui,
                        stop_event=stop_event,
                        output_mode=ctx.output_mode,
                        scope_unit=build_scope.unit_from_command(cmd),
                    )
                )
        except LockHeldByPeerError as exc:
            rc = 1
            log.step_fail(
                "kas_shell_live",
                reason=f"bitbake lock claimed by peer host {escape(exc.host)} during acquire; aborting before launch",
            )
            terminated = True
            return rc
        rc = outcome.rc
        completed = True
    finally:
        stop_event.set()
        warn = ui.warn_count
        err = ui.error_count
        w_label = "warning" if warn == 1 else "warnings"
        e_label = "error" if err == 1 else "errors"
        log.console.print(f"{warn} {w_label}, {err} {e_label}")
        # Build-end cache summary at the post-block site (see run_build).
        if outcome is not None:
            _print_cache_summary(log, outcome.cache_backend, outcome.cache_doc, ctx.output_mode)
        actual_rc = rc if rc is not None else -1
        if not terminated:
            if completed and actual_rc == 0:
                log.step_ok("kas_shell_live", exit_code=actual_rc)
            else:
                _report_opaque_failure(log, ui)
                log.step_fail(
                    "kas_shell_live",
                    reason=f"exit_code={actual_rc}" if completed else "wrapper-crash",
                    exit_code=actual_rc,
                    kas_log=str(log.kas_log_path),
                )
    return actual_rc


def _autocalibrate_psi(
    cfg: BuildConfig,
    peaks: dict[str, float],
    log: RunLogger,
    config_path: Path | None = None,
) -> dict[str, int]:
    """Write PSI-calibrated pressure_max_* after a successful build and report it.

    No-op (returns {}) when auto-calibration is disabled or no peaks were
    sampled. Returns the dict of values written so callers/tests can assert.
    """
    if not cfg.psi_autocalibrate or not peaks:
        return {}
    current: dict[str, float | None] = {
        "cpu": cfg.pressure_max_cpu,
        "io": cfg.pressure_max_io,
        "memory": cfg.pressure_max_memory,
    }
    changes = apply_autocalibration(current, peaks, config_path)
    if changes:
        summary = ", ".join(f"pressure_max_{dim}={changes[dim]}" for dim in PSI_DIMS if dim in changes)
        log.info(f"PSI auto-calibrated: {summary} (written to ~/.config/bakar/config.toml)")
    else:
        log.info("PSI auto-calibrate: thresholds already optimal, no change")
    return changes


class BuildtoolsMissingError(RuntimeError):
    """A host build was requested but no pinned buildtools-extended toolchain is present.

    Raised before bitbake is invoked so the build fails loudly instead of
    silently falling back to the system ``/usr/bin/gcc``. The message names the
    missing toolchain and how to point bakar at it.
    """


class BitbakeBinMissingError(RuntimeError):
    """A host build's derived bitbake ``bin`` directory does not exist on disk.

    Raised before bitbake is invoked so a wrong :attr:`BuildConfig.bitbake_bin_path`
    derivation fails loudly. Without this dir on the launch PATH, kas's
    ``find_program(ctx.environ['PATH'], 'bitbake')`` cannot locate bitbake and
    the launch fails with a confusing downstream error.
    """


def _capture_sourced_env(env_script: Path) -> dict[str, str]:
    """Source ``env_script`` in a clean shell and return the resulting environment.

    The buildtools-extended ``environment-setup-*`` script mutates PATH,
    OECORE_NATIVE_SYSROOT, CC/CXX and friends. Sourcing it in a subshell and
    dumping ``env`` is the only faithful way to capture those edits without
    reimplementing the script.
    """
    result = subprocess.run(
        ["bash", "-c", f". {shlex.quote(str(env_script))} && env -0"],
        capture_output=True,
        text=True,
        check=True,
    )
    captured: dict[str, str] = {}
    for entry in result.stdout.split("\0"):
        key, sep, value = entry.partition("=")
        if sep:
            captured[key] = value
    return captured


def _provision_buildtools(cfg: BuildConfig, passthrough: dict[str, str]) -> None:
    """Stage the pinned buildtools-extended toolchain into the host build env.

    Host builds must run against the pinned ``buildtools-extended`` gcc, never
    the rolling Arch system gcc. Detect the toolchain via
    :func:`bakar.diagnostics.detect_buildtools`; if it is absent, raise
    :class:`BuildtoolsMissingError` naming it instead of letting bitbake fall
    back to ``/usr/bin/gcc``. When present via the env-script path, source the
    script and merge its PATH/sysroot/compiler exports into ``passthrough``;
    when already sourced, the process env already carries them.

    No-op outside host mode - container builds get their toolchain from the kas
    image.
    """
    if not cfg.host_mode:
        return
    release_key = resolve_oe_core_release_key(cfg.workspace)
    toolchain = detect_buildtools(release_key=release_key)
    if not toolchain.present:
        raise BuildtoolsMissingError(
            "host build requires the pinned buildtools-extended toolchain, but it "
            f"was not found: {toolchain.detail}. Install Yocto's "
            "buildtools-extended-tarball and either source its environment-setup "
            f"script or set {BUILDTOOLS_DIR_ENV} to its install directory. Refusing "
            "to fall back to the system /usr/bin/gcc."
        )
    if toolchain.env_script is not None:
        sourced = _capture_sourced_env(toolchain.env_script)
        # Carry the toolchain's PATH (pinned gcc first) and every OE/SDK var the
        # script exports so the host bitbake sees the pinned compiler.
        if "PATH" in sourced:
            passthrough["PATH"] = sourced["PATH"]
        passthrough.update(
            {
                key: value
                for key, value in sourced.items()
                if key.startswith(("OECORE_", "SDKTARGETSYSROOT")) or key in {"CC", "CXX", "CPP", "AR", "LD", "CFLAGS"}
            }
        )
        # The env-setup script prepends the SDK bin dir (<sysroot>/usr/bin, where
        # python3 and gcc live) as the first PATH entry. OECORE_NATIVE_SYSROOT is
        # NOT reliably exported by the buildtools-extended script, so take the bin
        # dir from PATH directly rather than from that var.
        sdk_bin = sourced["PATH"].split(os.pathsep)[0] if sourced.get("PATH") else None
    else:
        sdk_bin = str(toolchain.sysroot / "usr" / "bin") if toolchain.sysroot is not None else None
    # Host bitbake must run under the SDK's own python, which ships bitbake's
    # runtime deps (e.g. websockets for the hashserv ws client) and matches the
    # release the metadata targets. Its bin is already first on PATH via the
    # sourced toolchain PATH above, so the bitbake shebang resolves to it; record
    # BB_PYTHON3 too for any sub-invocation that honors it.
    if sdk_bin:
        passthrough["BB_PYTHON3"] = str(Path(sdk_bin) / "python3")
        # That python has its cert path baked in as the SDK's own build path
        # (/usr/local/oe-sdk-hardcoded-buildpath/...), which does not survive
        # relocation, so ssl.get_default_verify_paths() reports cafile=None and
        # every HTTPS verify under it fails. The tarball ships a usable bundle
        # and nothing points at it: cve-update-nvd2-native's do_fetch burns its
        # five retries on CERTIFICATE_VERIFY_FAILED and leaves whatever CVE
        # database it already had, which reads as a fresh scan against months-old
        # advisories. An explicit value wins - a corporate trust store is not in
        # the SDK's bundle, and overriding it would break the fetch it fixes.
        if "SSL_CERT_FILE" not in passthrough:
            ca_bundle = Path(sdk_bin).parent.parent / "etc" / "ssl" / "certs" / "ca-certificates.crt"
            # Only when it is really there: openssl treats an unreadable
            # SSL_CERT_FILE as an empty trust store, so a wrong path is worse
            # than none.
            if ca_bundle.is_file():
                passthrough["SSL_CERT_FILE"] = str(ca_bundle)


def _apply_host_mode_env(
    cfg: BuildConfig,
    python_executable: Path | None,
    passthrough: dict[str, str],
    *,
    provision_buildtools: bool = True,
) -> None:
    """Inject host-mode Python interpreter settings into the env dict (mutates in place).

    ``provision_buildtools`` gates the buildtools-extended staging side effect
    (and its loud failure when absent) so dry-run/script-gen env rendering never
    sources the toolchain or aborts on a missing one - it mirrors the
    ``ensure_hashserv`` guard the hashserv/sccache side effects use.
    """
    if cfg.host_mode:
        # Stage the pinned buildtools-extended toolchain first (raises loudly
        # when absent). _provision_buildtools sets BB_PYTHON3 to the SDK python,
        # which ships bitbake's runtime deps; do not shadow it with bakar's venv.
        if provision_buildtools:
            _provision_buildtools(cfg, passthrough)
            # OE's HOSTTOOLS resolves each tool against BB_ORIGENV's PATH, and kas
            # launches bitbake via find_program(ctx.environ['PATH'], 'bitbake'), so
            # the bundled bitbake bin must be on the launch PATH. Fail loud on a
            # wrong derivation instead of producing a broken launch. Gated on
            # provision_buildtools so dry-run/script-gen rendering never aborts.
            if not cfg.bitbake_bin_path.is_dir():
                raise BitbakeBinMissingError(
                    "host build requires the bundled bitbake bin directory on the "
                    f"launch PATH, but it does not exist: {cfg.bitbake_bin_path}. "
                    "Check the workspace layout (bitbake must be provisioned before "
                    "the build)."
                )
        # Prepend the bitbake bin after the buildtools toolbin so kas's launch
        # can find bitbake; the SDK python (set by _provision_buildtools, its bin
        # already first on PATH) stays the host interpreter by default.
        passthrough["PATH"] = str(cfg.bitbake_bin_path) + os.pathsep + passthrough.get("PATH", "")
        if python_executable is not None:
            # Explicit interpreter override (e.g. stress-parse's obmalloc-patched
            # CPython) wins over the SDK python and goes first on PATH.
            py_path = python_executable.resolve()
            passthrough["BB_PYTHON3"] = str(py_path)
            passthrough["PATH"] = str(py_path.parent) + os.pathsep + passthrough.get("PATH", "")
        elif not provision_buildtools:
            # Dry-run/script-gen skipped provisioning, so no SDK is on PATH; fall
            # back to bakar's interpreter for env rendering only. Real builds skip
            # this branch and keep the SDK python first on PATH.
            passthrough["BB_PYTHON3"] = sys.executable
            passthrough["PATH"] = sysconfig.get_path("scripts") + os.pathsep + passthrough.get("PATH", "")


def _build_env(
    cfg: BuildConfig,
    python_executable: Path | None = None,
    *,
    ensure_hashserv: bool = True,
    eventlog_path: str | None = None,
) -> dict[str, str]:
    """Return the environment to hand to kas-container.

    Keeps SSTATE_DIR, DL_DIR, NPROC, and KAS_* from the caller's shell
    (these are the knobs kas-container actually reads) plus a stable
    PATH and HOME so the subprocess behaves the same as an interactive
    shell run. NPROC defaults to os.cpu_count() when not set by the
    caller, so BB_NUMBER_THREADS and PARALLEL_MAKE in the overlay pick
    up the actual machine core count instead of the hardcoded fallback.

    KAS_WORK_DIR is forced to the BSP-specific subtree
    (``cfg.bsp_root`` = ``workspace/<bsp_family>``) so kas-container
    bind-mounts that subtree as ``/work`` inside the container. With
    this setting, in-container paths (``/work/sources/...``,
    ``/work/forks/...``, ``/work/build/...``, ``/work/ccache``) are
    byte-identical between NXP and TI, so neither the kas template nor
    any recipe needs to know which BSP it is in.

    The ccache bind-mount (``/work/ccache``) is injected at the call
    site via ``_ccache_args()`` as a ``--runtime-args`` CLI flag, not
    here.  ``kas-container`` unconditionally overwrites ``KAS_RUNTIME_ARGS``
    before its option-parsing loop, making env-var injection unreliable.

    ``python_executable`` overrides the host-mode BB_PYTHON3 and PATH
    interpreter. Lets stress-parse point bitbake at a
    locally-built CPython (e.g. one with the obmalloc atfork patch)
    without reinstalling bakar under it. When None, host mode defaults
    to ``sys.executable``.
    """
    passthrough = {
        k: v
        for k, v in os.environ.items()
        # SSL_CERT_FILE so an operator who names a trust store keeps it: the
        # buildtools default below is a fallback for the relocated SDK's missing
        # one, not a policy about which CAs to trust.
        if k.startswith(
            ("KAS_", "BB_", "SSTATE_", "DL_", "NPROC", "PATH", "HOME", "USER", "SDKMACHINE", "SSL_CERT_FILE")
        )
    }
    # PATH might not have leaked via the startswith rule if the shell
    # exported it without prefix; ensure it is present.
    passthrough.setdefault("PATH", os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
    passthrough.setdefault("HOME", os.environ.get("HOME", "/tmp"))
    # NPROC precedence: a non-empty live env var wins, then cfg.nproc, then the
    # cpu_count auto-detect. An exported-but-empty NPROC ("") is treated as unset
    # so the overlays never expand BB_NUMBER_THREADS to "" or PARALLEL_MAKE to
    # "-j ". This truthiness check matches check_nproc, so the doctor and the
    # build agree on the empty case.
    if not passthrough.get("NPROC"):
        passthrough["NPROC"] = str(cfg.nproc if cfg.nproc is not None else (os.cpu_count() or 16))
    # Container image: config value is a fallback; a live env var wins via setdefault.
    # This is what actually passes the image to kas-container; resolve() already
    # handles the env-beats-config precedence when building cfg, but kas-container
    # reads KAS_CONTAINER_IMAGE directly from its own environment, so we must
    # re-export it here. setdefault keeps a caller-supplied env var in place.
    if not cfg.host_mode:
        passthrough.setdefault("KAS_CONTAINER_IMAGE", cfg.kas_container_image)
    # Cache dirs: config value is a fallback; a live env var wins via setdefault.
    if cfg.dl_dir is not None:
        passthrough.setdefault("DL_DIR", cfg.dl_dir)
    if cfg.sstate_dir is not None:
        passthrough.setdefault("SSTATE_DIR", cfg.sstate_dir)
    if cfg.sstate_mirrors is not None:
        passthrough.setdefault("SSTATE_MIRRORS", cfg.sstate_mirrors)
    if cfg.sstate_mirror_url is not None:
        passthrough.setdefault("BAKAR_SSTATE_MIRROR_URL", cfg.sstate_mirror_url)
    # sccache-dist scheduler: exported only when distributed-compile is enabled,
    # so a disabled build is byte-for-byte unchanged. The sccache overlay reads
    # this var (like BAKAR_SSTATE_MIRROR_URL above). In container mode localhost
    # is the container itself, so rewrite it to the host-gateway alias exactly as
    # the hashserv URL is rewritten below; the binary/config mounts live in
    # _ccache_args.
    if cfg.use_sccache_dist and cfg.sccache_scheduler_url is not None:
        if cfg.host_mode:
            passthrough.setdefault("BAKAR_SCCACHE_SCHEDULER_URL", cfg.sccache_scheduler_url)
        else:
            passthrough.setdefault(
                "BAKAR_SCCACHE_SCHEDULER_URL",
                cfg.sccache_scheduler_url.replace("localhost", "host.docker.internal"),
            )
    # Scheduler and PSI thresholds:
    # only emit when set (empty dimension is disabled in the overlay via the
    # os.environ.get(..., '') expression, so omitting the key is equivalent).
    # Config stores avg10 percent (0-100, the unit psi.py calibrates from);
    # bitbake's exceeds_max_pressure() compares against the delta of the
    # total= stall counter in microseconds per second (0-1,000,000), so
    # convert percent -> us/s here at the boundary. Without the conversion
    # a 20% threshold lands as 20 us/s (0.002% stall) and throttles task
    # launch for nearly the whole build.
    if cfg.scheduler is not None:
        passthrough["BB_SCHEDULER"] = cfg.scheduler
    if cfg.pressure_max_cpu is not None:
        passthrough["BB_PRESSURE_MAX_CPU"] = str(int(cfg.pressure_max_cpu * 10_000))
    if cfg.pressure_max_io is not None:
        passthrough["BB_PRESSURE_MAX_IO"] = str(int(cfg.pressure_max_io * 10_000))
    if cfg.pressure_max_memory is not None:
        passthrough["BB_PRESSURE_MAX_MEMORY"] = str(int(cfg.pressure_max_memory * 10_000))
    # Decoupled parallelism: an explicit cfg override always wins. When a field
    # is None, derive it from every perf input bakar can observe (local cpus,
    # host RAM, the active launcher, and the live cluster cpu count under
    # sccache-dist) via the shared _derive_parallelism_plan helper - the same
    # path materialize_overlay uses for the container literal, so host and
    # container modes derive identically. ``ensure_hashserv`` doubles as the
    # cluster-probe side-effect guard so dry-run/script-gen never reach the
    # network. parallel_make sizes compile -j to the cluster; bb_number_threads
    # sizes recipe concurrency to local RAM.
    if cfg.parallel_make is None or cfg.bb_number_threads is None:
        plan = _derive_parallelism_plan(cfg, probe_cluster_ok=ensure_hashserv)
    if cfg.parallel_make is not None:
        passthrough["BAKAR_PARALLEL_MAKE"] = str(cfg.parallel_make)
    else:
        passthrough["BAKAR_PARALLEL_MAKE"] = str(plan.parallel_make)
    if cfg.bb_number_threads is not None:
        passthrough["BAKAR_BB_NUMBER_THREADS"] = str(cfg.bb_number_threads)
    else:
        passthrough["BAKAR_BB_NUMBER_THREADS"] = str(plan.bb_number_threads)
    # Persistent hashserv: when enabled, ensure the workspace-scoped
    # daemon is running and rewrite the URL for container reachability.
    # The overlay's BB_HASHSERVE = ${@os.environ.get('BB_HASHSERVE', 'auto')}
    # falls through to "auto" when this block omits the key.
    if cfg.bb_hashserve:
        # Central cross-node tier: point at the shared Rust/PostgreSQL hashserv
        # (CentralTierAction persisted this host:port endpoint) instead of the
        # per-workspace bitbake daemon. In container mode the cluster IP is
        # reachable directly, so no host.docker.internal rewrite is needed.
        passthrough["BB_HASHSERVE"] = cfg.bb_hashserve
    elif cfg.use_hashequiv and ensure_hashserv:
        url = hashserv.ensure_running(
            cfg.hashserv_state_key,
            binary_root=cfg.bsp_root,
            bind_host=cfg.cluster_bind_host or "localhost",
        )
        if url is not None:
            if cfg.host_mode:
                passthrough["BB_HASHSERVE"] = url
            else:
                passthrough["BB_HASHSERVE"] = url.replace("localhost", "host.docker.internal")
    # Persistent, cluster-reachable PR service (host mode only). meta-avocado
    # sets PRSERV_HOST=localhost:0, a per-build autostart whose DB sits under the
    # volatile PERSISTENT_DIR (TMPDIR/cache); a wiped build tree then resets PRs
    # to r0 while TOPDIR buildhistory keeps r0.N, failing the
    # version-going-backwards QA on do_packagedata_setscene. Start one managed
    # prserv keyed to the shared sstate and override PRSERV_HOST so PRs stay
    # monotonic across builds/TMPDIR-wipes and reach other cluster nodes via
    # cluster_bind_host. ``ensure_hashserv`` is the dry-run/script-gen guard.
    if cfg.prserv_host:
        # Central cross-node tier: the shared Rust/PostgreSQL prserv
        # (CentralTierAction persisted this endpoint). One monotonic PR DB for the
        # whole cluster, surviving TMPDIR wipes, instead of the per-workspace
        # bitbake daemon - so PRs never go backwards regardless of build tree.
        passthrough["PRSERV_HOST"] = cfg.prserv_host
    elif cfg.host_mode and ensure_hashserv:
        prserv_addr = prserv.ensure_running(
            cfg.prserv_state_key,
            binary_root=cfg.bsp_root,
            bind_host=cfg.cluster_bind_host or "localhost",
        )
        if prserv_addr is not None:
            passthrough["PRSERV_HOST"] = prserv_addr
    # Persistent sccache server: in host mode, pre-start one detached server so
    # it survives bitbake's per-task process-group teardown. Without it the
    # first task's auto-started server dies with that task, churning fallbacks
    # and poisoning the cache with truncated objects. Host mode only - in
    # container mode sccache runs inside the container. ``ensure_hashserv``
    # doubles as the side-effect guard, so dry-run/script-gen never spawn it.
    if cfg.use_sccache_dist and cfg.host_mode and ensure_hashserv:
        sccache_server.ensure_running(cfg.sccache_scheduler_url, uds_path=str(sccache_server.default_uds_path()))
    if cfg.is_meta_avocado:
        passthrough["KAS_WORK_DIR"] = str(cfg.workspace)
        passthrough["KAS_BUILD_DIR"] = str(cfg.bsp_root / "build")
    else:
        passthrough["KAS_WORK_DIR"] = str(cfg.bsp_root)

    _apply_host_mode_env(cfg, python_executable, passthrough, provision_buildtools=ensure_hashserv)
    # When the caller supplies a container-visible event-log path, point
    # bitbake at it via BB_DEFAULT_EVENTLOG (cooker.py honors this var literally
    # via setupEventLog, no datetime substitution). Omit the key when None so
    # the env-rendering-only sites and the existing _build_env test calls keep
    # producing the pre-change env.
    if eventlog_path is not None:
        passthrough["BB_DEFAULT_EVENTLOG"] = eventlog_path
    return passthrough


def _find_oe_eventlog(cfg: BuildConfig, log: RunLogger) -> Path | None:
    """Return the bitbake event log from OE-core's default location, or None.

    OE-core's bitbake.conf sets:
        BB_DEFAULT_EVENTLOG ?= "${LOG_DIR}/eventlog/${DATETIME}.json"
    The tuning overlays declare ``BB_DEFAULT_EVENTLOG: null`` in their kas
    ``env:`` section, which makes kas whitelist the var in
    BB_ENV_PASSTHROUGH_ADDITIONS so the ``docker -e`` injection reaches
    bitbake's data store and the log lands at the run-dir path the live
    tailer follows. When that chain is broken (a build without the bakar
    overlay, or an older generated YAML), bitbake falls back to the ?=
    default and writes to
    ``bsp_root/build/tmp/log/eventlog/YYYYMMDDHHMMSS.json`` instead.

    Returns the newest JSON file in that directory whose mtime is at or after
    the build start time (derived from log.run_id, which is generated at
    RunLogger construction time before any subprocess is launched). Falls back
    to run_dir.stat().st_mtime - 60 when the run_id cannot be parsed.

    Using run_id rather than run_dir.stat().st_mtime avoids a race where the
    run dir's mtime is updated by the final events.jsonl write *after* bitbake
    finishes writing its event log, making the log appear older than the
    watermark.

    run_id is ``YYYYMMDD-HHMMSS-<pid>`` (the pid suffix disambiguates two
    builds started in the same second - see RunLogger.run_id), so only the
    leading 15-char timestamp is parsed; the pid carries no timing information.
    """
    eventlog_dir = cfg.resolved_tmpdir / "log" / "eventlog"
    if not eventlog_dir.is_dir():
        return None
    try:
        watermark = datetime.strptime(log.run_id[:15], "%Y%m%d-%H%M%S").timestamp()
    except ValueError, OSError:
        watermark = log.run_dir.stat().st_mtime - 60
    entries: list[tuple[float, Path]] = []
    for p in eventlog_dir.glob("*.json"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime >= watermark:
            entries.append((mtime, p))
    if not entries:
        return None
    return max(entries)[1]


def copy_oe_eventlog_to_run_dir(cfg: BuildConfig, log: RunLogger) -> bool:
    """Copy the OE-core event log to the run dir when our expected path is absent.

    Returns True when a file was copied, False otherwise.  Callers should call
    this before ``log.persist_bitbake_events()`` so the normalizer finds the file
    at the expected path.
    """
    if log.eventlog_path.is_file():
        return False
    oe_log = _find_oe_eventlog(cfg, log)
    if oe_log is None:
        return False
    shutil.copy2(oe_log, log.eventlog_path)
    return True


def persist_run_artifacts(cfg: BuildConfig, log: RunLogger, *, timings_path: Path | None = None) -> None:
    """Copy and normalize a completed run's artifacts, tolerating any failure.

    Wraps ``copy_oe_eventlog_to_run_dir``, ``log.persist_bitbake_events()``, and
    (when ``timings_path`` is given) ``log.persist_task_timings(timings_path)``
    in one best-effort block. A completed command must not crash on persist
    failure, so any exception is caught and reported as a warning rather than
    propagated.
    """
    try:
        copy_oe_eventlog_to_run_dir(cfg, log)
        log.persist_bitbake_events()
        if timings_path is not None:
            log.persist_task_timings(timings_path)
    except Exception as exc:  # noqa: BLE001 - defense-in-depth; a completed command must not crash on persist failure
        log.console.print(f"[yellow]warning: failed to persist run artifacts: {exc}[/]")


def _container_eventlog_path(cfg: BuildConfig, log: RunLogger) -> str:
    """Return the bitbake event-log path as bitbake sees it inside the container.

    bitbake writes the event log from inside kas-container, so BB_DEFAULT_EVENTLOG
    must name a path valid in the container's filesystem. kas-container bind-mounts
    KAS_WORK_DIR as ``/work``; ``_build_env`` assigns KAS_WORK_DIR = ``cfg.workspace``
    for meta-avocado and ``cfg.bsp_root`` otherwise. The host run dir
    (``cfg.runs_dir/<run_id>`` = ``bsp_root/build/runs/<run_id>``) lives under that
    mount root, so the container path is ``/work`` + the run dir's path relative to
    the mount root + the event-log filename. In ``cfg.host_mode`` there is no
    container, so the host path is used verbatim.

    Fallback if a real build shows the file is not written at this path (the
    kas-container mount mapping differs from the above): glob
    ``bitbake_eventlog_*.json`` under the build dir filtered by an mtime watermark
    captured immediately before the bitbake invocation.
    """
    host_path = log.eventlog_path
    if cfg.host_mode:
        return str(host_path)
    mount_root = cfg.workspace if cfg.is_meta_avocado else cfg.bsp_root
    try:
        rel = host_path.relative_to(mount_root)
    except ValueError:
        # The run dir is outside the bind-mounted tree - e.g. `bakar dump`
        # and `bakar lock` use a TemporaryDirectory run dir. bitbake cannot
        # write into the container at that host path, and those callers do
        # not persist the artifact, so fall back to the host path rather
        # than crashing the command.
        return str(host_path)
    return str(Path("/work") / rel)


def _finish_step(log: RunLogger, step: str, rc: int) -> None:
    """Log a step's terminal event, deriving pass/fail from its exit code.

    Sister helper for :func:`run_shell` and :func:`run_shell_capture`, which
    both wrap a subprocess whose only signal is a return code. Centralizes the
    ``reason`` string shape and ensures the structured ``exit_code`` field is
    always attached to ``step_fail`` events, not just embedded in the reason
    string.
    """
    if rc != 0:
        log.step_fail(step, reason=f"exit_code={rc}", exit_code=rc)
    else:
        log.step_ok(step, exit_code=rc)


def run_shell(ctx: KasBuildContext, args: list[str], command: str | None = None) -> int:
    """Drop into a kas-container shell, passing through extra args.

    When ``command`` is provided, kas-container runs it non-interactively
    via ``-c <command>`` instead of opening an interactive shell. The
    overlay is layered in via the same colon-joined arg as ``run_build``.

    When ``cfg.host_mode`` is True, plain ``kas shell`` runs directly on
    the host (no kas-container wrapper, no Docker). The host must have
    the bitbake build prereqs installed (zstd, git, ...) and a
    bitbake-supported Python on PATH.
    """
    cfg, log, kas_yaml, overlay_source = ctx.cfg, ctx.log, ctx.kas_yaml, ctx.overlay_source
    log.step_start("kas_shell", command=command, host_mode=cfg.host_mode)
    kas_arg = _build_kas_arg(cfg, kas_yaml, overlay_source, ctx.extra_overlays)
    exe = "kas" if cfg.host_mode else "kas-container"
    cmd = [exe, *_ccache_args(cfg), "shell", kas_arg]
    if command is not None:
        cmd.extend(["-c", command])
    cmd.extend(args)
    proc = subprocess.Popen(
        cmd, cwd=cfg.bsp_root, env=_build_env(cfg, eventlog_path=_container_eventlog_path(cfg, log))
    )
    rc = proc.wait()
    _finish_step(log, "kas_shell", rc)
    return rc


#: Grace window for reaping a capture the escalation ladder has just killed.
#: The ladder already spent its own SIGTERM->SIGKILL wait before returning, so
#: anything still unreaped here is stuck in uninterruptible state and waiting
#: longer would only re-block the teardown the timeout exists to unblock.
_CAPTURE_REAP_TIMEOUT_S = 5.0


def run_shell_capture(
    ctx: KasBuildContext,
    command: str,
    stdout_path: Path,
    *,
    step: str = "kas_shell_capture",
    python_executable: Path | None = None,
    stderr_path: Path | None = None,
    env_overrides: dict[str, str] | None = None,
    timeout: float | None = None,
    isolate_process_group: bool = False,
) -> int:
    """Run ``kas-container shell -c <command>`` with output captured to file.

    Sister to :func:`run_shell`. Same env+cwd plumbing via
    :func:`_build_env`; the only difference is that stdout and stderr
    are merged and redirected to ``stdout_path`` instead of inheriting
    the parent terminal. Returns the kas-container exit code.

    Pass ``stderr_path`` to split the two streams instead: stdout stays
    clean in ``stdout_path`` and kas's own diagnostics (INFO progress
    chatter, error text) land in ``stderr_path``. Callers that treat the
    whole capture as a payload - ``bakar getvar`` reading
    ``bitbake-getvar --value`` - need the split; callers that scan the
    merged log for failure signatures (triage, stress-parse) must not
    pass it.

    Used by :mod:`bakar.steps.stress_parse` to capture each
    ``bitbake -p`` iteration's output to its own log file for offline
    fork-race signature scanning.

    ``python_executable`` is forwarded to :func:`_build_env` so the
    kas shell's PATH and BB_PYTHON3 point at a caller-chosen interpreter
    (obmalloc-patch validation).

    ``timeout`` bounds the wait in seconds and raises
    :exc:`subprocess.TimeoutExpired` when it fires; a Ctrl-C during the wait
    raises :exc:`KeyboardInterrupt` instead. Both escalate the child the same
    way before re-raising: in container mode via
    :func:`bakar.build_stop.escalate_container_tree` (the child is the
    host-side ``kas-container`` client, and killing only that client leaves
    the container - and the cooker inside it - running), falling back to
    :func:`bakar.build_stop.escalate_process_tree` in host mode or when no
    container resolves. It defaults to None - unbounded - because a deadline
    chosen for one caller is wrong for the other seven, several of which wrap
    a full build or a stress-parse loop. Raising rather than returning a
    sentinel keeps a timeout distinguishable from a command that merely
    exited non-zero.

    ``isolate_process_group`` puts the child in its own session, which is a
    precondition for the host-mode escalation above rather than a
    convenience: without it the child shares bakar's process group and the
    ladder refuses to signal it. It defaults to False so the callers that
    never time out keep the signal-propagation behaviour they have today - a
    Ctrl-C at the terminal reaches a shared-group child and would not reach
    an isolated one.
    """
    cfg, log, kas_yaml, overlay_source = ctx.cfg, ctx.log, ctx.kas_yaml, ctx.overlay_source
    log.step_start(step, command=command, stdout_path=str(stdout_path), host_mode=cfg.host_mode)

    lock_outcome = clear_stale_bitbake_locks(cfg)
    for removed_path in lock_outcome.removed:
        log.warn(f"removed stale bitbake lock: {removed_path} (owning process was gone)")
    if lock_outcome.refusal is not None:
        log.step_fail(step, reason=_lock_refusal_message(lock_outcome.refusal), exit_code=1)
        return 1

    kas_arg = _build_kas_arg(cfg, kas_yaml, overlay_source, ctx.extra_overlays)
    exe = "kas" if cfg.host_mode else "kas-container"
    # The run_id label is what lets a timeout's container-mode escalation find
    # THIS invocation's container - only pass it when isolate_process_group
    # requests that escalation path. Labelling every caller's container would
    # make it carry the same bakar.run_id as the build's own container, which
    # _container_id (docker/podman ps -q -f label=...) cannot tell apart: it
    # returns whichever container answers the label first.
    label_run_id = log.run_id if isolate_process_group else None
    cmd = [exe, *_ccache_args(cfg, run_id=label_run_id), "shell", kas_arg, "-c", command]
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    if stderr_path is not None:
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ExitStack() as stack:
            stack.enter_context(lock_owner_marker(cfg, log))
            fh = stack.enter_context(stdout_path.open("wb"))
            stderr_target: int | IO[bytes] = subprocess.STDOUT
            if stderr_path is not None:
                stderr_target = stack.enter_context(stderr_path.open("wb"))
            env = _build_env(
                cfg,
                python_executable=python_executable,
                eventlog_path=_container_eventlog_path(cfg, log),
            )
            # Applied here rather than inside _build_env so an override reaches
            # only the caller that asked for it. SHELL is the motivating case:
            # kas hands a -c payload to $SHELL, so a bash-only payload needs it
            # pinned - but `bakar shell` spawns $SHELL as the user's interactive
            # shell, and pinning it there would silently replace their shell.
            if env_overrides:
                env.update(env_overrides)
            proc = subprocess.Popen(
                cmd,
                cwd=cfg.bsp_root,
                env=env,
                stdout=fh,
                stderr=stderr_target,
                start_new_session=isolate_process_group,
            )
            try:
                rc = proc.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                interrupted = isinstance(exc, KeyboardInterrupt)
                if not isolate_process_group:
                    # Not isolated: this child shares bakar's own process group,
                    # so escalate_process_tree's pgid check would refuse it
                    # outright and a container-mode escalation would stop the
                    # wrong thing out from under a caller that never asked for
                    # any of this - the seven callers that never pass timeout=
                    # still raise KeyboardInterrupt here on an ordinary Ctrl-C.
                    # Propagate exactly as it did before this timeout/escalation
                    # path existed: no warn, no step_fail, no escalation attempt.
                    raise
                reason = "interrupted" if interrupted else f"no exit after {timeout:.0f}s"
                log.warn(f"{step}: {reason}; escalating the capture's process tree")
                # In container mode the child is the host-side kas-container
                # client; escalating its process tree (as the host path does)
                # stops that client but not the container still running
                # underneath it, so the bitbake cooker inside would keep the
                # lock. Resolve and stop the container itself first, and only
                # fall back to the host-side ladder when none resolves.
                if cfg.host_mode or not build_stop.escalate_container_tree(log.run_id):
                    build_stop.escalate_process_tree(proc.pid, log.run_dir)
                # Reap so the killed child does not linger as a zombie holding
                # the redirected file descriptors open.
                with suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=_CAPTURE_REAP_TIMEOUT_S)
                log.step_fail(step, reason=reason, exit_code=proc.returncode)
                raise
    except LockHeldByPeerError as exc:
        log.step_fail(
            step,
            reason=f"bitbake lock claimed by peer host {escape(exc.host)} during acquire; aborting before launch",
            exit_code=1,
        )
        return 1
    _finish_step(log, step, rc)
    return rc


def run_kas_subcommand(
    ctx: KasBuildContext,
    subcommand: str,
    extra_args: list[str],
    *,
    step: str = "kas_subcommand",
    capture_to: Path | None = None,
    timeout: float | None = None,
) -> int:
    """Run a kas subcommand (e.g. ``dump``, ``lock``) with overlay assembly.

    Sister to :func:`run_shell`/:func:`run_shell_capture`. Selects ``kas``
    vs ``kas-container`` from ``cfg.host_mode`` and layers the overlay in via
    the same colon-joined arg as :func:`run_build`. Used by ``bakar dump``
    (subcommand ``dump``) and the BYO path of ``bakar lock`` (subcommand
    ``lock``).

    ``step`` names the event-log step this call reports under - defaulting to
    the generic ``kas_subcommand`` these two commands log unchanged. The
    post-build graph capture's kas-dump target resolution passes its own
    distinct name (``graph_capture_kas_dump``) so its failures never share an
    event-log step name with a genuine ``bakar dump``/``bakar lock`` failure -
    see :mod:`bakar.triage`'s ``_POST_BUILD_STEPS``, which used to exclude the
    shared name unconditionally and could have suppressed a real failure from
    either command had one ever run against the build's own persistent
    ``RunLogger`` instead of the ephemeral one both currently use.

    When ``capture_to`` is a path, the subprocess stdout is redirected to that
    file so large ``kas dump`` output streams to disk instead of buffering in
    memory; when None, stdout inherits the parent terminal. Returns the kas
    exit code.

    ``timeout`` bounds the wait in seconds; it defaults to None (unbounded) so
    the two existing callers - ``bakar dump`` and the BYO path of ``bakar
    lock`` - keep today's behaviour. A timeout returns 124 (the shell
    convention for a timed-out command) rather than raising: this command
    never holds the bitbake lock, so unlike :func:`run_shell_capture` a hang
    here costs only a wasted wait, not a stranded lock, and the existing
    non-zero-rc handling below already gives every caller a path to treat
    "could not run this" as a plain failure. ``subprocess.run``'s own
    ``timeout`` kills the direct child, which is sufficient in host mode -
    but in container mode that direct child is the host-side kas-container
    client, and killing it does not stop the container it launched (the
    runtime does not stop a container merely because the client that started
    it exits). A caller that passes ``timeout`` gets the container labelled
    with this run's id so a timeout can resolve and stop it via
    :func:`bakar.build_stop.escalate_container_tree`, falling back to letting
    the host-side kill stand when no container resolves.
    """
    cfg, log, kas_yaml, overlay_source = ctx.cfg, ctx.log, ctx.kas_yaml, ctx.overlay_source
    log.step_start(step, subcommand=subcommand, host_mode=cfg.host_mode)
    kas_arg = _build_kas_arg(cfg, kas_yaml, overlay_source, ctx.extra_overlays)
    exe = "kas" if cfg.host_mode else "kas-container"
    # Only label the container when a timeout was actually requested - see
    # run_shell_capture's identical reasoning: an unconditional label would
    # make every caller's container carry the same bakar.run_id, which
    # _container_id (docker/podman ps -q -f label=...) cannot tell apart.
    label_run_id = log.run_id if timeout is not None else None
    cmd = [exe, *_ccache_args(cfg, run_id=label_run_id), subcommand, kas_arg, *extra_args]
    try:
        if capture_to is not None:
            capture_to.parent.mkdir(parents=True, exist_ok=True)
            with capture_to.open("wb") as fh:
                proc = subprocess.run(  # pragma: no cover
                    cmd,
                    cwd=cfg.bsp_root,
                    env=_build_env(cfg, ensure_hashserv=False, eventlog_path=_container_eventlog_path(cfg, log)),
                    stdout=fh,
                    check=False,
                    timeout=timeout,
                )
        else:
            proc = subprocess.run(  # pragma: no cover
                cmd,
                cwd=cfg.bsp_root,
                env=_build_env(cfg, ensure_hashserv=False, eventlog_path=_container_eventlog_path(cfg, log)),
                check=False,
                timeout=timeout,
            )
    except FileNotFoundError:
        log.step_fail(step, reason=f"{exe} not found")
        raise
    except subprocess.TimeoutExpired:
        # subprocess.run's own timeout already killed the direct child; in
        # container mode that leaves the container itself (and whatever it is
        # running) alive, so resolve and stop it by the label above. There is
        # no host-side process left to fall back to the way run_shell_capture
        # falls back to escalate_process_tree - subprocess.run already reaped
        # the direct child - so an unverified stop is surfaced in the failure
        # reason rather than silently read as a clean one.
        reason = f"{subcommand} timed out after {timeout:.0f}s"
        if not cfg.host_mode and not build_stop.escalate_container_tree(log.run_id):
            reason += " (container escalation did not verify the container stopped)"
        log.step_fail(step, reason=reason)
        return 124
    rc = proc.returncode
    if rc != 0:
        log.step_fail(step, reason=f"{subcommand} exited {rc}")
    else:
        log.step_ok(step, exit_code=rc)
    return rc
