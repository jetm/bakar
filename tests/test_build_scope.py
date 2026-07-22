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
def _force_systemd_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to systemd-run being available so wrap tests are host-independent.

    ``systemd_run_available`` is ``functools.cache``d; clear it and stub the
    inputs it reads (binary, runtime dir, and the throwaway probe) so tests do
    not depend on the host having systemd and never create a real scope.
    """
    build_scope.systemd_run_available.cache_clear()
    monkeypatch.setattr(build_scope.shutil, "which", lambda _name: "/usr/bin/systemd-run")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setattr(
        build_scope.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 0),
    )


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
    # Resource-control properties, defaults: 85/90% memory, 50/50 weights.
    assert "MemoryHigh=85%" in out
    assert "MemoryMax=90%" in out
    assert "MemorySwapMax=0" in out
    assert "CPUWeight=50" in out
    assert "IOWeight=50" in out
    # The original kas command is preserved as the tail.
    assert out[-3:] == _CMD
    # Journal hint logged so the run log records where to find the scope.
    assert any(unit in line and "journalctl" in line for line in log.infos)


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
    cfg = _cfg(tmp_path, scope_cpu_weight=0, scope_io_weight=0)
    out = build_scope.wrap_build_command(_CMD, cfg, _FakeLog(), unit_suffix="build")
    joined = " ".join(out)
    assert "CPUWeight" not in joined
    assert "IOWeight" not in joined
    assert "MemoryMax=90%" in joined  # memory ceiling still applied


def test_wrap_omits_out_of_range_memory_fraction(tmp_path: Path) -> None:
    # A fraction > 1 (or <= 0) is treated as "leave this control unset" rather
    # than emitting a nonsensical percentage.
    cfg = _cfg(tmp_path, scope_memory_high=0.0, scope_memory_max=1.0)
    out = build_scope.wrap_build_command(_CMD, cfg, _FakeLog(), unit_suffix="build")
    joined = " ".join(out)
    assert "MemoryHigh" not in joined  # 0.0 omitted
    assert "MemoryMax=100%" in joined  # 1.0 is in-range (== total)


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

    monkeypatch.setattr(step_kas, "clear_stale_bitbake_locks", lambda cfg: [])
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
