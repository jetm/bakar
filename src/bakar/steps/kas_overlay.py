"""Overlay and local.conf materialization for the kas build step.

Split out of :mod:`bakar.steps.kas_build` (task 10.2). Every symbol here is
re-exported at ``bakar.steps.kas_build`` so existing import sites and test
monkeypatches keep working unchanged.

``_derive_parallelism_plan``'s cluster probe goes through
``bakar.steps.kas_build.probe_cluster`` via a function-body deferred import,
not a module-level one (which would create an import cycle with
``kas_build``), because the test suite monkeypatches ``kas_build.probe_cluster``
and expects that patch to reach this call.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from bakar import sccache_server, tuning
from bakar.config import _overlay_dir

if TYPE_CHECKING:
    from bakar.config import BuildConfig


# Overlay materialization: the kas-container bind-mount only includes
# ``KAS_WORK_DIR`` (= bsp_root) as ``/work``. Copying the overlay
# under ``<bsp_root>/.bakar/overlays/`` puts it inside that mount so
# the ``<user-yml>:<overlay>`` colon-joined arg resolves cleanly from
# the container's perspective.
_OVERLAY_DIR_RELPATH = Path(".bakar") / "overlays"

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
        # Deferred: see the module docstring for why this is not a top-level import.
        from bakar.steps import kas_build

        try:
            report = kas_build.probe_cluster(cfg.sccache_scheduler_url)
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


def _append_local_conf_lines(text: str, lines: list[str]) -> str:
    """Append ``lines`` to ``text``'s ``local_conf_header`` block, indented to
    match the block's own ``INHERIT`` line (or 4 spaces if none is found).

    Pure (no filesystem side effects). Callers own idempotency - each line
    already present in ``text`` must be filtered out before calling, since this
    always appends whatever it is given. Returns ``text`` unchanged when
    ``lines`` is empty."""
    if not lines:
        return text
    m = re.search(r"^(?P<indent>[ \t]+)INHERIT\b", text, re.MULTILINE)
    indent = m.group("indent") if m else "    "
    addition = "".join(f"{indent}{line}\n" for line in lines)
    return text.rstrip("\n") + "\n" + addition


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
    return _append_local_conf_lines(text, lines)


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
    return _append_local_conf_lines(text, [f'export SCCACHE_SERVER_UDS = "{uds}"'])


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


# The per-build link-timing log the mold overlay's wrappers append to. It must
# live under the kas bind mount (only KAS_WORK_DIR = <base> is mounted /work in
# container mode), so the path is delivered as an exported literal baked into the
# overlay - kas scrubs env passthrough, so a BAKAR_MOLD_LINKLOG env var would
# reach the daemon empty (see _inject_literal_sccache for the same problem).
_MOLD_LINKLOG_NAME = "mold-linklog.jsonl"


def _inject_literal_mold(cfg: BuildConfig, text: str) -> str:
    """Bake the mold link-log path and non-default ``MOLD_MODE`` into the overlay.

    Two literals are appended to the mold overlay's ``local_conf_header`` block:

    * ``export BAKAR_MOLD_LINKLOG`` - the per-build link-timing log. Mirrors
      :func:`_inject_literal_ccache`'s host/container dual path: the log lands
      under KAS_WORK_DIR (``cfg.workspace`` for meta-avocado, else
      ``cfg.bsp_root``) so it is inside the ``/work`` bind mount, written as the
      absolute host path in host mode and as ``/work/<name>`` in container mode.
    * ``MOLD_MODE`` - emitted only when ``cfg.mold_mode`` is not ``list``. The
      bbclass carries ``MOLD_MODE ??= "list"``, so list is already the default
      and needs no line; ``baseline`` (the symmetric bfd measurement arm) and
      ``global`` are unreachable unless the mode is written into local.conf here.

    Each line is guarded so re-running the injector no-ops (idempotent) and it is
    pure (no filesystem side effects), so dry-run rendering is safe."""
    lines = []
    if cfg.mold_mode != "list" and not re.search(r"^\s*MOLD_MODE\b", text, re.MULTILINE):
        lines.append(f'MOLD_MODE = "{cfg.mold_mode}"')
    if not re.search(r"^\s*export\s+BAKAR_MOLD_LINKLOG\b", text, re.MULTILINE):
        base = cfg.workspace if cfg.is_meta_avocado else cfg.bsp_root
        log_path = str(base / _MOLD_LINKLOG_NAME) if cfg.host_mode else f"/work/{_MOLD_LINKLOG_NAME}"
        lines.append(f'export BAKAR_MOLD_LINKLOG = "{log_path}"')
    return _append_local_conf_lines(text, lines)


def _inject_local_tmpdir(cfg: BuildConfig, text: str) -> str:
    """Append a literal ``TMPDIR`` assignment to the main tuning overlay's
    ``local_conf_header`` block, redirecting the build tmp to node-local disk.

    Fires only when ``local_tmpdir_base`` is set AND the build is host mode -
    otherwise returns ``text`` unchanged so an unset-knob build is byte-for-byte
    identical to today. ``cfg.resolved_tmpdir`` is itself host-mode-gated, but
    the no-op is enforced here independently so the injector cannot ever emit a
    workspace-relative ``TMPDIR`` line for a build that never asked for one.

    bitbake reads ``TMPDIR`` from ``local.conf`` (weak ``?=`` default in
    bitbake.conf, overridden by the hard local.conf assignment), so the
    ``local_conf_header`` channel is the reliable one - the same one
    :func:`_inject_literal_sccache` uses; ``TMPDIR`` is not a kas-read variable
    and the env passthrough allowlist never forwards it.

    Wired into the MAIN tuning overlay only (not the sccache/ccache/mold
    extras), since kas merges every overlay's header block and injecting into
    the shared chain would emit N duplicate ``TMPDIR`` lines. A regex guard
    keeps a re-materialize from doubling the line (mirroring
    :func:`_inject_literal_sccache`)."""
    if not cfg.local_tmpdir_base or not cfg.host_mode:
        return text
    if re.search(r"^\s*TMPDIR\s*=", text, re.MULTILINE):
        return text
    tmpdir = str(cfg.resolved_tmpdir)
    # The value lands inside a quoted bitbake assignment. A quote, backslash, or
    # control char in local_tmpdir_base/machine would terminate the string and
    # inject arbitrary local.conf statements (or make the overlay unparsable).
    # Fail fast on the misconfiguration rather than emitting a broken conf.
    if any(c in tmpdir for c in '"\\\n\r\x00'):
        raise ValueError(
            f"local_tmpdir_base resolves to a path unsafe for local.conf injection "
            f"(contains a quote, backslash, or control character): {tmpdir!r}"
        )
    return _append_local_conf_lines(text, [f'TMPDIR = "{tmpdir}"'])


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


def materialize_overlay(cfg: BuildConfig, overlay_source: Path, *, is_main_overlay: bool = False) -> Path:
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

    ``is_main_overlay`` gates the ``TMPDIR`` injection to the single main
    tuning overlay (the ``overlay_source`` from ``KasBuildContext``), never the
    sccache/ccache/mold extras, so the merged local.conf carries exactly one
    ``TMPDIR`` line.
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
        if overlay_source.name == "bakar-tuning-mold.yml":
            injected = _inject_literal_mold(cfg, injected)
        # TMPDIR goes on the main overlay only: kas merges every overlay's
        # local_conf_header block, so injecting into the shared chain would emit
        # one duplicate TMPDIR line per extra overlay.
        if is_main_overlay:
            injected = _inject_local_tmpdir(cfg, injected)
        if injected != original:
            dest.write_text(injected, encoding="utf-8")
    return dest.relative_to(cfg.bsp_root)


# The bakar-provided layers (e.g. classes/sccache.bbclass) are materialized
# next to the overlays under ``.bakar/`` so kas can add them via each tuning
# overlay's ``repos:`` entry. The relative repos path ``.bakar/<name>`` resolves
# against ``KAS_WORK_DIR`` in both host mode (build CWD) and container mode
# (``/work``), mirroring :func:`materialize_overlay`.
_SCCACHE_LAYER_NAME = "meta-bakar-sccache"
_HOST_LAYER_NAME = "meta-bakar-host"
_CACHE_CLASSIFY_LAYER_NAME = "meta-bakar-cache-classify"
_MOLD_LAYER_NAME = "meta-bakar-mold"


def materialize_layer(cfg: BuildConfig, name: str) -> Path:
    """Copy the bundled ``<name>`` layer into ``<base>/.bakar/`` and return the dest.

    ``base`` is the workspace for meta-avocado and ``bsp_root`` for every other
    family: kas resolves the overlay's relative ``.bakar/<name>`` repos path
    against KAS_WORK_DIR (see ``_build_env``), and meta-avocado points
    KAS_WORK_DIR at the workspace while ``bsp_root`` is the nested
    ``workspace/build-<stem>`` dir, so the layer must land under the workspace
    ``.bakar``; for every other family KAS_WORK_DIR == ``bsp_root`` and the two
    coincide.

    Overwrites on every call so the layer tracks the packaged source
    byte-for-byte. Returns the destination directory (not a ``bsp_root``-relative
    path, unlike :func:`materialize_overlay`), which each tuning overlay
    references by the relative path ``.bakar/<name>``.
    """
    source = _overlay_dir() / name
    base = cfg.workspace if cfg.is_meta_avocado else cfg.bsp_root
    dest = base / ".bakar" / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    return dest


def materialize_sccache_layer(cfg: BuildConfig) -> Path:
    """Materialize the ``meta-bakar-sccache`` layer (see :func:`materialize_layer`)."""
    return materialize_layer(cfg, _SCCACHE_LAYER_NAME)


def materialize_host_layer(cfg: BuildConfig) -> Path:
    """Materialize the ``meta-bakar-host`` layer (see :func:`materialize_layer`).

    Only invoked in host mode, where the layer's rpm bbappend keeps rpm-native
    from dlopening the build host's rpm transaction plugins.
    """
    return materialize_layer(cfg, _HOST_LAYER_NAME)


def materialize_cache_classify_layer(cfg: BuildConfig) -> Path:
    """Materialize the ``meta-bakar-cache-classify`` layer (see :func:`materialize_layer`).

    Unlike the gated layers, this one is called unconditionally at every call
    site - the overlay itself is the single unconditional entry in
    ``_tuning_extra_overlays`` (every build gets the cache-backend classification
    emitter, not just sccache-dist/host-mode builds).
    """
    return materialize_layer(cfg, _CACHE_CLASSIFY_LAYER_NAME)
