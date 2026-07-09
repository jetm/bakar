"""Regenerate the kas YAML and run `kas-container build`.

The YAML generator lives in :mod:`bakar.kas`; this step wraps it
plus the build invocation, and layers in
the static tuning overlay (``overlays/bakar-tuning-<bsp>.yml``)
on top of whatever kas YAML the caller passes in.

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
:func:`bakar.triage._translate_container_path` and is applied only by
``bakar triage`` when it surfaces the failing recipe log. No host-path
rewrite is needed here.
"""

from __future__ import annotations

import os
import pty
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from rich.live import Live

from bakar import build_stop, hashserv, prserv, sccache_server, task_timings, tuning
from bakar.cache_render import (
    build_end_summary_plain,
    build_end_summary_rich,
    cache_delta,
    cache_hit_pct,
    ccache_doc,
    cluster_doc,
    daemon_doc,
    render_ccache_cache,
    render_cluster,
    render_sccache_cache,
)
from bakar.config import _overlay_dir
from bakar.diagnostics import (
    BUILDTOOLS_DIR_ENV,
    detect_buildtools,
    probe_build_daemon,
    probe_ccache,
    probe_cluster,
    resolve_oe_core_release_key,
)
from bakar.eventlog import tail_events
from bakar.kas import KasGenOptions, write_yaml
from bakar.output_mode import OutputMode
from bakar.psi import PSI_DIMS, apply_autocalibration, read_psi_avg10
from bakar.steps.build_ui import BuildUIState, _fmt_stall
from bakar.triage import _translate_container_path, write_error_report

if TYPE_CHECKING:
    from rich.console import Console

    from bakar.bsp_model import BspModel
    from bakar.config import BuildConfig
    from bakar.observability import RunLogger


# knotty in TTY mode emits ANSI CSI escapes to manipulate the cursor and
# redraw progress lines in place.  We strip both the standard CSI form
# (ESC [ ... letter) and the less common OSC form (ESC ] ... BEL) before
# writing to kas.log so downstream tools (triage, grep, bakar log) see
# clean plain text.  The regex is deliberately conservative; anything
# exotic gets left as-is.  See ``bakar log`` for the downstream reader.
ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*\x07")
LINE_SPLIT_RE = re.compile(rb"\r\n|\n|\r")

# Overlay materialization: the kas-container bind-mount only includes
# ``KAS_WORK_DIR`` (= bsp_root) as ``/work``. Copying the overlay
# under ``<bsp_root>/.bakar/overlays/`` puts it inside that mount so
# the ``<user-yml>:<overlay>`` colon-joined arg resolves cleanly from
# the container's perspective.
_OVERLAY_DIR_RELPATH = Path(".bakar") / "overlays"


def _strip_ansi(s: str) -> str:
    return ANSI_OSC_RE.sub("", ANSI_CSI_RE.sub("", s))


# The bakar tuning overlays size build parallelism through a bitbake expression
# `${@os.environ.get('BAKAR_PARALLEL_MAKE') or os.environ.get('NPROC', '16')}`.
# bitbake only honors that env lookup when the var survives clean_environment
# (i.e. is in BB_ENV_PASSTHROUGH_ADDITIONS), which is unreliable across kas
# subcommands - kas build silently dropped it, so every build ran the `16`
# default regardless of config. Resolving the value here and writing a literal
# `-j N` into the materialized overlay makes the figure immune to env scrubbing.
_PARALLELISM_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?P<key>BB_NUMBER_PARSE_THREADS|BB_NUMBER_THREADS|PARALLEL_MAKE)"
    r'\s*=\s*"[^"]*os\.environ\.get[^"]*"',
    re.MULTILINE,
)


def _resolve_nproc_base(cfg: BuildConfig) -> int:
    """Concrete NPROC base, mirroring :func:`_build_env`'s precedence: a
    non-empty live ``NPROC`` env var wins, then ``cfg.nproc``, then
    ``os.cpu_count()``."""
    live = os.environ.get("NPROC")
    if live and live.strip().isdigit():
        return int(live)
    if cfg.nproc is not None:
        return cfg.nproc
    return os.cpu_count() or 16


def _derive_parallelism_plan(cfg: BuildConfig, *, probe_cluster_ok: bool) -> tuning.ParallelismPlan:
    """Derive a :class:`tuning.ParallelismPlan` from every perf input bakar can see.

    Computes the NPROC base (:func:`_resolve_nproc_base`), the active launcher,
    host RAM (:func:`tuning.host_ram_gb`), and - only under sccache-dist when
    ``probe_cluster_ok`` - the live cluster cpu count. The cluster probe shells
    out to the scheduler (network), so callers pass ``probe_cluster_ok=False`` on
    side-effect-free paths (dry-run/script-gen). Any probe failure falls back to
    a None cluster cpu count, which sizes PARALLEL_MAKE to the local cpu count.
    """
    nproc_local = _resolve_nproc_base(cfg)
    launcher = "sccache-dist" if cfg.use_sccache_dist else "ccache" if cfg.use_ccache else "none"
    cluster_cpus: int | None = None
    if cfg.use_sccache_dist and probe_cluster_ok:
        try:
            report = probe_cluster(cfg.sccache_scheduler_url)
            if report.reachable and report.capacity is not None:
                cluster_cpus = report.capacity.num_cpus
        except OSError, subprocess.SubprocessError, ValueError:
            cluster_cpus = None
    return tuning.derive_parallelism(
        nproc_local=nproc_local,
        ram_gb=tuning.host_ram_gb(),
        launcher=launcher,
        cluster_cpus=cluster_cpus,
    )


def _resolve_parallelism(cfg: BuildConfig) -> tuple[int, int]:
    """Resolve ``(PARALLEL_MAKE -j, BB_NUMBER_THREADS)`` to concrete ints.

    An explicit cfg override always wins; an unset field is derived from the
    topology- and RAM-aware plan (:func:`_derive_parallelism_plan`). Cluster
    probing is enabled here because the only caller, :func:`materialize_overlay`
    via :func:`_inject_literal_parallelism`, runs solely on the real-build path -
    the dry-run/script-gen paths return before the overlay materialize calls and
    never reach this code.
    """
    if cfg.parallel_make is not None and cfg.bb_number_threads is not None:
        return cfg.parallel_make, cfg.bb_number_threads
    plan = _derive_parallelism_plan(cfg, probe_cluster_ok=True)
    parallel_make = cfg.parallel_make if cfg.parallel_make is not None else plan.parallel_make
    bb_number_threads = cfg.bb_number_threads if cfg.bb_number_threads is not None else plan.bb_number_threads
    return parallel_make, bb_number_threads


def _inject_literal_parallelism(cfg: BuildConfig, text: str) -> str:
    """Replace the overlay's ``os.environ``-based PARALLEL_MAKE/BB_NUMBER_THREADS
    expressions with the resolved literal values. Lines without the env lookup
    (and overlays without these keys) are returned unchanged."""
    parallel_make, bb_number_threads = _resolve_parallelism(cfg)
    values = {
        "BB_NUMBER_THREADS": str(bb_number_threads),
        "BB_NUMBER_PARSE_THREADS": str(bb_number_threads),
        "PARALLEL_MAKE": f"-j {parallel_make}",
    }

    def _sub(match: re.Match[str]) -> str:
        key = match.group("key")
        return f'{match.group("indent")}{key} = "{values[key]}"'

    return _PARALLELISM_LINE_RE.sub(_sub, text)


def _inject_literal_sccache(cfg: BuildConfig, text: str) -> str:
    """Append literal, exported ``SCCACHE_CONF``/``SCCACHE_DIR`` assignments to
    the sccache overlay's ``local_conf_header``.

    Container mode passes these to the in-container daemon as ``BAKAR_*`` env
    vars and relies on kas's ``null``-env block to whitelist them through
    ``BB_ENV_PASSTHROUGH_ADDITIONS`` - the same mechanism ``kas build`` silently
    drops (see ``_inject_literal_parallelism``). When dropped, the daemon starts
    config-less: local-only compilation, ``$HOME/.cache`` instead of ``/work``,
    no scheduler. Baking the values straight into ``local.conf`` (exported, so
    the daemon subprocess inherits them) makes them immune to env scrubbing.

    The config is bind-mounted at its own host path (see ``_ccache_args``), so
    that absolute path is valid inside the container too. Host mode takes a
    different branch (:func:`_inject_host_sccache`): it exports the pre-started
    daemon's unix socket so private-netns do_compile can reach it."""
    if not cfg.use_sccache_dist:
        return text
    if cfg.host_mode:
        return _inject_host_sccache(text)
    # Idempotency: match the actual exported assignment, not the string
    # "SCCACHE_CONF" which also appears in this overlay's comments and in the
    # BAKAR_SCCACHE_CONF env key (a substring check there would no-op the inject).
    if re.search(r"^\s*export\s+SCCACHE_CONF\b", text, re.MULTILINE):
        return text
    sccache_conf = Path.home() / ".config" / "sccache" / "config"
    if not sccache_conf.is_file():
        return text
    lines = []
    # The scheduler URL also rides the dropped BAKAR_* env path, so bake it in
    # too - both so the daemon knows the scheduler and so the dist guard (which
    # keys on SCCACHE_DIST_SCHEDULER_URL) actually fires. localhost is the
    # container itself, so rewrite to the host gateway as the passthrough does.
    if cfg.sccache_scheduler_url:
        url = cfg.sccache_scheduler_url.replace("localhost", "host.docker.internal")
        lines.append(f'export SCCACHE_DIST_SCHEDULER_URL = "{url}"')
    lines.append(f'export SCCACHE_CONF = "{sccache_conf}"')
    lines.append('export SCCACHE_DIR = "/work/.sccache-cache"')
    m = re.search(r"^(?P<indent>[ \t]+)INHERIT\b", text, re.MULTILINE)
    indent = m.group("indent") if m else "    "
    addition = "".join(f"{indent}{line}\n" for line in lines)
    return text.rstrip("\n") + "\n" + addition


def _inject_host_sccache(text: str) -> str:
    """Bake the pre-started daemon's unix socket into the host-mode sccache overlay.

    bitbake runs each task in a private network namespace (loopback down for
    tasks without a [network] grant), so a TCP ``127.0.0.1:4226`` daemon is
    unreachable: the task's ``sccache gcc`` auto-starts its own server, which
    inherits the kas throwaway ``HOME``, finds no ``~/.config/sccache/config``,
    and compiles locally - the whole build runs on one node while the cluster
    sits idle.

    A unix-domain socket is a filesystem path: it is reachable across the network
    namespace boundary AND without loopback, so every task (do_compile with a
    [network] grant and do_configure without one) can connect to the pre-started
    daemon over it. The daemon - started by ``ensure_running`` in the host netns
    with the real config - does the dist dispatch. Bake an exported
    ``SCCACHE_SERVER_UDS`` into ``local.conf`` (like the container-mode literals
    above) so it survives kas's ``clean_environment`` scrub. The path matches
    :func:`sccache_server.default_uds_path`, which ``ensure_running`` binds.

    A global export routes do_configure's conftests through the daemon too (they
    distribute or fail fast to a local recompile); a task-scoped socket is NOT an
    option, because a configure task stripped of the socket falls back to the TCP
    port and its loopback-down netns then makes sccache fail outright.
    """
    if re.search(r"^\s*export\s+SCCACHE_SERVER_UDS\b", text, re.MULTILINE):
        return text
    uds = sccache_server.default_uds_path()
    m = re.search(r"^(?P<indent>[ \t]+)INHERIT\b", text, re.MULTILINE)
    indent = m.group("indent") if m else "    "
    addition = f'{indent}export SCCACHE_SERVER_UDS = "{uds}"\n'
    return text.rstrip("\n") + "\n" + addition


def _inject_literal_ccache(cfg: BuildConfig, text: str) -> str:
    """Set ``CCACHE_DIR`` to the per-mode cache path.

    The overlay carries a neutral, host-canonical default (``${TOPDIR}/ccache``)
    that never names a container path; this rewrites it to the absolute host
    cache dir in host mode (the default), or to the kas-container bind-mount
    target (``/work/ccache``) when the container path is opted in. The rewrite
    runs in both modes so the host default stays free of any ``/work`` reference
    and the container value is constructed here rather than hardcoded in the
    overlay - container mode bind-mounts ``cfg.effective_ccache_dir`` to
    ``/work/ccache`` (see ``_ccache_args``), the in-container path written here.

    Only the ``CCACHE_DIR`` line is touched - ``CCACHE_MAXSIZE``, the
    ``export CCACHE_MAXSIZE``, ``INHERIT += "ccache"``, and the nodejs disable
    are left alone. The original indentation is preserved. Kept pure (no
    filesystem side effects) so dry-run rendering is safe; the host-mode dir is
    created by the caller (``materialize_overlay``)."""
    target = str(cfg.effective_ccache_dir) if cfg.host_mode else "/work/ccache"
    return re.sub(
        r'^(?P<indent>[ \t]*)CCACHE_DIR\s*=\s*"[^"]*"',
        lambda m: f'{m.group("indent")}CCACHE_DIR = "{target}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )


# The base overlays statically strip rm_work (default off while bakar is in
# use); _inject_rm_work deletes that whole block when [build] rm_work is opted
# back on. The block spans its comment through the USER_CLASSES line, so the
# generated local.conf carries no stale "we disabled rm_work" comment when the
# user kept it on. Only the base overlays carry the block, so this no-ops on the
# opt-in overlays.
_RM_WORK_BLOCK_RE = re.compile(
    r"\n[ \t]*#[^\n]*Disable rm_work while bakar.*?\n[ \t]*USER_CLASSES:remove = \"rm_work\"",
    re.DOTALL,
)


def _inject_rm_work(cfg: BuildConfig, text: str) -> str:
    """Remove the rm_work-removal block when ``cfg.rm_work`` is True.

    Default (rm_work False) keeps the base overlay's ``INHERIT:remove`` /
    ``USER_CLASSES:remove = "rm_work"`` lines so rm_work stays off while bakar is
    in use. When the user opts rm_work back on ([build] rm_work / BAKAR_RM_WORK /
    .bakar.toml), strip the block so the container's default rm_work stands."""
    if not cfg.rm_work:
        return text
    return _RM_WORK_BLOCK_RE.sub("", text)


def materialize_overlay(cfg: BuildConfig, overlay_source: Path) -> Path:
    """Copy ``overlay_source`` into ``<bsp_root>/.bakar/overlays/``.

    Returns the path *relative to* ``cfg.bsp_root`` so callers can
    pass it straight into the ``kas-container build <user>:<overlay>``
    colon-joined argument.

    Always overwrites the destination so the overlay content tracks
    ``overlay_source`` byte-for-byte on every invocation. Earlier
    revisions symlinked, but kas resolves symlinks before running its
    "all configs must share a git repo" check, so a YAML in repo A
    layered with a symlink whose target lives in repo B (the bakar
    install) tripped ``All concatenated config files must belong to
    the same repository or all must be outside of versioning control``.
    Copying drops a real file into the user's tree, putting both
    configs in the same repo (or outside any repo) and sidesteps the
    bind-mount issue where a symlink target outside ``KAS_WORK_DIR``
    dangles inside the kas-container view.
    """
    overlay_dir = cfg.bsp_root / _OVERLAY_DIR_RELPATH
    overlay_dir.mkdir(parents=True, exist_ok=True)
    dest = overlay_dir / overlay_source.name
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    shutil.copy2(overlay_source, dest)
    # Bake the resolved parallelism into the bakar tuning overlays so the figure
    # cannot be lost to bitbake's clean_environment (see _inject_literal_parallelism).
    if overlay_source.name.startswith("bakar-tuning-"):
        original = dest.read_text(encoding="utf-8")
        injected = _inject_literal_parallelism(cfg, original)
        injected = _inject_rm_work(cfg, injected)
        if overlay_source.name == "bakar-tuning-sccache.yml":
            injected = _inject_literal_sccache(cfg, injected)
        if overlay_source.name == "bakar-tuning-ccache.yml":
            injected = _inject_literal_ccache(cfg, injected)
            # Host mode has no /work/ccache bind mount; ensure the rewritten host
            # cache dir exists so ccache can write. Real-build-only path - the
            # injector stays pure for dry-run rendering.
            if cfg.host_mode:
                cfg.effective_ccache_dir.mkdir(parents=True, exist_ok=True)
        if injected != original:
            dest.write_text(injected, encoding="utf-8")
    return dest.relative_to(cfg.bsp_root)


# The bakar-provided sccache layer (classes/sccache.bbclass) is materialized
# next to the overlays under ``.bakar/`` so kas can add it via the sccache
# overlay's ``repos:`` entry. The relative repos path ``.bakar/<name>`` resolves
# against ``bsp_root`` in both host mode (build CWD) and container mode
# (KAS_WORK_DIR = /work), mirroring :func:`materialize_overlay`.
_SCCACHE_LAYER_NAME = "meta-bakar-sccache"


def materialize_sccache_layer(cfg: BuildConfig) -> Path:
    """Copy the bundled ``meta-bakar-sccache`` layer into ``<bsp_root>/.bakar/``.

    Returns the destination directory. Overwrites on every call so the layer
    tracks the packaged source byte-for-byte. The sccache tuning overlay
    references it by the relative path ``.bakar/meta-bakar-sccache``.
    """
    source = _overlay_dir() / _SCCACHE_LAYER_NAME
    # kas resolves the overlay's relative `.bakar/meta-bakar-sccache` repos path
    # against KAS_WORK_DIR (see _build_env). meta-avocado sets KAS_WORK_DIR to the
    # workspace while bsp_root is the nested workspace/build-<stem> dir, so the
    # layer must land under the workspace .bakar; for every other family
    # KAS_WORK_DIR == bsp_root, so they coincide.
    base = cfg.workspace if cfg.is_meta_avocado else cfg.bsp_root
    dest = base / ".bakar" / _SCCACHE_LAYER_NAME
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    return dest


_HOST_LAYER_NAME = "meta-bakar-host"


def materialize_host_layer(cfg: BuildConfig) -> Path:
    """Copy the bundled ``meta-bakar-host`` layer into ``<bsp_root>/.bakar/``.

    Returns the destination directory. Overwrites on every call so the layer
    tracks the packaged source byte-for-byte. The host tuning overlay
    (``bakar-tuning-host.yml``) references it by the relative path
    ``.bakar/meta-bakar-host``. Mirrors :func:`materialize_sccache_layer`; only
    invoked in host mode, where the layer's rpm bbappend keeps rpm-native from
    dlopening the build host's rpm transaction plugins.
    """
    source = _overlay_dir() / _HOST_LAYER_NAME
    base = cfg.workspace if cfg.is_meta_avocado else cfg.bsp_root
    dest = base / ".bakar" / _HOST_LAYER_NAME
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    return dest


def _setup_meta_avocado_build_dir(cfg: BuildConfig) -> None:
    """Create the build directory for Avocado OS builds.

    Idempotent: safe to call on every build invocation.
    """
    cfg.bsp_root.mkdir(parents=True, exist_ok=True)


def _write_meta_avocado_wrapper(cfg: BuildConfig, kas_yaml: Path) -> Path:
    """Write a wrapper YAML that includes the machine YAML via repo reference.

    The wrapper is the single top-level file fed to ``kas dump``. It
    declares meta-avocado as a local repo so kas can resolve the
    ``repo: meta-avocado`` include. The overlay is passed separately as
    the second colon-joined argument to ``kas dump`` (both wrapper and
    overlay live in ``bsp_root``, which shares the same git root, so
    the same-repo check passes).

    Returns the wrapper path (``bsp_root/avocado-wrapper.yml``).
    """
    abs_yaml = kas_yaml.resolve()
    for parent in [abs_yaml, *abs_yaml.parents]:
        if parent.name == "meta-avocado":
            yaml_in_meta = abs_yaml.relative_to(parent)
            break
    else:
        raise RuntimeError(f"kas YAML {kas_yaml} is not inside a meta-avocado repository")
    wrapper = cfg.bsp_root / "avocado-wrapper.yml"
    wrapper.write_text(
        "header:\n"
        "  version: 16\n"
        "  includes:\n"
        "    - repo: meta-avocado\n"
        f"      file: {yaml_in_meta.as_posix()}\n"
        "repos:\n"
        "  meta-avocado:\n"
        "    path: meta-avocado\n",
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
    dump = cfg.bsp_root / "avocado-bakar.yml"
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
    list for host-mode builds where no container is involved.

    The returned list is shaped as exactly two elements: ``--runtime-args``
    followed by a single concatenated string value. kas-container parses
    ``--runtime-args`` as one string; emitting two ``--runtime-args`` pairs
    would let the second occurrence overwrite the first.

    The string always contains the workspace ccache bind mount. When
    ``cfg.use_hashequiv`` is True, ``--add-host=host.docker.internal:gateway``
    is appended so the container can reach the hashserv daemon on the host
    bridge. When ``eventlog_path`` is provided, ``-e BB_DEFAULT_EVENTLOG=<path>``
    is appended so bitbake inside the container writes its event log to the
    run-dir path that is bind-mounted under ``/work``. kas-container only
    forwards a fixed env-var allowlist into Docker, so this is the only
    reliable way to pass ``BB_DEFAULT_EVENTLOG`` through.

    Creates the host-side ccache directory when absent so the Docker
    bind-mount never targets a missing path. When ``dry_run`` is True, the
    directory is not created so a preview invocation has no filesystem effect.
    """
    if cfg.host_mode:
        return []
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
    artifact = f"build/tmp/deploy/images/{cfg.machine}/{cfg.image}-{cfg.machine}.wic"
    sys.stdout.write(f"INFO     artifact: {artifact}\n")
    sys.stdout.flush()


def clear_stale_bitbake_locks(cfg: BuildConfig) -> list[Path]:
    """Remove stale bitbake lock and socket files when the owning process is gone.

    BitBake writes its PID into ``<build>/bitbake.lock`` at startup and
    removes it on clean exit. A crash leaves the lock and both Unix sockets
    (``bitbake.sock``, ``hashserve.sock``) behind, causing the next
    invocation to refuse to start ("bitbake is already running").

    Returns the list of paths removed.
    """
    build_dir = cfg.bsp_root / "build"
    lock = build_dir / "bitbake.lock"
    sockets = [build_dir / "bitbake.sock", build_dir / "hashserve.sock"]

    def _remove_all() -> list[Path]:
        removed = []
        for p in [lock, *sockets]:
            if p.exists() or p.is_socket():
                p.unlink(missing_ok=True)
                removed.append(p)
        return removed

    if not lock.exists():
        return _remove_all()

    try:
        pid = int(lock.read_text().strip())
    except ValueError, OSError:
        return _remove_all()

    try:
        os.kill(pid, 0)
        # Process exists - confirm it is actually bitbake before leaving the lock alone.
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        if cmdline_path.exists():
            cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode(errors="replace")
            if "bitbake" not in cmdline.lower():
                return _remove_all()
    except ProcessLookupError:
        return _remove_all()
    except PermissionError:
        pass
    return []


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
    # The sccache overlay references the meta-bakar-sccache layer by a relative
    # repos path; materialize it under .bakar/ so kas can resolve and inherit it.
    if cfg.use_sccache_dist:
        materialize_sccache_layer(cfg)
    if cfg.host_mode:
        materialize_host_layer(cfg)
    if cfg.is_meta_avocado:
        _setup_meta_avocado_build_dir(cfg)
        overlay_rel = materialize_overlay(cfg, overlay_source)
        extra_overlay_rels = [materialize_overlay(cfg, p) for p in extra_overlays or []]
        wrapper = _write_meta_avocado_wrapper(cfg, kas_yaml)
        dump = _run_kas_dump(cfg, wrapper, overlay_rel, extra_overlay_rels)
        return str(dump)
    kas_yaml_rel = _resolve_user_yaml(cfg, kas_yaml)
    overlay_rel = materialize_overlay(cfg, overlay_source)
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
    the oe-layertool setup script for ti, and ``kas-container checkout`` for
    bbsetup/generic (and any other family). The commands match what a real
    sync would invoke (see :mod:`bakar.commands.sync` and
    :func:`bakar.steps.ti_layertool._build_layertool_cmd`).
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
    return [f"kas-container checkout {shlex.quote(kas_arg)}"]


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


# How often the stall watchdog samples running-task log freshness.
_STALL_POLL_SECS = 30

# How often the error watchdog checks for a task failure. Short on purpose -
# unlike the stall watchdog (which waits out a long silence threshold before
# it even starts caring), this one exists to react as fast as possible once
# ui.had_task_failures flips true, matching _heartbeat's cadence.
_ERROR_POLL_SECS = 1

# Plain-mode status heartbeat tick. The TICK is the throttle: the status thread
# samples the current build state once per interval (level-sampled), so a task
# storm cannot flood the log. plain_status_line() only dedups identical lines.
_PLAIN_STATUS_INTERVAL = 2.0


class _PlainFrameController:
    """Frame controller for plain (CI) output: no Rich ``Live``, a throttled status thread.

    Exposes the exact surface the PTY closures call on a Rich ``Live`` - ``console``,
    ``stop()``, ``start(*, refresh=False)``, and a writable ``transient`` attribute - as
    no-ops / plain writes, so ``_run_pty_with_ui``'s body runs unchanged. As a context
    manager it starts a daemon thread that prints ``ui.plain_status_line()`` on a fixed
    ``_PLAIN_STATUS_INTERVAL`` tick and joins it on exit (before the caller prints its
    post-build summary), so a stale heartbeat cannot interleave with the final lines.
    """

    def __init__(self, ui: BuildUIState, console: Console, stop_event: threading.Event) -> None:
        self.console = console
        self.transient = False
        self._ui = ui
        self._stop_event = stop_event
        self._thread: threading.Thread | None = None

    def stop(self) -> None:
        """No-op: there is no Live region to tear down in plain mode."""

    def start(self, *, refresh: bool = False) -> None:
        """No-op: mirrors ``Live.start(refresh=...)`` so the freeze/restart path is safe."""

    def _loop(self) -> None:  # pragma: no cover - timing-driven daemon thread
        while not self._stop_event.wait(timeout=_PLAIN_STATUS_INTERVAL):
            line = self._ui.plain_status_line()
            if line is not None:
                # markup=False: the status line contains literal brackets (e.g.
                # "bakar[build]") that Rich would otherwise parse as style tags.
                self.console.print(line, markup=False)

    def __enter__(self) -> _PlainFrameController:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        # Set the stop event ourselves so the status thread always terminates,
        # even if the body raised before its own stop_event.set() (e.g. a Ctrl-C
        # during thread startup) - otherwise the join would time out with the
        # daemon still emitting heartbeats during teardown.
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2 * _PLAIN_STATUS_INTERVAL)


@dataclass(slots=True)
class _PtyOutcome:
    """Result of a PTY-driven run: the child exit code plus stall-abort context.

    ``stall_tasks`` is the list of running task labels at the moment the stall
    watchdog aborted the build (``None`` for a normal exit), so the caller can
    record a ``stall-timeout`` step_fail instead of a bare exit code.

    ``cache_backend``/``cache_doc`` carry the active backend name and its
    per-build cache delta (computed at teardown), so the caller can print the
    build-end summary at the post-block site. Both are ``None`` when no cache
    backend was active.
    """

    rc: int | None
    stall_tasks: list[str] | None = None
    cache_backend: str | None = None
    cache_doc: dict | None = None


def _build_fail_reason(rc: int | None, stall_tasks: list[str] | None) -> str:
    """Compose the step_fail reason for a build, naming stuck tasks on a stall abort."""
    if stall_tasks:
        return f"stall-timeout: {', '.join(stall_tasks)}"
    if rc is not None:
        return f"exit_code={rc}"
    return "wrapper-crash"


def _print_cache_summary(log: RunLogger, backend: str | None, doc: dict | None, output_mode: OutputMode) -> None:
    """Print the build-end cache-usage summary at the post-block summary site.

    Called from the runner's finally, after the live frame has closed, so the
    summary cannot interleave with a heartbeat frame. Best-effort: emits nothing
    when no cache backend was active and never crashes a completed build.
    """
    if not doc or backend is None:
        return
    try:
        if output_mode is OutputMode.PLAIN:
            # markup=False: the ``bakar[cache]`` prefix has literal brackets that
            # Rich would otherwise parse as a style tag (as the heartbeat does).
            summary = build_end_summary_plain(doc, backend)
            if summary:
                log.console.print(summary, markup=False)
        else:
            log.console.print(build_end_summary_rich(doc, backend))
    except Exception:  # noqa: BLE001 - best-effort; never crash a completed build
        return


def _run_pty_with_ui(
    cmd: list[str],
    cfg: BuildConfig,
    log: RunLogger,
    ui: BuildUIState,
    stop_event: threading.Event,
    *,
    show_layers: bool = False,
    output_mode: OutputMode = OutputMode.RICH,
) -> _PtyOutcome:
    """Run ``cmd`` under a PTY, pumping its output into ``ui`` live.

    The pump thread writes every line to kas.log for `bakar log` to tail,
    parses bitbake counters into a rich Progress bar, and surfaces
    ERROR/WARNING/FATAL/QA Issue lines above the bar.  Nothing goes to
    sys.stdout directly - the Progress instance owns the terminal.

    PTY plumbing: openpty() gives us a (master, slave) fd pair. We pass
    slave as the child's stdout/stderr so kas-container's `[ -t 1 ]`
    check sees a TTY and adds `-t -i` to `docker run`, which in turn
    makes bitbake's knotty UI interactive. knotty uses CR (no newline)
    to redraw its status line in place, so we read chunks and split on
    \\r, \\n, or \\r\\n manually instead of line-iterating.

    Returns a :class:`_PtyOutcome` carrying the child exit code (``rc`` is
    ``None`` only if the wrapper crashed before ``proc.wait()`` could run) and,
    when the stall watchdog aborted the build, the wedged task labels. Does not
    do step logging, warn/err printing, PSI calibration, or sampler management -
    the caller owns those.
    """
    rc: int | None = None
    stall_tasks: list[str] | None = None
    # Per-build cache delta for the build-end summary, filled at teardown.
    cache_backend: str | None = None
    cache_doc: dict | None = None
    master_fd, slave_fd = pty.openpty()  # pragma: no cover
    try:
        with log.kas_log_path.open("w", encoding="utf-8", buffering=1) as kas_log:
            proc = subprocess.Popen(  # pragma: no cover
                cmd,
                cwd=cfg.bsp_root,
                # stdin must be a TTY too: kas-container sees stdout as a
                # TTY (via slave_fd) and passes -t -i to docker, which
                # then requires stdin to also be a TTY or it refuses with
                # "cannot attach stdin to a TTY-enabled container
                # because stdin is not a terminal". Sharing the same pty
                # slave across stdin/stdout/stderr satisfies that check.
                # We never write to master_fd, so the child's stdin reads
                # block indefinitely - which is fine for a batch build.
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=_build_env(cfg, eventlog_path=_container_eventlog_path(cfg, log)),
                start_new_session=True,
                close_fds=True,
            )
            os.close(slave_fd)
            slave_fd = -1
            # Persist the build PGID so `bakar stop` can target this run.
            # proc.pid is the PGID because start_new_session=True above makes
            # the child a process-group leader.
            build_stop.write_launch_record(
                log.run_dir,
                pgid=proc.pid,
                mode=("host" if cfg.host_mode else "container"),
                runtime=(None if cfg.host_mode else build_stop._detect_runtime()),
                container_label=(None if cfg.host_mode else build_stop.run_id_label(log.run_id)),
            )

            live_frozen = False

            def _process_line(line: str) -> None:  # pragma: no cover
                nonlocal live_frozen
                kas_log.write(line + "\n")
                kas_log.flush()
                msg = ui.process_line(line)
                # Failure freeze: stop the Live BEFORE printing the first
                # error line of a task failure, committing the collapsed
                # frame (pipeline, sstate, failure count) into the
                # scrollback above the failure text about to stream.
                if not live_frozen and ui.take_fail_freeze():
                    live.stop()
                    live_frozen = True
                if msg:
                    live.console.print(msg)
                info = ui.take_pending_log()
                if info:
                    log.info(info)
                alerts = ui.take_pending_alerts()
                for alert in alerts:
                    live.console.print(alert)
                # Resume the Live once the failure context has fully landed:
                # after the TaskFailed alert block (event feed), or on the
                # next task-counter line (regex fallback, where no event
                # will arrive).
                if live_frozen and (alerts or ui.take_pending_restart()):
                    live.start(refresh=True)
                    live_frozen = False
                    ui.notify_restarted()

            def _pump() -> None:  # pragma: no cover
                buf = b""
                while True:
                    try:
                        chunk = os.read(master_fd, 8192)
                    except OSError:
                        # EIO fires on Linux when the slave side closes
                        # (child exited). Treat as EOF.
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        m = LINE_SPLIT_RE.search(buf)
                        if m is None:
                            break
                        raw = buf[: m.start()]
                        buf = buf[m.end() :]
                        if not raw:
                            continue
                        line = _strip_ansi(raw.decode("utf-8", errors="replace"))
                        _process_line(line)
                if buf:
                    tail = _strip_ansi(buf.decode("utf-8", errors="replace"))
                    if tail:
                        _process_line(tail)

            # One-shot layer display: kas materializes bblayers.conf early in
            # the build (manifest paths have it even earlier, from setup-env),
            # so the heartbeat polls for it and prints the panel above the
            # live region as soon as the data exists - at the START of the
            # build, where it is useful, instead of after it finishes.
            layers_pending = show_layers

            def _heartbeat() -> None:
                nonlocal layers_pending
                while not stop_event.wait(timeout=1):
                    if proc.poll() is not None:
                        break
                    if layers_pending:  # pragma: no cover - PTY-thread path
                        from bakar.layers import collect_layer_hashes, layer_hash_table

                        hashes = collect_layer_hashes(cfg)
                        if hashes:
                            live.console.print(layer_hash_table(hashes))
                            layers_pending = False

            event_feed_count = 0
            event_feed_error = ""

            def _event_tail() -> None:  # pragma: no cover
                # Authoritative feed: drive the live model from bitbake's
                # structured event log. ui.process_line (regex) stays as the
                # degraded fallback. A tailer error must never crash the
                # build, but it must not die silently either - the count and
                # error are reported after the build so a dead feed (live UI
                # quietly running on the regex fallback) is diagnosable.
                nonlocal event_feed_count, event_feed_error
                try:
                    for class_name, event in tail_events(log.eventlog_path, stop_event):
                        ui.process_event(class_name, event)
                        event_feed_count += 1
                except Exception as exc:  # noqa: BLE001 - event feed errors are captured and reported; must not crash the build thread
                    event_feed_error = f"{type(exc).__name__}: {exc}"

            def _stall_watchdog() -> None:  # pragma: no cover
                # Self-guard against a wedged task (e.g. a deadlocked final
                # link): when every running task's log has been silent past
                # cfg.stall_abort_secs, SIGINT the build so it fails cleanly
                # naming the stuck task instead of spinning until the user
                # Ctrl-C's. bitbake's own keepalive output flows through the
                # PTY pump, so raw output cannot be the signal - log freshness
                # is what distinguishes a wedge from a slow-but-alive compile.
                nonlocal stall_tasks
                if cfg.stall_abort_secs <= 0:
                    return
                while not stop_event.wait(timeout=_STALL_POLL_SECS):
                    if proc.poll() is not None:
                        break
                    report = ui.stall_report()
                    if report is None:
                        continue
                    stalled, labels = report
                    if stalled >= cfg.stall_abort_secs:
                        stall_tasks = labels
                        log.warn(
                            f"build stalled: no log output for {_fmt_stall(stalled)} from running "
                            f"task(s) {', '.join(labels)}; aborting. Disable with "
                            "`bakar settings set build.stall_abort_secs 0`."
                        )
                        build_stop.stop_running_proc(proc, cfg, log)
                        break

            def _error_watchdog() -> None:  # pragma: no cover
                # SIGINT the build the moment any task fails, instead of
                # waiting for bitbake's own halt-on-failure to drain every
                # already-running task on its own schedule. bitbake already
                # stops scheduling *new* tasks the instant a task fails
                # regardless of this setting - this only stops bakar's live
                # view from rendering a misleadingly-normal progress display
                # while it waits for tasks that started before the failure
                # (which can run for a long time) to finish on their own.
                if not cfg.stop_on_error:
                    return
                while not stop_event.wait(timeout=_ERROR_POLL_SECS):
                    if proc.poll() is not None:
                        break
                    if ui.had_task_failures:
                        log.warn(
                            "build failed: a task reported failure; aborting immediately. "
                            "Disable with `bakar settings set build.stop_on_error false`."
                        )
                        build_stop.stop_running_proc(proc, cfg, log)
                        break

            # Holds the freshest daemon_doc/ccache_doc the cache-probe thread
            # computed, so the build-end persist reuses that probe rather than
            # issuing a second one after the build completes. The ``first_*``
            # holders snapshot the FIRST SUCCESSFUL PROBE from the cache-probe
            # thread (see ``_cache_probe`` -> ``_refresh`` below), NOT build
            # start: the thread's initial ``_refresh()`` call races the build
            # process and can fail (daemon/cache not up yet), in which case the
            # holder stays None until a later iteration succeeds. Any cache
            # activity between build start and that first successful probe is
            # therefore excluded from the build-end delta (``cache_delta``
            # below). This is a deliberate tradeoff, not a bug: closing the gap
            # would require a synchronous pre-loop baseline snapshot, which is
            # riskier than the narrow accuracy gap it leaves. In the degenerate
            # case where the probe only ever succeeds once (first == last), the
            # delta is honestly all-zero - not wrong, just narrow.
            last_daemon_doc: list = [None]
            first_daemon_doc: list = [None]
            last_ccache_doc: list = [None]
            first_ccache_doc: list = [None]

            def _cache_probe() -> None:  # pragma: no cover
                # Refresh the cluster/cache header lines shown in the build UI.
                # sccache-dist builds show the cluster + sccache daemon lines;
                # ccache builds show a single ccache hit/miss line. No-op when
                # neither cache launcher is active.
                if not (cfg.use_sccache_dist or cfg.ccache):
                    return

                def _refresh() -> None:
                    # Best-effort cosmetic probe: a failure here must never crash
                    # or spew from this daemon thread, so swallow everything (the
                    # probes are never-raise in production; this guards the test
                    # harness and any unforeseen edge).
                    try:
                        if cfg.use_sccache_dist:
                            cluster = probe_cluster(cfg.sccache_scheduler_url)
                            daemon = probe_build_daemon()
                            lines = render_cluster(cluster_doc(cluster, cfg.sccache_scheduler_url))
                            doc = daemon_doc(daemon) if daemon.running else None
                            if doc is not None:
                                last_daemon_doc[0] = doc
                                if first_daemon_doc[0] is None:
                                    first_daemon_doc[0] = doc
                                # Live badge is status, not accounting: cumulative
                                # so-far hit rate plus the current daemon verdict.
                                ui.set_cache_badge(
                                    active=True,
                                    hit_pct=cache_hit_pct(doc["cache_hits"], doc["cache_misses"]),
                                    verdict=doc["verdict"],
                                )
                            lines.append(render_sccache_cache(doc))
                        else:
                            cc = probe_ccache(cfg.effective_ccache_dir)
                            cc_doc = ccache_doc(cc)
                            if cc_doc is not None:
                                last_ccache_doc[0] = cc_doc
                                if first_ccache_doc[0] is None:
                                    first_ccache_doc[0] = cc_doc
                                # ccache has no distribution: cache badge only,
                                # no verdict (suppresses the dist badge/token).
                                ui.set_cache_badge(active=True, hit_pct=cc_doc["hit_rate"], verdict=None)
                            lines = [render_ccache_cache(cc_doc)]
                        ui.set_dist_lines(lines)
                    except Exception:  # noqa: BLE001 - cosmetic probe, never crash the build thread
                        return

                _refresh()  # show immediately
                while not stop_event.wait(timeout=3):
                    _refresh()

            # Share the run logger's console so log.info() (the parse-complete
            # line) coordinates with the live region instead of printing onto
            # the same line as the setup bar.
            frame_cm: Live | _PlainFrameController = (
                _PlainFrameController(ui, log.console, stop_event)
                if output_mode is OutputMode.PLAIN
                else Live(get_renderable=ui.make_renderable, console=log.console, refresh_per_second=8)
            )
            with frame_cm as live:
                pump = threading.Thread(target=_pump, daemon=True)  # pragma: no cover
                pump.start()
                heartbeat = threading.Thread(target=_heartbeat, daemon=True)  # pragma: no cover
                heartbeat.start()
                event_tail = threading.Thread(target=_event_tail, daemon=True)  # pragma: no cover
                event_tail.start()
                watchdog = threading.Thread(target=_stall_watchdog, daemon=True)  # pragma: no cover
                watchdog.start()
                error_watchdog = threading.Thread(target=_error_watchdog, daemon=True)  # pragma: no cover
                error_watchdog.start()
                cache_probe = threading.Thread(target=_cache_probe, daemon=True)  # pragma: no cover
                cache_probe.start()
                try:
                    rc = proc.wait()
                except KeyboardInterrupt:
                    build_stop.stop_running_proc(proc, cfg, log)
                    rc = proc.wait()
                stop_event.set()
                pump.join(timeout=5)
                heartbeat.join(timeout=2)
                watchdog.join(timeout=2)
                error_watchdog.join(timeout=2)
                if layers_pending:  # pragma: no cover - fast build finished before first heartbeat tick
                    from bakar.layers import collect_layer_hashes, layer_hash_table

                    hashes = collect_layer_hashes(cfg)
                    if hashes:
                        live.console.print(layer_hash_table(hashes))
                        layers_pending = False
                event_tail.join(timeout=5)
                # Join the cache probe (the one teardown thread not joined
                # above) so the last_* holders are current before we read them.
                cache_probe.join(timeout=1)
                # Persist this-build cache deltas (the raw counters are
                # cumulative odometers). Persist the DELTA, not the lifetime
                # total, for whichever backend was active; the probe branches are
                # mutually exclusive so exactly one artifact is written. Compute
                # the summary doc here (inside the block, where the holders are
                # read) but PRINT it at the post-block site. Best-effort: a
                # persistence failure must never crash a completed build.
                try:
                    sccache_delta = cache_delta(first_daemon_doc[0], last_daemon_doc[0])
                    ccache_delta = cache_delta(first_ccache_doc[0], last_ccache_doc[0])
                    log.persist_sccache_stats(sccache_delta)
                    log.persist_ccache_stats(ccache_delta)
                    if sccache_delta is not None:
                        cache_backend, cache_doc = "sccache", sccache_delta
                    elif ccache_delta is not None:
                        cache_backend, cache_doc = "ccache", ccache_delta
                except Exception as exc:  # noqa: BLE001 - best-effort; never crash the build
                    log.warn(f"failed to persist cache stats: {exc}")
                if event_feed_error:
                    log.warn(f"bitbake event feed died ({event_feed_error}); live UI ran on regex fallback")
                elif event_feed_count == 0:
                    log.warn(
                        f"bitbake event feed inactive (0 events from {log.eventlog_path}); "
                        "live UI ran on regex fallback"
                    )
                if rc == 0:
                    # Freeze the final frame with every reached pipeline
                    # segment checked (Live renders once more on exit);
                    # without this the header ends on a spinner forever.
                    ui.finish()
                elif ui.had_task_failures:
                    # Each failure's pipeline status and context already
                    # committed inline (frozen frame + alert block);
                    # repeating the frame here would wedge it between the
                    # failure text and the runner's exit lines. No-op when
                    # the Live is still frozen (already out of the way).
                    live.transient = True
                else:
                    # Failed without a recorded task failure (parse abort,
                    # container error): keep a collapsed closing status.
                    ui.finish_failed()
    finally:
        build_stop.remove_pid(log.run_dir)
        if slave_fd != -1:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass
    return _PtyOutcome(rc=rc, stall_tasks=stall_tasks, cache_backend=cache_backend, cache_doc=cache_doc)


def run_build(ctx: KasBuildContext, *, extra_overlays: list[Path] | None = None, show_layers: bool = False) -> int:
    """Run `kas-container build <kas_yaml>:<overlay>` with the measurement harness.

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
            print(line)
        log.step_skip("kas_build", reason="dry-run")
        return 0

    removed = clear_stale_bitbake_locks(cfg)
    for lock in removed:
        log.warn(f"removed stale bitbake lock: {lock} (owning process was gone)")

    build_stop.check_unclean_stop(cfg.bsp_root, log.console)

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

    log.info(f"exec: {' '.join(cmd)}")
    # Baselines are scoped per (workspace, machine, mode): a different
    # project's builds must not train the stuck-task thresholds this one reads.
    timings_path = task_timings.timings_path_for(cfg.bsp_root, cfg.machine, host_mode=cfg.host_mode)
    # ``ui`` is created before the try so the finally block can always read
    # its warn/error counts even if _run_pty_with_ui raises before returning.
    ui = BuildUIState(
        start_monotonic=log.start_monotonic,
        logfile_translator=(None if cfg.host_mode else lambda p: _translate_container_path(p, cfg.bsp_root)),
        timings_path=timings_path,
    )
    terminated = False
    rc: int | None = None
    stall_tasks: list[str] | None = None
    outcome: _PtyOutcome | None = None
    try:
        outcome = _run_pty_with_ui(cmd, cfg, log, ui, stop_event, show_layers=show_layers, output_mode=ctx.output_mode)
        rc, stall_tasks = outcome.rc, outcome.stall_tasks
        if rc == 0:
            deploy = cfg.bsp_root / "build" / "tmp" / "deploy" / "images" / cfg.machine
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
                deploy = cfg.bsp_root / "build" / "tmp" / "deploy" / "images" / cfg.machine
                log.step_ok("kas_build", deploy_dir=str(deploy), exit_code=rc)
            else:
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


def run_shell_live(ctx: KasBuildContext, command: str) -> int:
    """Run ``kas-container shell -c <command>`` with the live knotty UI.

    Sister to :func:`run_shell_capture`, but instead of capturing output to
    a file it pumps the child's PTY through :func:`_run_pty_with_ui` so the
    user sees knotty's live progress bar. Used for non-interactive
    ``bakar bitbake`` invocations (anything that is not ``devshell`` or
    ``listtasks``). Returns the kas-container exit code.
    """
    cfg, log, kas_yaml, overlay_source = ctx.cfg, ctx.log, ctx.kas_yaml, ctx.overlay_source
    log.step_start("kas_shell_live", command=command, host_mode=cfg.host_mode)
    kas_arg = _build_kas_arg(cfg, kas_yaml, overlay_source, ctx.extra_overlays)
    exe = "kas" if cfg.host_mode else "kas-container"
    cmd = [exe, *_ccache_args(cfg, eventlog_path=_container_eventlog_path(cfg, log)), "shell", kas_arg, "-c", command]

    ui = BuildUIState(
        start_monotonic=log.start_monotonic,
        logfile_translator=(None if cfg.host_mode else lambda p: _translate_container_path(p, cfg.bsp_root)),
        timings_path=task_timings.timings_path_for(cfg.bsp_root, cfg.machine, host_mode=cfg.host_mode),
    )
    stop_event = threading.Event()

    # Mirror run_build's terminal-event guarantee: if _run_pty_with_ui raises
    # before returning (e.g. kas-container missing -> FileNotFoundError), the
    # finally still sets stop_event (stopping the heartbeat thread), prints the
    # tally, and emits a step_fail so events.jsonl never dead-ends at step_start
    # and bakar triage has a terminal event to find.
    rc: int | None = None
    completed = False
    outcome: _PtyOutcome | None = None
    try:
        outcome = _run_pty_with_ui(cmd, cfg, log, ui, stop_event, output_mode=ctx.output_mode)
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
        if completed and actual_rc == 0:
            log.step_ok("kas_shell_live", exit_code=actual_rc)
        else:
            log.step_fail(
                "kas_shell_live",
                reason=f"exit_code={actual_rc}" if completed else "wrapper-crash",
                exit_code=actual_rc,
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
        if k.startswith(("KAS_", "BB_", "SSTATE_", "DL_", "NPROC", "PATH", "HOME", "USER", "SDKMACHINE"))
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
    """
    eventlog_dir = cfg.bsp_root / "build" / "tmp" / "log" / "eventlog"
    if not eventlog_dir.is_dir():
        return None
    try:
        watermark = datetime.strptime(log.run_id, "%Y%m%d-%H%M%S").timestamp()
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


def run_shell_capture(
    ctx: KasBuildContext,
    command: str,
    stdout_path: Path,
    *,
    step: str = "kas_shell_capture",
    python_executable: Path | None = None,
) -> int:
    """Run ``kas-container shell -c <command>`` with output captured to file.

    Sister to :func:`run_shell`. Same env+cwd plumbing via
    :func:`_build_env`; the only difference is that stdout and stderr
    are merged and redirected to ``stdout_path`` instead of inheriting
    the parent terminal. Returns the kas-container exit code.

    Used by :mod:`bakar.steps.stress_parse` to capture each
    ``bitbake -p`` iteration's output to its own log file for offline
    fork-race signature scanning.

    ``python_executable`` is forwarded to :func:`_build_env` so the
    kas shell's PATH and BB_PYTHON3 point at a caller-chosen interpreter
    (obmalloc-patch validation).
    """
    cfg, log, kas_yaml, overlay_source = ctx.cfg, ctx.log, ctx.kas_yaml, ctx.overlay_source
    log.step_start(step, command=command, stdout_path=str(stdout_path), host_mode=cfg.host_mode)
    kas_arg = _build_kas_arg(cfg, kas_yaml, overlay_source, ctx.extra_overlays)
    exe = "kas" if cfg.host_mode else "kas-container"
    cmd = [exe, *_ccache_args(cfg), "shell", kas_arg, "-c", command]
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("wb") as fh:
        proc = subprocess.Popen(
            cmd,
            cwd=cfg.bsp_root,
            env=_build_env(
                cfg,
                python_executable=python_executable,
                eventlog_path=_container_eventlog_path(cfg, log),
            ),
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
        rc = proc.wait()
    _finish_step(log, step, rc)
    return rc


def run_kas_subcommand(
    ctx: KasBuildContext, subcommand: str, extra_args: list[str], *, capture_to: Path | None = None
) -> int:
    """Run a kas subcommand (e.g. ``dump``, ``lock``) with overlay assembly.

    Sister to :func:`run_shell`/:func:`run_shell_capture`. Selects ``kas``
    vs ``kas-container`` from ``cfg.host_mode`` and layers the overlay in via
    the same colon-joined arg as :func:`run_build`. Used by ``bakar dump``
    (subcommand ``dump``) and the BYO path of ``bakar lock`` (subcommand
    ``lock``).

    When ``capture_to`` is a path, the subprocess stdout is redirected to that
    file so large ``kas dump`` output streams to disk instead of buffering in
    memory; when None, stdout inherits the parent terminal. Returns the kas
    exit code.
    """
    cfg, log, kas_yaml, overlay_source = ctx.cfg, ctx.log, ctx.kas_yaml, ctx.overlay_source
    log.step_start("kas_subcommand", subcommand=subcommand, host_mode=cfg.host_mode)
    kas_arg = _build_kas_arg(cfg, kas_yaml, overlay_source, ctx.extra_overlays)
    exe = "kas" if cfg.host_mode else "kas-container"
    cmd = [exe, *_ccache_args(cfg), subcommand, kas_arg, *extra_args]
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
                )
        else:
            proc = subprocess.run(  # pragma: no cover
                cmd,
                cwd=cfg.bsp_root,
                env=_build_env(cfg, ensure_hashserv=False, eventlog_path=_container_eventlog_path(cfg, log)),
                check=False,
            )
    except FileNotFoundError:
        log.step_fail("kas_subcommand", reason=f"{exe} not found")
        raise
    rc = proc.returncode
    if rc != 0:
        log.step_fail("kas_subcommand", reason=f"{subcommand} exited {rc}")
    else:
        log.step_ok("kas_subcommand", exit_code=rc)
    return rc
