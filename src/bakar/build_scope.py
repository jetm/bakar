"""Wrap the kas/bitbake build in a transient systemd user scope.

``bakar build`` (and the live ``bakar bitbake`` path) historically ran
kas/kas-container as a plain child of the interactive shell. Two problems
follow from that:

* **Session teardown kills the build.** Closing the terminal, an SSH
  disconnect, or the Claude Code harness reaping an idle background shell
  sends SIGHUP to the session; the build dies with it. The work lives in the
  caller's ``session-<n>.scope`` cgroup, which ``systemd-logind`` reaps when
  the session ends.
* **A runaway has no containment.** A build that balloons past physical RAM
  can drive the whole box into an OOM storm or swap-thrash death spiral,
  taking down PID 1 and the desktop with it rather than just the build.

Wrapping the invocation in ``systemd-run --user --scope`` fixes both without
changing what the build itself does:

* The scope is a transient unit under ``user@<uid>.service`` /
  ``app.slice`` - a *sibling* of the session scope, not a child - so it
  survives terminal/session teardown (``journalctl``/``systemctl --user``
  can still see and stop it after the shell is gone).
* ``--scope`` runs the command in the foreground, inheriting the caller's
  controlling TTY, full environment, and CWD, so the PTY-driven live UI,
  ``kas``/``docker``, ``sccache``, and every ``BAKAR_*``/``KAS_*`` env var
  keep working exactly as before.
* The scope's own cgroup carries safe resource controls (see below).

Resource controls (all configurable via ``~/.config/bakar/config.toml``
``[build]``; see :mod:`bakar.user_config`):

* ``MemoryHigh``/``MemoryMax`` - cgroup memory ceilings, **OFF by default**
  (``scope_memory_high``/``scope_memory_max`` default to ``0.0`` = omit).
  They are opt-in because on a host with a large zram/zswap swap they do more
  harm than good: ``MemoryMax`` (``memory.max``) caps only RAM-resident memory,
  so crossing it spills the cgroup's pages into swap - and zram stores them
  *compressed in RAM*, so the "hard ceiling" never bounds physical RAM.
  ``MemoryHigh`` then just forces reclaim on that unswappable/anon-heavy set,
  spinning CPUs in direct reclaim; on a box with ``softlockup_panic=1`` that
  can panic the whole machine, and on any workstation it swap-thrashes the
  desktop. A build that fit in RAM before the scope existed never needed a
  ceiling. Enable them (set a ``0<f<=1`` fraction) only on a *dedicated* build
  host where OOM-killing the build to protect the host is the goal; when
  ``MemoryMax`` is set the scope also emits ``MemorySwapMax=0`` so the cap
  becomes a real RAM ceiling (a clean cgroup-OOM instead of a zram thrash).
* ``oom_score_adjust`` (positive) - so under *global* memory pressure the
  kernel picks the build as the OOM victim and protects system services.
  This one is NOT a scope property: ``OOMScoreAdjust=`` belongs to the exec
  context (systemd.exec), which a scope unit - it adopts an already-running
  process rather than spawning one - cannot set. Instead the build is
  launched through a tiny ``sh -c 'echo N > /proc/self/oom_score_adj; exec
  "$@"'`` shim so the value is written before exec and inherited by every
  descendant (in host mode: bitbake, the workers, and every compiler).
* ``CPUWeight``/``IOWeight`` (below the default 100) - keep the host
  responsive under contention. These only bite when something else wants the
  CPU/IO, so they never slow an otherwise-idle build; set either to 0 to omit
  it.

Parallelism is deliberately untouched. ``BB_NUMBER_THREADS`` and
``PARALLEL_MAKE`` are NOT capped here: the root cause of a given runaway is
unknown per-build, and lowering parallelism would confound diagnosis. This
module only bounds the blast radius (containment) and the lifetime
(survival); it does not throttle the build's own concurrency.

Host mode vs container mode - the cgroup boundary is real and worth stating
plainly. In host mode (bakar's structural default) kas runs bitbake directly
as descendants of the scope, so ``MemoryMax`` genuinely caps the build. In
container mode the heavy work runs inside a ``docker``/``podman`` container
whose processes live in the *runtime's* cgroup, not this scope - so the scope
still delivers session-survival and journal visibility, but the memory
ceiling only bounds the lightweight ``kas-container``/``docker`` client, not
the container itself. A hard container memory cap would need ``docker run
--memory`` and is out of scope here.

Scope of the hardening, explicitly: by default this delivers session-survival
plus CPU/IO responsiveness and a positive OOM score; memory *containment*
(the ceilings above) is opt-in. It does NOT prevent - and is not intended to
prevent - the XFS-root-fs-corruption class of kernel panic that motivated
this work; that is a filesystem/kernel fault tracked separately, and no
amount of cgroup control changes it.

This module is foundation-tier: it imports nothing above :mod:`bakar.config`
(and that only for typing), so :mod:`bakar.steps.kas_build` can call it
without an upward edge.
"""

from __future__ import annotations

import functools
import hashlib
import os
import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bakar.config import BuildConfig
    from bakar.observability import RunLogger


# Bound the availability probe so a wedged user manager cannot hang the build.
_PROBE_TIMEOUT_SECS = 10


@functools.cache
def systemd_run_available() -> bool:
    """Return True when a ``systemd-run --user`` scope can actually be created.

    The ``systemd-run`` binary on PATH plus a user runtime dir
    (``XDG_RUNTIME_DIR``) is necessary but not sufficient: on WSL without
    systemd, a minimal/misconfigured container, or an SSH session with no live
    user manager, the binary is present yet ``--user`` cannot reach the manager
    bus, so wrapping would turn into a hard launch failure at build time. After
    the cheap checks pass, probe once with a throwaway ``--scope true`` so those
    environments fall back to an unwrapped launch (via
    :func:`wrap_build_command`) instead of failing the build.

    Cached because the answer cannot change within one process and the build
    path queries it more than once; the probe therefore runs at most once per
    process, and only when scoping is enabled (callers short-circuit on
    ``cfg.scope`` first).
    """
    if shutil.which("systemd-run") is None or not os.environ.get("XDG_RUNTIME_DIR"):
        return False
    try:
        result = subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", "--", "true"],
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SECS,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        # Binary vanished between the which() check and exec, or the probe timed
        # out (TimeoutExpired ⊂ SubprocessError) on a wedged manager.
        return False
    return result.returncode == 0


def scope_unit_name(cfg: BuildConfig, unit_suffix: str) -> str:
    """Return a stable, valid scope unit name for this workspace+target.

    Keyed on the effective BSP root plus the machine so two builds of the same
    target in the same tree share one unit name (a useful guard: a second such
    build would collide with the still-running scope), while different
    workspaces/targets get distinct units and distinct
    ``journalctl --user -u <unit>`` streams. The path is hashed rather than
    embedded so the result is always a legal unit name regardless of the
    workspace path's characters. ``unit_suffix`` (``build`` vs ``bitbake``)
    keeps a full build and a recipe-level bitbake run from sharing a unit.
    """
    key = f"{cfg.bsp_root}\0{cfg.machine}".encode()
    digest = hashlib.sha256(key).hexdigest()[:10]
    return f"bakar-{unit_suffix}-{digest}"


def _fraction_to_percent(fraction: float) -> int | None:
    """Convert a ``(0, 1]`` RAM fraction to a systemd percentage, or None to omit.

    systemd accepts ``MemoryHigh=``/``MemoryMax=`` as a percentage of physical
    RAM, which keeps the limit correct on any box without bakar computing byte
    counts. A non-positive or out-of-range fraction returns None so the caller
    omits the property entirely (leaving that control unset).
    """
    if fraction <= 0 or fraction > 1:
        return None
    return round(fraction * 100)


def _scope_properties(cfg: BuildConfig) -> list[str]:
    """Assemble the ``--property KEY=VALUE`` values for the scope's cgroup.

    Emits only resource-control properties (systemd.resource-control), the set a
    scope unit can carry. ``oom_score_adjust`` is handled separately via an exec
    shim (see the module docstring), not here. A CPU/IO weight of 0 omits that
    property.
    """
    props: list[str] = []
    high = _fraction_to_percent(cfg.scope_memory_high)
    if high is not None:
        props.append(f"MemoryHigh={high}%")
    hard = _fraction_to_percent(cfg.scope_memory_max)
    if hard is not None:
        props.append(f"MemoryMax={hard}%")
        # Deny the build any swap, but ONLY when a MemoryMax cap is opted in.
        # Without this, MemoryMax is defeated on a host with a large (zram) swap:
        # crossing the cap spills the build's pages into swap instead of
        # OOM-killing it, and zram stores them compressed in RAM, so the cap
        # never bounds physical RAM. Pinning swap to 0 makes the cap real. It is
        # scoped to the MemoryMax opt-in on purpose: emitting it unconditionally
        # makes the build's anon un-swappable even with no cap, which just shifts
        # global swap pressure onto the desktop.
        props.append("MemorySwapMax=0")
    if cfg.scope_cpu_weight > 0:
        props.append(f"CPUWeight={cfg.scope_cpu_weight}")
    if cfg.scope_io_weight > 0:
        props.append(f"IOWeight={cfg.scope_io_weight}")
    return props


def _reset_stale_scope(unit: str) -> None:
    """Flush a lingering transient scope named ``unit`` before re-creating it.

    ``--collect`` GCs the scope on a clean failure, but a hard-killed build
    (SIGKILL, an OOM, a 143 from a background-shell reaper) can leave the
    config-hash-named unit loaded - or its transient fragment on disk - so the
    next same-config build dies with "unit already loaded or has a fragment
    file" and zero bitbake events before a task runs. ``reset-failed`` flushes
    an inactive or failed unit (and its fragment) without disturbing an active
    one, so a genuinely concurrent same-config build still collides correctly
    while a dead scope no longer blocks the next run. Best-effort: a missing
    systemctl or an absent unit is a no-op.
    """
    try:
        subprocess.run(
            ["systemctl", "--user", "reset-failed", unit],
            check=False,
            capture_output=True,
        )
    except OSError:
        pass


def wrap_build_command(
    cmd: list[str],
    cfg: BuildConfig,
    log: RunLogger,
    *,
    unit_suffix: str,
) -> list[str]:
    """Return ``cmd`` wrapped in a transient ``systemd-run --user --scope``.

    Returns ``cmd`` unchanged when scoping is disabled (``[build] scope =
    false`` / ``--no-scope``) or when :func:`systemd_run_available` is False
    (logged once as a warning, since it means the build loses both the memory
    ceiling and session-survival). Otherwise prepends the ``systemd-run``
    invocation with the resource-control properties and, when
    ``scope_oom_score_adjust`` is set, a ``sh -c`` shim that writes
    ``oom_score_adj`` before exec so every build descendant inherits it.

    ``--collect`` GCs the transient unit on a clean failure, but a hard-killed
    build can still leave the config-hash-named unit lingering, so
    :func:`_reset_stale_scope` flushes it first (see there); ``--quiet``
    suppresses systemd-run's own "Running as unit" chatter (the
    live UI owns the terminal), with the unit name and its journal command
    logged to the run log instead.

    The wrapper preserves the launch contract ``bakar stop`` relies on:
    ``systemd-run --scope`` exec-chains into the command, so the ``Popen``
    PID stays the build's process-group leader (host-mode ``killpg`` still
    reaches it) and the command's argv keeps a ``kas``/``kas-container`` token
    for the ``/proc/<pgid>/cmdline`` identity check; container-mode stop is
    label-based and unaffected.
    """
    if not cfg.scope:
        return cmd
    if not systemd_run_available():
        log.warn(
            "systemd-run unavailable; running the build without a transient scope "
            "(no cgroup memory ceiling, no session-survival). Install systemd's "
            "user manager or set `bakar settings set build.scope false` to silence this."
        )
        return cmd

    unit = scope_unit_name(cfg, unit_suffix)
    _reset_stale_scope(unit)
    prefix = ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}"]
    for prop in _scope_properties(cfg):
        prefix += ["--property", prop]

    inner = cmd
    if cfg.scope_oom_score_adjust > 0:
        # OOMScoreAdjust is an exec-context property a scope unit cannot set, so
        # write oom_score_adj before exec; every descendant inherits it.
        shim = f'echo {cfg.scope_oom_score_adjust} > /proc/self/oom_score_adj; exec "$@"'
        inner = ["sh", "-c", shim, "sh", *inner]

    log.info(f"systemd scope: {unit} (journal: journalctl --user -u {unit})")
    return [*prefix, "--", *inner]


# The user-manager bus vars systemd-run --user needs to reach the transient-unit
# API. _build_env deliberately hands the child a curated env that omits them, so
# a scoped launch must add them back or systemd-run fails with "Failed to connect
# to user scope bus ... $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined".
_SCOPE_BUS_ENV_KEYS = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def scope_env(env: dict[str, str], cfg: BuildConfig) -> dict[str, str]:
    """Return ``env`` augmented with the user-bus vars ``systemd-run --user`` needs.

    Returns ``env`` unchanged (same object) when scoping is disabled or
    unavailable - matching :func:`wrap_build_command`'s gate so the env is only
    touched when the command is actually scoped. Otherwise returns a copy with
    ``XDG_RUNTIME_DIR``/``DBUS_SESSION_BUS_ADDRESS`` copied from the current
    process environment (only when present and not already set), so the curated
    build env from ``_build_env`` still reaches the user manager.
    """
    if not cfg.scope or not systemd_run_available():
        return env
    augmented = dict(env)
    for key in _SCOPE_BUS_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None and key not in augmented:
            augmented[key] = value
    return augmented
