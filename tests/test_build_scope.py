"""Tests for the transient-systemd-scope build wrapper (``bakar.build_scope``).

Cover the pure assembly of ``systemd-run --user --scope`` argv (properties,
oom shim, unit naming, opt-out, and the unavailable fallback) and the wiring
into ``run_build`` / ``run_shell_live`` that scopes the real build command.
The subprocess is never launched: the module functions are pure, and the
integration tests stub ``_run_pty_with_ui`` to capture the argv it would run.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

import bakar.steps.kas_build as step_kas
from bakar import build_scope
from bakar.config import BuildConfig
from bakar.observability import RunLogger
from bakar.steps.kas_build import KasBuildContext, _PtyOutcome

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


class _FakeLog:
    """Captures ``warn``/``info`` so the wrapper's logging can be asserted."""

    def __init__(self) -> None:
        self.warns: list[str] = []
        self.infos: list[str] = []

    def warn(self, msg: str) -> None:
        self.warns.append(msg)

    def info(self, msg: str) -> None:
        self.infos.append(msg)


def _cfg(workspace: Path, **overrides: object) -> BuildConfig:
    base = BuildConfig(
        workspace=workspace,
        bsp_family="generic",
        machine="qemux86-64",
        distro="generic",
        image="generic",
        manifest="",
        repo_url="https://example.invalid/repo.git",
        repo_branch="",
        kas_container_image="jetm/kas-build-env:latest",
    )
    return replace(base, **overrides) if overrides else base


@pytest.fixture(autouse=True)
def _force_systemd_available(monkeypatch: pytest.MonkeyPatch):
    """Default to systemd-run being available so wrap tests are host-independent.

    ``systemd_run_available`` is ``functools.cache``d; clear it and stub the
    inputs it reads (binary, runtime dir, and the throwaway probe) so tests do
    not depend on the host having systemd and never create a real scope. Clear
    again on teardown so the True computed under these stubs does not leak past
    this module (a latent pytest-randomly hazard).
    """
    # Capture the real cached function now (before any test replaces the module
    # attribute with a stub lambda) so teardown can clear its cache regardless.
    real = build_scope.systemd_run_available
    real.cache_clear()
    monkeypatch.setattr(build_scope.shutil, "which", lambda _name: "/usr/bin/systemd-run")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setattr(
        build_scope.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 0),
    )
    yield
    real.cache_clear()


# ---------------------------------------------------------------------------
# systemd_run_available
# ---------------------------------------------------------------------------


def test_available_true_when_binary_and_runtime_dir_present() -> None:
    build_scope.systemd_run_available.cache_clear()
    assert build_scope.systemd_run_available() is True


def test_unavailable_without_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    build_scope.systemd_run_available.cache_clear()
    monkeypatch.setattr(build_scope.shutil, "which", lambda _name: None)
    assert build_scope.systemd_run_available() is False


def test_unavailable_without_runtime_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    build_scope.systemd_run_available.cache_clear()
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert build_scope.systemd_run_available() is False


def test_unavailable_when_probe_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # Binary + XDG present but the user manager is dead (WSL / minimal container):
    # the throwaway `systemd-run --user --scope true` exits non-zero.
    build_scope.systemd_run_available.cache_clear()
    monkeypatch.setattr(
        build_scope.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 1),
    )
    assert build_scope.systemd_run_available() is False


def test_unavailable_when_probe_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    build_scope.systemd_run_available.cache_clear()

    def _boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd="systemd-run", timeout=10)

    monkeypatch.setattr(build_scope.subprocess, "run", _boom)
    assert build_scope.systemd_run_available() is False


# ---------------------------------------------------------------------------
# scope_unit_name
# ---------------------------------------------------------------------------


def test_unit_name_stable_per_workspace_target(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert build_scope.scope_unit_name(cfg, "build") == build_scope.scope_unit_name(cfg, "build")


def test_unit_name_distinct_per_suffix(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert build_scope.scope_unit_name(cfg, "build") != build_scope.scope_unit_name(cfg, "bitbake")


def test_unit_name_distinct_per_machine(tmp_path: Path) -> None:
    a = _cfg(tmp_path, machine="qemux86-64")
    b = _cfg(tmp_path, machine="imx8mp-var-dart")
    assert build_scope.scope_unit_name(a, "build") != build_scope.scope_unit_name(b, "build")


def test_unit_name_is_legal_charset(tmp_path: Path) -> None:
    # Even with a path that has characters illegal in a unit name, the hash keeps
    # the result legal (letters, digits, hyphen).
    weird = tmp_path / "has spaces & colons:"
    weird.mkdir()
    name = build_scope.scope_unit_name(_cfg(weird), "build")
    assert name.startswith("bakar-build-")
    assert all(c.isalnum() or c == "-" for c in name)


# ---------------------------------------------------------------------------
# wrap_build_command
# ---------------------------------------------------------------------------

_CMD = ["kas-container", "build", "foo.yml:bar.yml"]


def test_wrap_disabled_returns_cmd_unchanged(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, scope=False)
    log = _FakeLog()
    assert build_scope.wrap_build_command(_CMD, cfg, log, unit_suffix="build") == _CMD
    assert log.warns == []
    assert log.infos == []


def test_wrap_unavailable_returns_cmd_and_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_scope.systemd_run_available.cache_clear()
    monkeypatch.setattr(build_scope.shutil, "which", lambda _name: None)
    cfg = _cfg(tmp_path)
    log = _FakeLog()
    assert build_scope.wrap_build_command(_CMD, cfg, log, unit_suffix="build") == _CMD
    assert len(log.warns) == 1
    assert "systemd-run unavailable" in log.warns[0]


def test_wrap_builds_scope_prefix_and_properties(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    log = _FakeLog()
    out = build_scope.wrap_build_command(_CMD, cfg, log, unit_suffix="build")
    unit = build_scope.scope_unit_name(cfg, "build")
    assert out[:6] == ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}"]
    # All resource controls are OFF by default: memory ceilings swap-thrash /
    # soft-lock a zram host, and CPU/IO weights realize the cpu/io cgroup
    # controllers session-wide (which stalled the box under chromium's I/O). So
    # a default scope emits NO --property flags at all - only survival + oom.
    joined = " ".join(out)
    assert "MemoryHigh" not in joined
    assert "MemoryMax" not in joined
    assert "MemorySwapMax" not in joined
    assert "CPUWeight" not in joined
    assert "IOWeight" not in joined
    # The original kas command is preserved as the tail.
    assert out[-3:] == _CMD
    # Journal hint logged so the run log records where to find the scope.
    assert any(unit in line and "journalctl" in line for line in log.infos)


def test_wrap_controller_weights_opt_in_emits_them(tmp_path: Path) -> None:
    # Weights are off by default; explicitly setting them still emits the
    # CPUWeight=/IOWeight= properties for the rare host where measured
    # contention justifies the session-wide controller realization.
    cfg = _cfg(tmp_path, scope_cpu_weight=50, scope_io_weight=50)
    joined = " ".join(build_scope.wrap_build_command(_CMD, cfg, _FakeLog(), unit_suffix="build"))
    assert "CPUWeight=50" in joined
    assert "IOWeight=50" in joined


def test_wrap_memory_ceiling_opt_in_emits_cap_and_swap_deny(tmp_path: Path) -> None:
    # Enabling MemoryMax emits the cap AND MemorySwapMax=0 so the cap is a real
    # RAM ceiling (a clean cgroup-OOM), not one defeated by zram. MemoryHigh
    # rides along when also set.
    cfg = _cfg(tmp_path, scope_memory_high=0.85, scope_memory_max=0.90)
    joined = " ".join(build_scope.wrap_build_command(_CMD, cfg, _FakeLog(), unit_suffix="build"))
    assert "MemoryHigh=85%" in joined
    assert "MemoryMax=90%" in joined
    assert "MemorySwapMax=0" in joined


def test_wrap_no_swap_deny_when_ceilings_off(tmp_path: Path) -> None:
    # Default (ceilings off): the build must NOT be denied swap - unconditional
    # MemorySwapMax=0 with no cap just shifts swap pressure onto the desktop.
    joined = " ".join(build_scope.wrap_build_command(_CMD, _cfg(tmp_path), _FakeLog(), unit_suffix="build"))
    assert "MemorySwapMax" not in joined


def test_wrap_resets_stale_scope_before_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """wrap must reset-failed the config-hash-named unit before launching.

    ``--collect`` GCs a scope on clean failure, but a hard-killed build (SIGKILL,
    OOM, a 143 from a reaper) can leave the unit loaded, so the next same-config
    run dies with "unit already loaded or has a fragment file" and 0 bitbake
    events. reset-failed flushes the dead unit first so the next run proceeds.
    """
    monkeypatch.setattr(build_scope, "systemd_run_available", lambda: True)
    calls: list[list[str]] = []

    def _record(argv: list[str], *_a: object, **_k: object) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(build_scope.subprocess, "run", _record)
    cfg = _cfg(tmp_path)
    unit = build_scope.scope_unit_name(cfg, "build")

    build_scope.wrap_build_command(_CMD, cfg, _FakeLog(), unit_suffix="build")

    assert ["systemctl", "--user", "reset-failed", unit] in calls


def test_wrap_sets_oom_via_exec_shim(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, scope_oom_score_adjust=750)
    out = build_scope.wrap_build_command(_CMD, cfg, _FakeLog(), unit_suffix="build")
    sep = out.index("--")
    inner = out[sep + 1 :]
    # OOMScoreAdjust is not a scope property (rejected by systemd); it is applied
    # via an inherited oom_score_adj written by an sh shim before exec.
    assert "OOMScoreAdjust" not in " ".join(out)
    assert inner[0] == "sh"
    assert inner[1] == "-c"
    assert "echo 750 > /proc/self/oom_score_adj" in inner[2]
    assert 'exec "$@"' in inner[2]
    assert inner[-3:] == _CMD


def test_wrap_no_shim_when_oom_zero(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, scope_oom_score_adjust=0)
    out = build_scope.wrap_build_command(_CMD, cfg, _FakeLog(), unit_suffix="build")
    sep = out.index("--")
    assert out[sep + 1 :] == _CMD  # no sh shim, kas command runs directly


def test_wrap_omits_zero_weights(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, scope_cpu_weight=0, scope_io_weight=0, scope_memory_max=0.90)
    out = build_scope.wrap_build_command(_CMD, cfg, _FakeLog(), unit_suffix="build")
    joined = " ".join(out)
    assert "CPUWeight" not in joined
    assert "IOWeight" not in joined
    assert "MemoryMax=90%" in joined  # opted-in memory ceiling still applied


def test_wrap_zero_disables_and_one_is_full_ram(tmp_path: Path) -> None:
    # 0.0 is the in-range disable value (property omitted); 1.0 is the max.
    cfg = _cfg(tmp_path, scope_memory_high=0.0, scope_memory_max=1.0)
    out = build_scope.wrap_build_command(_CMD, cfg, _FakeLog(), unit_suffix="build")
    joined = " ".join(out)
    assert "MemoryHigh" not in joined  # 0.0 disabled
    assert "MemoryMax=100%" in joined  # 1.0 == total RAM
    assert "MemorySwapMax=0" in joined  # cap opted in -> swap denied


def test_wrap_high_only_is_ignored_and_warns(tmp_path: Path) -> None:
    # MemoryHigh without MemoryMax is the harmful zram regime: gate it on the
    # cap. High-only emits no memory property and logs a warning.
    cfg = _cfg(tmp_path, scope_memory_high=0.85, scope_memory_max=0.0)
    log = _FakeLog()
    joined = " ".join(build_scope.wrap_build_command(_CMD, cfg, log, unit_suffix="build"))
    assert "MemoryHigh" not in joined
    assert "MemoryMax" not in joined
    assert "MemorySwapMax" not in joined
    assert any("scope_memory_high is set but scope_memory_max" in w for w in log.warns)


def test_wrap_sub_percent_fraction_omitted(tmp_path: Path) -> None:
    # A positive fraction that rounds to 0% must be omitted, not emitted as
    # MemoryMax=0% (which is memory.max=0 - an instant OOM of the scope).
    cfg = _cfg(tmp_path, scope_memory_max=0.004)
    joined = " ".join(build_scope.wrap_build_command(_CMD, cfg, _FakeLog(), unit_suffix="build"))
    assert "MemoryMax" not in joined
    assert "MemorySwapMax" not in joined


def test_parallelism_never_touched(tmp_path: Path) -> None:
    # The whole point: containment must not cap concurrency.
    cfg = _cfg(tmp_path)
    joined = " ".join(build_scope.wrap_build_command(_CMD, cfg, _FakeLog(), unit_suffix="build"))
    assert "BB_NUMBER_THREADS" not in joined
    assert "PARALLEL_MAKE" not in joined


# ---------------------------------------------------------------------------
# scope_env
# ---------------------------------------------------------------------------


def test_scope_env_adds_bus_vars_when_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    # Mimic _build_env's curated output, which omits the session bus vars.
    curated = {"PATH": "/usr/bin", "HOME": "/home/x"}
    out = build_scope.scope_env(curated, _cfg(tmp_path))
    assert out["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert out["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"
    assert out["PATH"] == "/usr/bin"  # curated keys preserved


def test_scope_env_unchanged_when_scope_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    curated = {"PATH": "/usr/bin"}
    out = build_scope.scope_env(curated, _cfg(tmp_path, scope=False))
    assert out is curated  # same object, untouched


def test_scope_env_unchanged_when_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_scope.systemd_run_available.cache_clear()
    monkeypatch.setattr(build_scope.shutil, "which", lambda _name: None)
    curated = {"PATH": "/usr/bin"}
    out = build_scope.scope_env(curated, _cfg(tmp_path))
    assert out is curated


def test_scope_env_does_not_override_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    curated = {"XDG_RUNTIME_DIR": "/already/set"}
    out = build_scope.scope_env(curated, _cfg(tmp_path))
    assert out["XDG_RUNTIME_DIR"] == "/already/set"


# ---------------------------------------------------------------------------
# Integration: run_build / run_shell_live apply the wrapper
# ---------------------------------------------------------------------------


def _run_build_ctx(tmp_path: Path, log: RunLogger, **cfg_overrides: object) -> KasBuildContext:
    cfg = _cfg(tmp_path, **cfg_overrides)
    bsp_root = cfg.bsp_root
    bsp_root.mkdir(parents=True, exist_ok=True)
    kas_yaml = bsp_root / "build.yml"
    kas_yaml.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    overlay = bsp_root / "overlay.yml"
    overlay.write_text("header:\n  version: 14\n")
    return KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)


def _capture_run_build_cmd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **cfg_overrides: object) -> list[str]:
    captured: list[list[str]] = []

    def fake_pty(cmd, *_a, **_kw):  # type: ignore[no-untyped-def]
        captured.append(cmd)
        return _PtyOutcome(rc=0)

    monkeypatch.setattr(
        step_kas, "clear_stale_bitbake_locks", lambda cfg: step_kas.build_stop.LockClearOutcome(removed=[])
    )
    monkeypatch.setattr(step_kas.build_stop, "check_unclean_stop", lambda *a, **kw: None)
    monkeypatch.setattr(step_kas, "persist_run_artifacts", lambda *a, **kw: None)
    monkeypatch.setattr(step_kas, "_run_pty_with_ui", fake_pty)

    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = _run_build_ctx(tmp_path, log, **cfg_overrides)
        rc = step_kas.run_build(ctx)
    assert rc == 0
    assert captured, "run_build never called _run_pty_with_ui"
    return captured[0]


def test_run_build_scopes_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd = _capture_run_build_cmd(tmp_path, monkeypatch)
    assert cmd[0] == "systemd-run", f"build command was not scoped: {cmd!r}"
    assert "--scope" in cmd
    # The kas invocation still ends the argv, so the build itself is unchanged.
    assert "build" in cmd


def test_run_build_unscoped_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd = _capture_run_build_cmd(tmp_path, monkeypatch, scope=False)
    assert cmd[0] != "systemd-run"
    assert cmd[0] in ("kas", "kas-container")


def test_run_shell_live_scopes_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    def fake_pty(cmd, *_a, **_kw):  # type: ignore[no-untyped-def]
        captured.append(cmd)
        return _PtyOutcome(rc=0)

    monkeypatch.setattr(step_kas, "_run_pty_with_ui", fake_pty)
    monkeypatch.setattr(step_kas, "persist_run_artifacts", lambda *a, **kw: None)

    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = _run_build_ctx(tmp_path, log)
        rc = step_kas.run_shell_live(ctx, "bitbake core-image-minimal")
    assert rc == 0
    assert captured[0][0] == "systemd-run", f"bitbake command was not scoped: {captured[0]!r}"
    assert "bakar-bitbake-" in " ".join(captured[0])


# ---------------------------------------------------------------------------
# idle-scope reclaim (bitbake's persistent cooker holding a finished scope open)
# ---------------------------------------------------------------------------
#
# Regression: `bakar bitbake <recipe>` succeeded, then the identical re-run died
# in ~0.3s with "Unit bakar-bitbake-<hash>.scope was already loaded". The scope
# stayed `active running` because bitbake's memory-resident cooker
# (bitbake-server, BB_SERVER_TIMEOUT=0) outlives its client and was the only
# process left in the cgroup - so `reset-failed` correctly declined to touch an
# active unit and the collision stood. Observed cgroup at the time held exactly
# one pid whose cmdline was ".../bin/bitbake-server ... bitbake.sock 0 0 None 0".

_SERVER_CMDLINE = (
    "/opt/buildtools/sysroots/x86_64-pokysdk-linux/usr/bin/python3 "
    "/ws/bitbake/bin/bitbake-server decafbad 3 5 /ws/build/bitbake-cookerdaemon.log "
    "/ws/build/bitbake.lock /ws/build/bitbake.sock 0 0 None 0"
)
_CLIENT_CMDLINE = "kas shell /ws/avocado-bakar.yml -c bitbake chromium-ozone-wayland"
_WORKER_CMDLINE = "/ws/bitbake/bin/bitbake-worker decafbad"


def _fake_cgroup(monkeypatch: pytest.MonkeyPatch, procs: dict[int, str] | None) -> None:
    """Point the idle probe at a fake cgroup: pid -> cmdline, or None = unknown."""
    monkeypatch.setattr(
        build_scope,
        "_scope_cgroup_procs",
        lambda _unit: None if procs is None else sorted(procs),
    )
    monkeypatch.setattr(build_scope, "_proc_cmdline", lambda pid: procs.get(pid) if procs else None)


def test_scope_idle_when_only_bitbake_server_remains(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cgroup holding just the cooker daemon is idle and reclaimable."""
    _fake_cgroup(monkeypatch, {689374: _SERVER_CMDLINE})

    assert build_scope._scope_is_idle("bakar-bitbake-deadbeef") is True


def test_scope_not_idle_when_live_client_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constraint 1/2: a genuinely running build must never read as idle.

    The kas client lives for the whole build, so its presence alongside the
    server is what keeps a concurrent same-config build colliding correctly
    instead of being reaped.
    """
    _fake_cgroup(monkeypatch, {100: _CLIENT_CMDLINE, 689374: _SERVER_CMDLINE})

    assert build_scope._scope_is_idle("bakar-bitbake-deadbeef") is False


def test_scope_not_idle_when_worker_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bitbake-worker means tasks are executing - not idle."""
    _fake_cgroup(monkeypatch, {689374: _SERVER_CMDLINE, 689400: _WORKER_CMDLINE})

    assert build_scope._scope_is_idle("bakar-bitbake-deadbeef") is False


def test_scope_not_idle_when_cmdline_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: an unidentifiable process is assumed busy, never reaped."""
    monkeypatch.setattr(build_scope, "_scope_cgroup_procs", lambda _unit: [4242])
    monkeypatch.setattr(build_scope, "_proc_cmdline", lambda _pid: None)

    assert build_scope._scope_is_idle("bakar-bitbake-deadbeef") is False


def test_scope_not_idle_when_cgroup_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit absent / systemctl unavailable -> unknown -> assumed busy."""
    _fake_cgroup(monkeypatch, None)

    assert build_scope._scope_is_idle("bakar-bitbake-deadbeef") is False


def test_reclaim_stops_unit_only_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """The idle case issues `systemctl --user stop <unit>`; the busy case does not."""
    calls: list[list[str]] = []

    def _record(argv: list[str], *_a: object, **_k: object) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(build_scope.subprocess, "run", _record)
    monkeypatch.setattr(build_scope, "_scope_loaded", lambda _unit: True)

    monkeypatch.setattr(build_scope, "_scope_is_idle", lambda _unit: True)
    assert build_scope._reclaim_idle_scope("bakar-bitbake-deadbeef") is True
    assert calls == [["systemctl", "--user", "stop", "bakar-bitbake-deadbeef"]]

    calls.clear()
    monkeypatch.setattr(build_scope, "_scope_is_idle", lambda _unit: False)
    assert (
        build_scope._reclaim_idle_scope(
            "bakar-bitbake-deadbeef",
            settle_timeout=0,
            sleep=lambda _s: None,
        )
        is False
    )
    assert calls == []  # a busy scope is never stopped


def _incrementing_clock():
    """A monotonic clock stub yielding 0.0, 1.0, 2.0, ... so waits are hermetic."""
    state = {"n": -1.0}

    def _clock() -> float:
        state["n"] += 1.0
        return state["n"]

    return _clock


def test_reclaim_no_wait_when_unit_not_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The common case (no scope loaded) must not sleep at all.

    The settle wait only exists to outlast a dying previous build; gating it on
    LoadState keeps an ordinary build from paying for it.
    """
    slept: list[float] = []
    monkeypatch.setattr(build_scope, "_scope_loaded", lambda _unit: False)
    monkeypatch.setattr(build_scope, "_scope_is_idle", lambda _unit: pytest.fail("must not probe the cgroup"))

    result = build_scope._reclaim_idle_scope("bakar-bitbake-deadbeef", sleep=slept.append)

    assert result is False
    assert slept == []


def test_reclaim_waits_out_a_draining_previous_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl-C then immediate re-run: the corpse drains, then the scope is reclaimed.

    Reproduces the reported failure - the interrupted build's kas client and its
    32 parser processes are still alive for a few seconds, so the scope reads
    busy even though no build is running there.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(
        build_scope.subprocess,
        "run",
        lambda argv, *_a, **_k: calls.append(list(argv)) or subprocess.CompletedProcess(argv, 0),
    )
    monkeypatch.setattr(build_scope, "_scope_loaded", lambda _unit: True)
    # Busy for the first three polls (client + parsers dying), then drained.
    states = iter([False, False, False, True])
    monkeypatch.setattr(build_scope, "_scope_is_idle", lambda _unit: next(states, True))
    notes: list[str] = []

    result = build_scope._reclaim_idle_scope(
        "bakar-bitbake-deadbeef",
        sleep=lambda _s: None,
        clock=_incrementing_clock(),
        notify=notes.append,
        settle_timeout=30,
    )

    assert result is True
    assert ["systemctl", "--user", "stop", "bakar-bitbake-deadbeef"] in calls
    assert any("still draining" in n for n in notes)  # announced once, not per poll
    assert len(notes) == 1


def test_reclaim_gives_up_on_a_genuinely_concurrent_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constraint 1: a scope that never drains is left alone so the launch collides."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        build_scope.subprocess,
        "run",
        lambda argv, *_a, **_k: calls.append(list(argv)) or subprocess.CompletedProcess(argv, 0),
    )
    monkeypatch.setattr(build_scope, "_scope_loaded", lambda _unit: True)
    monkeypatch.setattr(build_scope, "_scope_is_idle", lambda _unit: False)  # a real build, never idle

    result = build_scope._reclaim_idle_scope(
        "bakar-bitbake-deadbeef",
        sleep=lambda _s: None,
        clock=_incrementing_clock(),
        settle_timeout=5,
    )

    assert result is False
    assert ["systemctl", "--user", "stop", "bakar-bitbake-deadbeef"] not in calls


def test_reclaim_stops_waiting_when_unit_disappears(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scope that unloads itself mid-wait ends the wait immediately."""
    loaded = iter([True, True, False])
    monkeypatch.setattr(build_scope, "_scope_loaded", lambda _unit: next(loaded, False))
    monkeypatch.setattr(build_scope, "_scope_is_idle", lambda _unit: False)

    result = build_scope._reclaim_idle_scope(
        "bakar-bitbake-deadbeef",
        sleep=lambda _s: None,
        clock=_incrementing_clock(),
        settle_timeout=60,
    )

    assert result is False


def test_reclaim_survives_missing_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort: no systemctl binary is a no-op, not a crash."""

    def _boom(*_a: object, **_k: object) -> subprocess.CompletedProcess:
        raise OSError("no systemctl")

    monkeypatch.setattr(build_scope, "_scope_is_idle", lambda _unit: True)
    monkeypatch.setattr(build_scope.subprocess, "run", _boom)

    assert build_scope._reclaim_idle_scope("bakar-bitbake-deadbeef") is False


def test_wrap_reclaims_idle_scope_before_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """wrap stops an idle scope, then reset-failed's it, before launching."""
    monkeypatch.setattr(build_scope, "systemd_run_available", lambda: True)
    monkeypatch.setattr(build_scope, "_scope_loaded", lambda _unit: True)
    monkeypatch.setattr(build_scope, "_scope_is_idle", lambda _unit: True)
    calls: list[list[str]] = []

    def _record(argv: list[str], *_a: object, **_k: object) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(build_scope.subprocess, "run", _record)
    cfg = _cfg(tmp_path)
    unit = build_scope.scope_unit_name(cfg, "bitbake")
    log = _FakeLog()

    build_scope.wrap_build_command(_CMD, cfg, log, unit_suffix="bitbake")

    stop = ["systemctl", "--user", "stop", unit]
    reset = ["systemctl", "--user", "reset-failed", unit]
    assert stop in calls
    assert reset in calls
    assert calls.index(stop) < calls.index(reset)  # reclaim precedes the flush
    assert any("reclaimed idle build scope" in msg for msg in log.infos)


def test_wrap_leaves_busy_scope_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Constraint 1: a scope with a live build is never stopped by wrap."""
    monkeypatch.setattr(build_scope, "systemd_run_available", lambda: True)
    monkeypatch.setattr(build_scope, "_scope_is_idle", lambda _unit: False)
    calls: list[list[str]] = []

    def _record(argv: list[str], *_a: object, **_k: object) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(build_scope.subprocess, "run", _record)
    cfg = _cfg(tmp_path)
    unit = build_scope.scope_unit_name(cfg, "bitbake")

    build_scope.wrap_build_command(_CMD, cfg, _FakeLog(), unit_suffix="bitbake")

    assert ["systemctl", "--user", "stop", unit] not in calls


# --- _scope_cgroup_procs / _proc_cmdline against a fake tree ----------------


def test_scope_cgroup_procs_reads_pids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ControlGroup is resolved via systemctl, then cgroup.procs is read directly."""
    rel = "/user.slice/user-1000.slice/user@1000.service/app.slice/bakar-bitbake-x.scope"
    cg = tmp_path / rel.lstrip("/")
    cg.mkdir(parents=True)
    (cg / "cgroup.procs").write_text("689374\n689400\n")
    monkeypatch.setattr(
        build_scope.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout=f"{rel}\n"),
    )

    assert build_scope._scope_cgroup_procs("u", cgroup_root=tmp_path) == [689374, 689400]


def test_scope_cgroup_procs_none_when_unit_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unloaded unit reports an empty ControlGroup -> unknown, not empty."""
    monkeypatch.setattr(
        build_scope.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout="\n"),
    )

    assert build_scope._scope_cgroup_procs("u") is None


def test_scope_cgroup_procs_none_when_stdout_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CompletedProcess with stdout=None must not raise (defensive)."""
    monkeypatch.setattr(
        build_scope.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0),
    )

    assert build_scope._scope_cgroup_procs("u") is None


def test_proc_cmdline_reads_nul_separated_argv(tmp_path: Path) -> None:
    """/proc/<pid>/cmdline is NUL-separated; it is joined with spaces."""
    (tmp_path / "4242").mkdir()
    (tmp_path / "4242" / "cmdline").write_bytes(b"python3\x00bitbake-server\x00decafbad\x00")

    assert build_scope._proc_cmdline(4242, proc_root=tmp_path) == "python3 bitbake-server decafbad "


def test_proc_cmdline_none_when_absent(tmp_path: Path) -> None:
    """A vanished pid yields None (caller treats it as busy, never as idle)."""
    assert build_scope._proc_cmdline(999999, proc_root=tmp_path) is None
