"""Wrap the kas/bitbake build in a transient systemd user scope.

``bakar build`` (and the live ``bakar bitbake`` path) historically ran
kas/kas-container as a plain child of the interactive shell. Two problems
follow from that:

* **Session teardown kills the build.** Closing the terminal, an SSH
  disconnect, or the Claude Code harness reaping an idle background shell
  sends SIGHUP to the session; the build dies with it. The work lives in the
  caller's ``session-<n>.scope`` cgroup, which ``systemd-logind`` reaps when
  the session ends.
* **A runaway may need containment.** A build that balloons past physical RAM
  can drive the whole box into an OOM storm or swap-thrash death spiral. The
  scope makes cgroup containment *available*, but it is OFF by default and
  deliberately narrow: on a host with a large swap it does more harm than good
  (see the memory-ceiling note below), so it is opt-in for a dedicated build
  host that wants it.

Wrapping the invocation in ``systemd-run --user --scope`` fixes session
survival unconditionally (and makes containment opt-in) without changing what
the build itself does:

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
  They are opt-in because on a host with a large zram/zswap swap they backfire,
  two ways depending on the swap policy. With swap denied (the paired
  ``MemorySwapMax=0``), crossing ``MemoryHigh`` drives futile ``memory.high``
  reclaim on the build's unswappable, anon-heavy working set - the box can die
  in that reclaim band *before* ``MemoryMax`` is even reached (hence no
  OOM-kill line in the log). With swap allowed, ``MemoryMax`` (``memory.max``)
  caps only RAM-resident memory, so the build spills into zram - stored
  *compressed in RAM* - and the "hard ceiling" never bounds physical RAM.
  Either way the box reclaim/swap-thrashes; on one booted with
  ``softlockup_panic=1`` that thrash hard-locked and panicked the machine. A
  build that fit in RAM before the scope existed never needed a ceiling at all.
  Enable them only on a *dedicated* build host where OOM-killing the build to
  protect the host is the goal: set ``scope_memory_max`` (``MemoryHigh`` is a
  cushion that applies only alongside it), and the scope then also emits
  ``MemorySwapMax=0`` so the cap becomes a real RAM ceiling (a clean
  cgroup-OOM).
* ``oom_score_adjust`` (positive) - so under *global* memory pressure the
  kernel picks the build as the OOM victim and protects system services.
  This one is NOT a scope property: ``OOMScoreAdjust=`` belongs to the exec
  context (systemd.exec), which a scope unit - it adopts an already-running
  process rather than spawning one - cannot set. Instead the build is
  launched through a tiny ``sh -c 'echo N > /proc/self/oom_score_adj; exec
  "$@"'`` shim so the value is written before exec and inherited by every
  descendant (in host mode: bitbake, the workers, and every compiler).
* ``CPUWeight``/``IOWeight`` - **OFF by default**
  (``scope_cpu_weight``/``scope_io_weight`` default to ``0`` = omit). Setting
  either does NOT stay contained to this scope: systemd realizes the cpu/io
  cgroup controllers across the whole ``app.slice`` hierarchy and its siblings
  (``unit_get_target_mask`` = own | members | siblings). Under a heavy-I/O
  recipe (chromium, webkit, LTO links) the io controller's proportional
  throttling can trigger a priority-inversion stall that hangs the whole
  session with no OOM and no panic - confirmed on a build host: a chromium
  build stalled the box with these at 50 and ran clean (io PSI flat,
  ``app.slice`` never gaining cpu/io) with them at 0. Enable them only where
  you have measured contention that justifies the risk.

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
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

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
    build would collide with the still-running scope - an *idle* scope left
    holding only bitbake's cooker daemon is reclaimed first, see
    :func:`_reclaim_idle_scope`), while different
    workspaces/targets get distinct units and distinct
    ``journalctl --user -u <unit>`` streams. The path is hashed rather than
    embedded so the result is always a legal unit name regardless of the
    workspace path's characters. ``unit_suffix`` (``build`` vs ``bitbake``)
    keeps a full build and a recipe-level bitbake run from sharing a unit.

    The ``.scope`` suffix is part of the name and is not optional. ``systemd-run
    --scope`` infers the unit type from the flag, so a bare ``--unit=bakar-x``
    still creates ``bakar-x.scope`` - but every *other* tool resolves an
    unsuffixed name to ``.service``. An unsuffixed name therefore made
    ``systemctl show`` report ``LoadState=not-found`` for a scope that was
    loaded and active, silently defeating both :func:`_reclaim_idle_scope` and
    :func:`_reset_stale_scope` (each ``check=False``, so the "Unit
    bakar-x.service not loaded" error never surfaced), and made the
    ``journalctl`` hint printed at launch return "-- No entries --".
    """
    key = f"{cfg.bsp_root}\0{cfg.machine}".encode()
    digest = hashlib.sha256(key).hexdigest()[:10]
    return f"bakar-{unit_suffix}-{digest}.scope"


def _fraction_to_percent(fraction: float) -> int | None:
    """Convert a ``[0, 1]`` RAM fraction to a systemd percentage, or None to omit.

    systemd accepts ``MemoryHigh=``/``MemoryMax=`` as a percentage of physical
    RAM, which keeps the limit correct on any box without bakar computing byte
    counts. Returns None (omit the property) for a non-positive/out-of-range
    fraction AND for any positive fraction that rounds to ``0%``: a
    ``MemoryMax=0%`` is ``memory.max=0``, which OOM-kills the scope on its first
    allocation, so a sub-1% fraction is treated as "off" rather than "cap at
    zero" (``0.0`` is the documented disable value).
    """
    if fraction <= 0 or fraction > 1:
        return None
    return round(fraction * 100) or None


def _scope_properties(cfg: BuildConfig) -> list[str]:
    """Assemble the ``--property KEY=VALUE`` values for the scope's cgroup.

    Emits only resource-control properties (systemd.resource-control), the set a
    scope unit can carry. ``oom_score_adjust`` is handled separately via an exec
    shim (see the module docstring), not here. A CPU/IO weight of 0 omits that
    property.
    """
    props: list[str] = []
    # Memory containment is gated on MemoryMax: it is the single opt-in switch.
    # MemoryHigh is a soft cushion below the hard cap and is meaningful ONLY
    # paired with it - MemoryHigh alone (swap still available) just makes a zram
    # host reclaim-and-swap-thrash, the exact regime this feature defaults off -
    # so it is emitted only inside the MemoryMax block. A high-only config is
    # warned about and ignored in wrap_build_command.
    hard = _fraction_to_percent(cfg.scope_memory_max)
    if hard is not None:
        high = _fraction_to_percent(cfg.scope_memory_high)
        if high is not None:
            props.append(f"MemoryHigh={high}%")
        props.append(f"MemoryMax={hard}%")
        # Deny the build any swap so the MemoryMax cap is a REAL RAM ceiling.
        # Without it, crossing the cap spills the build's pages into swap instead
        # of OOM-killing it, and a zram swap stores them compressed in RAM, so
        # the cap never bounds physical RAM. Scoped to the MemoryMax opt-in on
        # purpose: emitting it unconditionally makes the build's anon
        # un-swappable even with no cap, which just shifts swap pressure onto the
        # desktop.
        props.append("MemorySwapMax=0")
    if cfg.scope_cpu_weight > 0:
        props.append(f"CPUWeight={cfg.scope_cpu_weight}")
    if cfg.scope_io_weight > 0:
        props.append(f"IOWeight={cfg.scope_io_weight}")
    return props


# cgroup v2 root and procfs root, injectable so the readers below are testable
# against a fake tree without a live systemd.
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_PROC_ROOT = Path("/proc")

# A scope whose cgroup holds ONLY processes matching this marker is idle, not
# building. bitbake's memory-resident cooker (``bitbake-server``) deliberately
# outlives its client - with ``BB_SERVER_TIMEOUT=0`` it never times out - so it
# alone keeps the cgroup non-empty, and therefore the unit `active running`,
# long after the build finished. Matching ``bitbake-server`` specifically (not a
# bare ``bitbake``) is what keeps a ``bitbake-worker`` or a ``bin/bitbake``
# client from reading as idle; the same UI-vs-server cmdline distinction
# ``build_stop`` already relies on.
_IDLE_COOKER_MARKER = "bitbake-server"

# How long to wait for a scope left behind by an interrupted build to drain
# before concluding it holds a genuinely concurrent build. Ctrl-C leaves the
# kas client and its parser processes (32 of them on this tree) dying for a few
# seconds; a real build never drains, so it still collides after this window.
_SCOPE_SETTLE_SECONDS = 15.0
_SCOPE_SETTLE_POLL = 0.5


def _proc_cmdline(pid: int, *, proc_root: Path = _PROC_ROOT) -> str | None:
    """Return ``pid``'s space-joined argv, or None when it cannot be read.

    None means "unidentifiable" (the process exited, or procfs is unreadable);
    callers must treat that as "assume busy" rather than "assume idle".
    """
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def _scope_cgroup_procs(unit: str, *, cgroup_root: Path = _CGROUP_ROOT) -> list[int] | None:
    """Return the PIDs inside ``unit``'s cgroup, or None when undeterminable.

    Resolves the unit's cgroup path via ``systemctl --user show -p ControlGroup``
    and reads ``cgroup.procs`` directly, rather than parsing ``systemd-cgls``
    tree-drawing output. Returns None - meaning "unknown", which callers MUST
    treat as busy - when systemctl is missing or errors, the unit is not loaded
    (empty ControlGroup), or the cgroup file is unreadable. An empty list is a
    real answer: the unit exists and holds no processes.
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "--property=ControlGroup", "--value", unit],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    # stdout may be None when a caller/test stubs subprocess.run with a bare
    # CompletedProcess; treat that as "unknown" rather than raising.
    relative = (result.stdout or "").strip()
    if not relative.startswith("/"):
        # Empty (unit not loaded) or an unexpected shape; do not guess.
        return None
    try:
        raw = (cgroup_root / relative.lstrip("/") / "cgroup.procs").read_text()
    except OSError:
        return None
    pids: list[int] = []
    for token in raw.split():
        try:
            pids.append(int(token))
        except ValueError:
            return None
    return pids


def _scope_is_idle(unit: str) -> bool:
    """True only when ``unit``'s cgroup holds nothing but idle bitbake cookers.

    Fails CLOSED: any process whose cmdline cannot be read, or that is not a
    ``bitbake-server``, makes this False so the caller leaves the scope alone.
    A genuinely concurrent build always has its ``kas``/``sh`` client (and
    usually ``bitbake-worker``s) in the cgroup for the whole run, so it can
    never be mistaken for idle - which is what preserves the deliberate
    same-config collision guard.
    """
    pids = _scope_cgroup_procs(unit)
    if pids is None:
        return False
    for pid in pids:
        cmdline = _proc_cmdline(pid)
        if cmdline is None or _IDLE_COOKER_MARKER not in cmdline:
            return False
    return True


def _scope_loaded(unit: str) -> bool:
    """True when systemd still has ``unit`` loaded, so creating it would collide.

    A transient scope stays loaded for a moment after its processes exit, and
    ``systemd-run --unit=<name>`` fails with "already loaded or has a fragment
    file" for the whole of that window. This is the gate that keeps the settle
    wait in :func:`_reclaim_idle_scope` free in the common case: when no unit is
    loaded there is nothing to collide with, so there is nothing to wait for.
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "--property=LoadState", "--value", unit],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    return (result.stdout or "").strip() == "loaded"


def _reclaim_idle_scope(
    unit: str,
    *,
    settle_timeout: float = _SCOPE_SETTLE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    notify: Callable[[str], None] | None = None,
) -> bool:
    """Stop ``unit`` once nothing but bitbake's persistent cooker holds it open.

    Complements :func:`_reset_stale_scope`, which flushes an inactive or failed
    unit but correctly declines to touch an `active` one. A finished
    ``bakar bitbake`` run leaves the scope active anyway, because the cooker
    daemon it spawned outlives the client and keeps the cgroup non-empty - so
    the next identical invocation collided with a scope that had no build in it.

    The wait matters as much as the check. Interrupting a build with Ctrl-C and
    immediately re-running it lands in a window where the *previous* build's
    ``kas`` client and its parser processes are still shutting down: they are
    genuinely alive, so the scope reads as busy, but they are a corpse rather
    than a concurrent build. Polling until the scope drains turns that into a
    successful launch, while a real concurrent build never drains and so still
    collides after ``settle_timeout`` - which is the deliberate guard, not a
    bug. The wait is gated on the unit actually being loaded, so an ordinary
    build with no scope present pays nothing.

    Reclaiming costs the cooker's in-memory parse cache (the next run
    re-parses), the same price the manual ``systemctl --user stop`` workaround
    pays, and only when the scope is provably idle.

    Returns True when the unit was stopped. Best-effort throughout: a missing
    systemctl or an unreadable cgroup leaves the scope untouched.
    """
    if not _scope_loaded(unit):
        return False

    deadline = clock() + settle_timeout
    announced = False
    while not _scope_is_idle(unit):
        if clock() >= deadline:
            # Still busy after the grace window: treat it as a real concurrent
            # build and let the launch collide, exactly as designed.
            return False
        if notify is not None and not announced:
            announced = True
            notify(f"scope {unit} is still draining from a previous run; waiting up to {settle_timeout:.0f}s")
        sleep(_SCOPE_SETTLE_POLL)
        if not _scope_loaded(unit):
            # It went away on its own mid-wait; nothing left to reclaim.
            return False

    try:
        subprocess.run(
            ["systemctl", "--user", "stop", unit],
            check=False,
            capture_output=True,
        )
    except OSError:
        return False
    return True


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
    (logged once as a warning, since it means the build loses session-survival,
    the OOM-victim bias, and the CPU/IO de-prioritisation). Otherwise prepends
    the ``systemd-run`` invocation with the resource-control properties and, when
    ``scope_oom_score_adjust`` is set, a ``sh -c`` shim that writes
    ``oom_score_adj`` before exec so every build descendant inherits it.

    ``--collect`` GCs the transient unit on a clean failure, but a hard-killed
    build can still leave the config-hash-named unit lingering, so
    :func:`_reset_stale_scope` flushes it first (see there), and
    :func:`_reclaim_idle_scope` runs ahead of that to release a scope left
    `active` by nothing but bitbake's persistent cooker; ``--quiet``
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
            "(no session-survival, no OOM-victim bias, no CPU/IO de-prioritisation). "
            "Install systemd's user manager or set `bakar settings set build.scope false` "
            "to silence this."
        )
        return cmd

    # MemoryHigh is a cushion below the MemoryMax cap and does nothing useful
    # without it; worse, on a zram host MemoryHigh alone (swap still available)
    # reclaim-thrashes. Gate containment on MemoryMax and tell the user their
    # high-only config is ignored (_scope_properties omits it).
    if cfg.scope_memory_high > 0 and cfg.scope_memory_max <= 0:
        log.warn(
            "scope_memory_high is set but scope_memory_max is not; MemoryHigh alone "
            "swap-thrashes a host with zram swap, so memory containment is left off. "
            "Set scope_memory_max to enable it."
        )

    unit = scope_unit_name(cfg, unit_suffix)
    # Order matters: reclaim the active-but-idle case first (a finished run whose
    # cooker daemon still holds the cgroup), then flush an inactive/failed unit.
    if _reclaim_idle_scope(unit, notify=log.info):
        log.info(
            f"reclaimed idle build scope {unit} (only bitbake's persistent cooker remained; the next run re-parses)"
        )
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
