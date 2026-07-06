"""Hermetic unit tests for the standalone helpers in ``bakar.steps.kas_build``.

These tests exercise the pure-logic helpers around ``run_build``: overlay
materialization, the meta-avocado wrapper writer, the user-YAML
relative-path resolver, the ccache argv builder, the env-dict builder,
the dump branch stripper, the ``kas dump`` driver, and the stale
bitbake lock cleaner. Every test runs entirely against ``tmp_path``
with no network, no Docker, no real subprocess; bare ``subprocess.run``
is monkey-patched at the ``bakar.steps.kas_build.subprocess.run``
attribute so the underlying logic in ``_run_kas_dump`` runs end-to-end
against in-memory fixtures.

``run_build`` and its ``handle_line`` closure are intentionally NOT
exercised here - they are kernel-PTY + threading orchestration with no
testable logic seam and remain ``# pragma: no cover`` in the source.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import yaml

from bakar.config import BuildConfig
from bakar.observability import RunLogger
from bakar.steps.kas_build import (
    _autocalibrate_psi,
    _build_env,
    _build_fail_reason,
    _ccache_args,
    _find_oe_eventlog,
    _finish_step,
    _inject_literal_ccache,
    _resolve_user_yaml,
    _run_kas_dump,
    _strip_branch_from_dump,
    _write_meta_avocado_wrapper,
    clear_stale_bitbake_locks,
    copy_oe_eventlog_to_run_dir,
    materialize_overlay,
)
from bakar.user_config import load_user_config

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------


def _make_nxp_cfg(workspace: Path, *, host_mode: bool = False) -> BuildConfig:
    """Construct a minimal NXP BuildConfig anchored at ``workspace``.

    The ``nxp`` subdirectory is created so ``cfg.bsp_root`` resolves to
    a real directory; helpers that ``mkdir(parents=True, exist_ok=True)``
    or read/write inside ``bsp_root`` can run unchanged.
    """
    bsp_root = workspace / "nxp"
    bsp_root.mkdir(parents=True, exist_ok=True)
    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        repo_url="https://example.invalid/repo.git",
        repo_branch="scarthgap",
        kas_container_image="jetm/kas-build-env:latest",
        host_mode=host_mode,
    )


def _make_meta_avocado_cfg(workspace: Path) -> tuple[BuildConfig, Path]:
    """Construct a generic BuildConfig pointing at a meta-avocado YAML.

    Returns ``(cfg, kas_yaml)`` where ``kas_yaml`` lives inside a
    ``meta-avocado`` directory under ``workspace``. ``cfg.is_meta_avocado``
    is True for this config and ``cfg.bsp_root`` is
    ``workspace/build-<stem>``.
    """
    meta = workspace / "sources" / "meta-avocado"
    kas_dir = meta / "kas" / "machine"
    kas_dir.mkdir(parents=True, exist_ok=True)
    kas_yaml = kas_dir / "qemux86-64.yml"
    kas_yaml.write_text("header:\n  version: 16\n", encoding="utf-8")
    cfg = BuildConfig(
        workspace=workspace,
        bsp_family="generic",  # type: ignore[arg-type]
        machine="generic",
        distro="generic",
        image="generic",
        manifest="",
        repo_url="",
        repo_branch="",
        kas_container_image="jetm/kas-build-env:latest",
        kas_yaml_override=kas_yaml,
    )
    cfg.bsp_root.mkdir(parents=True, exist_ok=True)
    return cfg, kas_yaml


# ---------------------------------------------------------------------------
# materialize_overlay
# ---------------------------------------------------------------------------


def test_materialize_overlay_copies_to_bsp_root(tmp_path: Path) -> None:
    """The overlay file is copied into ``<bsp_root>/.bakar/overlays/``."""
    cfg = _make_nxp_cfg(tmp_path)
    overlay = tmp_path / "src-overlay.yml"
    overlay.write_text('local_conf_header:\n  bakar: |\n    BB_NUMBER_THREADS = "4"\n', encoding="utf-8")

    rel = materialize_overlay(cfg, overlay)

    dest = cfg.bsp_root / rel
    assert dest.is_file()
    assert not dest.is_symlink()
    assert dest.read_text(encoding="utf-8") == overlay.read_text(encoding="utf-8")


def test_materialize_overlay_returns_relative_to_bsp_root(tmp_path: Path) -> None:
    """The returned path is relative to ``cfg.bsp_root`` (overlay name preserved)."""
    cfg = _make_nxp_cfg(tmp_path)
    overlay = tmp_path / "tuning.yml"
    overlay.write_text("# overlay\n", encoding="utf-8")

    rel = materialize_overlay(cfg, overlay)

    assert rel.is_absolute() is False
    assert rel.parts[-1] == "tuning.yml"
    assert rel.parts[0] == ".bakar"


def test_materialize_overlay_overwrites_existing_destination(tmp_path: Path) -> None:
    """Every invocation refreshes the destination file byte-for-byte."""
    cfg = _make_nxp_cfg(tmp_path)
    overlay = tmp_path / "overlay.yml"
    overlay.write_text("first\n", encoding="utf-8")
    first_rel = materialize_overlay(cfg, overlay)
    assert (cfg.bsp_root / first_rel).read_text(encoding="utf-8") == "first\n"

    overlay.write_text("second\n", encoding="utf-8")
    second_rel = materialize_overlay(cfg, overlay)

    assert second_rel == first_rel
    assert (cfg.bsp_root / second_rel).read_text(encoding="utf-8") == "second\n"


# ---------------------------------------------------------------------------
# _write_meta_avocado_wrapper
# ---------------------------------------------------------------------------


def test_write_meta_avocado_wrapper_returns_bsp_root_wrapper_path(tmp_path: Path) -> None:
    """Wrapper lands at ``<bsp_root>/avocado-wrapper.yml`` and content is well-formed."""
    cfg, kas_yaml = _make_meta_avocado_cfg(tmp_path)

    wrapper = _write_meta_avocado_wrapper(cfg, kas_yaml)

    assert wrapper == cfg.bsp_root / "avocado-wrapper.yml"
    assert wrapper.is_file()
    parsed = yaml.safe_load(wrapper.read_text(encoding="utf-8"))
    assert parsed["header"]["version"] == 16
    assert parsed["repos"]["meta-avocado"]["path"] == "meta-avocado"
    include = parsed["header"]["includes"][0]
    assert include["repo"] == "meta-avocado"
    # The file path is relative to the meta-avocado boundary.
    assert include["file"] == "kas/machine/qemux86-64.yml"


def test_write_meta_avocado_wrapper_raises_outside_meta_avocado(tmp_path: Path) -> None:
    """A YAML outside any ``meta-avocado`` parent triggers ``RuntimeError``."""
    cfg = _make_nxp_cfg(tmp_path)
    stray = tmp_path / "not-meta-avocado" / "yaml.yml"
    stray.parent.mkdir()
    stray.write_text("header: {}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="meta-avocado"):
        _write_meta_avocado_wrapper(cfg, stray)


# ---------------------------------------------------------------------------
# _resolve_user_yaml
# ---------------------------------------------------------------------------


def test_resolve_user_yaml_returns_relative_when_inside_bsp_root(tmp_path: Path) -> None:
    """A YAML living inside ``bsp_root`` is returned as a relative path."""
    cfg = _make_nxp_cfg(tmp_path)
    kas_yaml = cfg.bsp_root / "kas-nxp.yml"
    kas_yaml.write_text("# kas\n", encoding="utf-8")

    rel = _resolve_user_yaml(cfg, kas_yaml)

    assert rel == kas_yaml.relative_to(cfg.bsp_root)
    assert rel.is_absolute() is False


def test_resolve_user_yaml_raises_when_outside_bsp_root(tmp_path: Path) -> None:
    """Non-meta-avocado config: a YAML outside ``bsp_root`` raises ``RuntimeError``."""
    cfg = _make_nxp_cfg(tmp_path)
    outside = tmp_path / "elsewhere" / "kas.yml"
    outside.parent.mkdir()
    outside.write_text("# kas\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="outside bsp_root"):
        _resolve_user_yaml(cfg, outside)


def test_resolve_user_yaml_meta_avocado_path_via_symlink(tmp_path: Path) -> None:
    """meta-avocado branch: the path is expressed via the ``meta-avocado`` symlink."""
    cfg, kas_yaml = _make_meta_avocado_cfg(tmp_path)
    assert cfg.is_meta_avocado is True

    rel = _resolve_user_yaml(cfg, kas_yaml)

    assert rel.parts[0] == "meta-avocado"
    assert rel.as_posix().endswith("kas/machine/qemux86-64.yml")


# ---------------------------------------------------------------------------
# _ccache_args
# ---------------------------------------------------------------------------


def test_ccache_args_host_mode_returns_empty(tmp_path: Path) -> None:
    """Host mode bypasses kas-container so no ``--runtime-args`` is emitted."""
    cfg = _make_nxp_cfg(tmp_path, host_mode=True)
    assert _ccache_args(cfg) == []


def test_ccache_args_container_mode_returns_two_element_list(tmp_path: Path) -> None:
    """Container mode emits exactly two elements: the flag and one string value."""
    cfg = _make_nxp_cfg(tmp_path, host_mode=False)
    args = _ccache_args(cfg)
    assert len(args) == 2
    assert args[0] == "--runtime-args"
    # The bind-mount string targets the workspace ccache dir.
    assert f"-v {cfg.workspace / 'ccache'}:/work/ccache:rw" in args[1]


# ---------------------------------------------------------------------------
# _build_env
# ---------------------------------------------------------------------------


def test_build_env_returns_minimum_required_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_build_env`` always populates KAS_WORK_DIR, PATH, HOME, and NPROC."""
    monkeypatch.delenv("NPROC", raising=False)
    cfg = _make_nxp_cfg(tmp_path)

    env = _build_env(cfg)

    assert env["KAS_WORK_DIR"] == str(cfg.bsp_root)
    assert "PATH" in env
    assert "HOME" in env
    assert env["NPROC"] != ""


def test_build_env_kas_work_dir_points_at_bsp_root(tmp_path: Path) -> None:
    """For non-meta-avocado builds, KAS_WORK_DIR equals ``cfg.bsp_root``."""
    cfg = _make_nxp_cfg(tmp_path)

    env = _build_env(cfg)

    assert env["KAS_WORK_DIR"] == str(cfg.bsp_root)
    assert env["KAS_WORK_DIR"].endswith("/nxp")


def test_build_env_nproc_defaults_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """NPROC falls back to ``os.cpu_count()`` (or 16) when the env var is absent."""
    monkeypatch.delenv("NPROC", raising=False)
    cfg = _make_nxp_cfg(tmp_path)

    env = _build_env(cfg)

    assert int(env["NPROC"]) >= 1


# ---------------------------------------------------------------------------
# _strip_branch_from_dump
# ---------------------------------------------------------------------------


def test_strip_branch_from_dump_removes_branch_when_commit_present(tmp_path: Path) -> None:
    """When both ``commit:`` and ``branch:`` are present, ``branch:`` is stripped."""
    dump = tmp_path / "avocado-bakar.yml"
    dump.write_text(
        "header:\n"
        "  version: 16\n"
        "repos:\n"
        "  meta-foo:\n"
        "    url: https://example.invalid/meta-foo.git\n"
        "    commit: deadbeefcafe1234567890\n"
        "    branch: scarthgap\n",
        encoding="utf-8",
    )

    result = _strip_branch_from_dump(dump)

    assert result is None  # mutates in place; returns None
    rewritten = dump.read_text(encoding="utf-8")
    assert "branch:" not in rewritten
    assert "commit:" in rewritten
    assert "deadbeefcafe1234567890" in rewritten


def test_strip_branch_from_dump_leaves_branch_alone_when_commit_absent(tmp_path: Path) -> None:
    """A repo with ``branch:`` but no ``commit:`` is untouched."""
    dump = tmp_path / "avocado-bakar.yml"
    original = (
        "header:\n"
        "  version: 16\n"
        "repos:\n"
        "  meta-bar:\n"
        "    url: https://example.invalid/meta-bar.git\n"
        "    branch: scarthgap\n"
    )
    dump.write_text(original, encoding="utf-8")

    _strip_branch_from_dump(dump)

    # File content equivalent: branch key still present, value unchanged.
    parsed = yaml.safe_load(dump.read_text(encoding="utf-8"))
    assert parsed["repos"]["meta-bar"]["branch"] == "scarthgap"


def test_strip_branch_from_dump_handles_missing_repos_block(tmp_path: Path) -> None:
    """A dump without a ``repos:`` mapping is a no-op (no exception)."""
    dump = tmp_path / "avocado-bakar.yml"
    dump.write_text("header:\n  version: 16\n", encoding="utf-8")

    _strip_branch_from_dump(dump)

    # Content unchanged after the early-return path.
    assert yaml.safe_load(dump.read_text(encoding="utf-8")) == {"header": {"version": 16}}


# ---------------------------------------------------------------------------
# _run_kas_dump
# ---------------------------------------------------------------------------


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Build a ``CompletedProcess`` with str output (matches text=True)."""
    return subprocess.CompletedProcess(args=["kas", "dump"], returncode=returncode, stdout=stdout, stderr=stderr)


DUMP_YAML = (
    "header:\n"
    "  version: 16\n"
    "repos:\n"
    "  meta-foo:\n"
    "    url: https://example.invalid/meta-foo.git\n"
    "    commit: deadbeefcafe1234567890\n"
    "    branch: scarthgap\n"
)


def test_run_kas_dump_success_writes_dump_and_strips_branch(tmp_path: Path) -> None:
    """First-try success: dump is written, ``_strip_branch_from_dump`` runs."""
    cfg = _make_nxp_cfg(tmp_path)
    wrapper = cfg.bsp_root / "avocado-wrapper.yml"
    wrapper.write_text("header:\n  version: 16\n", encoding="utf-8")
    overlay_rel = materialize_overlay(cfg, _write_overlay(tmp_path, "overlay.yml"))

    with patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)) as run:
        dump = _run_kas_dump(cfg, wrapper, overlay_rel)

    assert dump == cfg.bsp_root / "avocado-bakar.yml"
    assert dump.is_file()
    # _strip_branch_from_dump ran on the written file.
    text = dump.read_text(encoding="utf-8")
    assert "commit:" in text
    assert "branch:" not in text
    assert run.call_count == 1


def test_run_kas_dump_retries_on_rebased_remote_branch(tmp_path: Path) -> None:
    """First call fails with the rebase marker; retry with ``--skip repos_checkout`` succeeds."""
    cfg = _make_nxp_cfg(tmp_path)
    wrapper = cfg.bsp_root / "avocado-wrapper.yml"
    wrapper.write_text("header:\n  version: 16\n", encoding="utf-8")
    overlay_rel = materialize_overlay(cfg, _write_overlay(tmp_path, "overlay.yml"))

    responses = [
        _completed(1, stderr="error: origin/scarthgap does not contain commit deadbeef"),
        _completed(0, stdout=DUMP_YAML),
    ]
    with patch("bakar.steps.kas_build.subprocess.run", side_effect=responses) as run:
        dump = _run_kas_dump(cfg, wrapper, overlay_rel)

    assert dump.is_file()
    assert run.call_count == 2
    # The retry call must include the --skip repos_checkout flag.
    retry_argv = run.call_args_list[1].args[0]
    assert "--skip" in retry_argv
    assert "repos_checkout" in retry_argv
    # Stripping ran on the retry output too.
    assert "branch:" not in dump.read_text(encoding="utf-8")


def test_run_kas_dump_raises_when_initial_failure_is_unrecognised(tmp_path: Path) -> None:
    """A non-zero exit without the rebase marker raises ``RuntimeError`` (no retry)."""
    cfg = _make_nxp_cfg(tmp_path)
    wrapper = cfg.bsp_root / "avocado-wrapper.yml"
    wrapper.write_text("header:\n  version: 16\n", encoding="utf-8")
    overlay_rel = materialize_overlay(cfg, _write_overlay(tmp_path, "overlay.yml"))

    with (
        patch(
            "bakar.steps.kas_build.subprocess.run",
            return_value=_completed(2, stderr="syntax error in YAML"),
        ) as run,
        pytest.raises(RuntimeError, match="kas dump failed"),
    ):
        _run_kas_dump(cfg, wrapper, overlay_rel)

    assert run.call_count == 1


def _write_overlay(tmp_path: Path, name: str) -> Path:
    """Write a placeholder overlay file outside any BSP root for materialization tests."""
    p = tmp_path / name
    p.write_text('local_conf_header:\n  bakar: |\n    BB_NUMBER_THREADS = "4"\n', encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# clear_stale_bitbake_locks
# ---------------------------------------------------------------------------


def _seed_build_dir(cfg: BuildConfig) -> Path:
    """Create ``<bsp_root>/build/`` and return the path."""
    build_dir = cfg.bsp_root / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    return build_dir


def test_clear_stale_bitbake_locks_dead_pid_lock_is_removed(tmp_path: Path) -> None:
    """A lock owned by a dead PID is removed and reported."""
    cfg = _make_nxp_cfg(tmp_path)
    build = _seed_build_dir(cfg)
    lock = build / "bitbake.lock"
    lock.write_text("999999\n", encoding="utf-8")

    def _raise_dead(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    with patch("bakar.steps.kas_build.os.kill", side_effect=_raise_dead):
        removed = clear_stale_bitbake_locks(cfg)

    assert lock in removed
    assert not lock.exists()


def test_clear_stale_bitbake_locks_orphan_sockets_removed_with_no_lock(tmp_path: Path) -> None:
    """Sockets present without a lock file are removed unconditionally."""
    cfg = _make_nxp_cfg(tmp_path)
    build = _seed_build_dir(cfg)
    bb_sock = build / "bitbake.sock"
    hs_sock = build / "hashserve.sock"
    bb_sock.write_text("", encoding="utf-8")
    hs_sock.write_text("", encoding="utf-8")
    assert not (build / "bitbake.lock").exists()

    removed = clear_stale_bitbake_locks(cfg)

    assert bb_sock in removed
    assert hs_sock in removed
    assert not bb_sock.exists()
    assert not hs_sock.exists()


def test_clear_stale_bitbake_locks_live_bitbake_pid_leaves_lock(tmp_path: Path) -> None:
    """A live PID whose ``/proc`` cmdline contains ``bitbake`` is left alone."""
    cfg = _make_nxp_cfg(tmp_path)
    build = _seed_build_dir(cfg)
    lock = build / "bitbake.lock"
    lock.write_text("1234\n", encoding="utf-8")

    # os.kill(pid, 0) must succeed (return None) to signal the PID is alive.
    # Then the cmdline path is read and 'bitbake' must be detected. The
    # source reads /proc/<pid>/cmdline via Path.exists / Path.read_bytes,
    # so patch the Path methods directly rather than mocking the filesystem.
    real_read_bytes = type(lock).read_bytes
    real_exists = type(lock).exists

    def fake_exists(self: object) -> bool:
        if str(self) == "/proc/1234/cmdline":
            return True
        return real_exists(self)  # type: ignore[arg-type]

    def fake_read_bytes(self: object) -> bytes:
        if str(self) == "/proc/1234/cmdline":
            return b"bitbake-server\x00--server-only\x00"
        return real_read_bytes(self)  # type: ignore[arg-type]

    with (
        patch("bakar.steps.kas_build.os.kill", return_value=None),
        patch("bakar.steps.kas_build.Path.exists", new=fake_exists),
        patch("bakar.steps.kas_build.Path.read_bytes", new=fake_read_bytes),
    ):
        removed = clear_stale_bitbake_locks(cfg)

    assert removed == []
    assert lock.exists()
    assert lock.read_text(encoding="utf-8").strip() == "1234"


def test_clear_stale_bitbake_locks_live_non_bitbake_pid_removes_lock(tmp_path: Path) -> None:
    """A live PID whose cmdline is NOT bitbake gets the lock removed."""
    cfg = _make_nxp_cfg(tmp_path)
    build = _seed_build_dir(cfg)
    lock = build / "bitbake.lock"
    lock.write_text("4321\n", encoding="utf-8")

    real_read_bytes = type(lock).read_bytes
    real_exists = type(lock).exists

    def fake_exists(self: object) -> bool:
        if str(self) == "/proc/4321/cmdline":
            return True
        return real_exists(self)  # type: ignore[arg-type]

    def fake_read_bytes(self: object) -> bytes:
        if str(self) == "/proc/4321/cmdline":
            return b"vim\x00/etc/hosts\x00"
        return real_read_bytes(self)  # type: ignore[arg-type]

    with (
        patch("bakar.steps.kas_build.os.kill", return_value=None),
        patch("bakar.steps.kas_build.Path.exists", new=fake_exists),
        patch("bakar.steps.kas_build.Path.read_bytes", new=fake_read_bytes),
    ):
        removed = clear_stale_bitbake_locks(cfg)

    assert lock in removed
    assert not lock.exists()


def test_clear_stale_bitbake_locks_no_lock_no_sockets_returns_empty(tmp_path: Path) -> None:
    """When neither lock nor sockets exist, the cleaner returns an empty list."""
    cfg = _make_nxp_cfg(tmp_path)
    _seed_build_dir(cfg)

    removed = clear_stale_bitbake_locks(cfg)

    assert removed == []


# ---------------------------------------------------------------------------
# _autocalibrate_psi
# ---------------------------------------------------------------------------


class _FakeLog:
    """Minimal RunLogger stand-in capturing info() messages."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, msg: str, **_fields: object) -> None:
        self.messages.append(msg)


def test_autocalibrate_psi_disabled_is_noop(tmp_path: Path) -> None:
    """With psi_autocalibrate False, nothing is written and {} is returned."""
    cfg = _make_nxp_cfg(tmp_path)  # psi_autocalibrate defaults False
    log = _FakeLog()
    config_file = tmp_path / "config.toml"

    assert _autocalibrate_psi(cfg, {"cpu": 40.0}, log, config_file) == {}
    assert not config_file.exists()
    assert log.messages == []


def test_autocalibrate_psi_writes_and_reports(tmp_path: Path) -> None:
    """When enabled, recommended thresholds are written and the update is reported."""
    cfg = replace(_make_nxp_cfg(tmp_path), psi_autocalibrate=True)
    log = _FakeLog()
    config_file = tmp_path / "config.toml"

    changes = _autocalibrate_psi(cfg, {"cpu": 40.0, "io": 10.0, "memory": 5.0}, log, config_file)

    assert set(changes) == {"cpu", "io", "memory"}
    assert any("PSI auto-calibrated" in m for m in log.messages)
    assert load_user_config(config_file).pressure_max_cpu == changes["cpu"]


def test_autocalibrate_psi_no_peaks_is_noop(tmp_path: Path) -> None:
    """Enabled but with no sampled peaks (PSI unavailable): no write, no report."""
    cfg = replace(_make_nxp_cfg(tmp_path), psi_autocalibrate=True)
    log = _FakeLog()
    config_file = tmp_path / "config.toml"

    assert _autocalibrate_psi(cfg, {}, log, config_file) == {}
    assert not config_file.exists()


# ---------------------------------------------------------------------------
# _find_oe_eventlog / copy_oe_eventlog_to_run_dir
# ---------------------------------------------------------------------------


def _oe_eventlog_cfg(workspace: Path) -> BuildConfig:
    return _make_nxp_cfg(workspace)


def test_find_oe_eventlog_returns_none_when_dir_absent(tmp_path: Path) -> None:
    """No eventlog dir -> None, no crash."""
    cfg = _oe_eventlog_cfg(tmp_path)
    log = RunLogger(runs_dir=cfg.runs_dir)
    log.run_dir.mkdir(parents=True, exist_ok=True)
    assert _find_oe_eventlog(cfg, log) is None


def test_find_oe_eventlog_returns_none_when_no_new_files(tmp_path: Path) -> None:
    """Eventlog dir exists but all files predate the run start -> None."""
    from datetime import datetime

    cfg = _oe_eventlog_cfg(tmp_path)
    log = RunLogger(runs_dir=cfg.runs_dir)
    log.run_dir.mkdir(parents=True, exist_ok=True)
    elog_dir = cfg.bsp_root / "build" / "tmp" / "log" / "eventlog"
    elog_dir.mkdir(parents=True)
    old_file = elog_dir / "20260101120000.json"
    old_file.write_text("{}")
    # backdate: set mtime 10 seconds before the run_id-derived watermark
    watermark = datetime.strptime(log.run_id, "%Y%m%d-%H%M%S").timestamp()
    os.utime(old_file, (watermark - 10, watermark - 10))
    assert _find_oe_eventlog(cfg, log) is None


def test_find_oe_eventlog_returns_newest_file_after_watermark(tmp_path: Path) -> None:
    """Two new files -> the newer one is returned."""
    cfg = _oe_eventlog_cfg(tmp_path)
    log = RunLogger(runs_dir=cfg.runs_dir)
    log.run_dir.mkdir(parents=True, exist_ok=True)
    elog_dir = cfg.bsp_root / "build" / "tmp" / "log" / "eventlog"
    elog_dir.mkdir(parents=True)
    time.sleep(0.01)
    first = elog_dir / "20260604120000.json"
    first.write_text('{"a":1}')
    time.sleep(0.02)
    second = elog_dir / "20260604130000.json"
    second.write_text('{"b":2}')
    result = _find_oe_eventlog(cfg, log)
    assert result == second


def test_copy_oe_eventlog_noop_when_primary_exists(tmp_path: Path) -> None:
    """If log.eventlog_path already exists, nothing is copied and False is returned."""
    cfg = _oe_eventlog_cfg(tmp_path)
    log = RunLogger(runs_dir=cfg.runs_dir)
    log.run_dir.mkdir(parents=True, exist_ok=True)
    log.eventlog_path.write_text('{"existing":true}')
    assert copy_oe_eventlog_to_run_dir(cfg, log) is False
    assert log.eventlog_path.read_text() == '{"existing":true}'


def test_copy_oe_eventlog_copies_when_primary_absent(tmp_path: Path) -> None:
    """When primary path is absent and OE log exists, it is copied and True is returned."""
    cfg = _oe_eventlog_cfg(tmp_path)
    log = RunLogger(runs_dir=cfg.runs_dir)
    log.run_dir.mkdir(parents=True, exist_ok=True)
    elog_dir = cfg.bsp_root / "build" / "tmp" / "log" / "eventlog"
    elog_dir.mkdir(parents=True)
    time.sleep(0.01)
    oe_file = elog_dir / "20260604145000.json"
    oe_file.write_text('{"oe":true}')
    result = copy_oe_eventlog_to_run_dir(cfg, log)
    assert result is True
    assert log.eventlog_path.read_text() == '{"oe":true}'


@pytest.mark.unit
def test_build_fail_reason_stall_names_tasks() -> None:
    """A stall abort reports stall-timeout with the wedged task labels."""
    reason = _build_fail_reason(-2, ["u-boot:do_compile", "nodejs:do_compile"])
    assert reason == "stall-timeout: u-boot:do_compile, nodejs:do_compile"


@pytest.mark.unit
def test_build_fail_reason_exit_code_when_no_stall() -> None:
    """A plain nonzero exit reports the exit code."""
    assert _build_fail_reason(1, None) == "exit_code=1"


@pytest.mark.unit
def test_build_fail_reason_wrapper_crash_when_rc_none() -> None:
    """No exit code and no stall is a wrapper crash."""
    assert _build_fail_reason(None, None) == "wrapper-crash"


# ---------------------------------------------------------------------------
# _inject_rm_work
# ---------------------------------------------------------------------------

_RM_WORK_OVERLAY = (
    "local_conf_header:\n"
    "  zz-bakar-10-base: |\n"
    '    BB_NUMBER_THREADS = "4"\n'
    "    # Disable rm_work while bakar is in use. Strip both inherit paths.\n"
    '    INHERIT:remove = "rm_work"\n'
    '    USER_CLASSES:remove = "rm_work"\n'
)


def _materialize_rm_work_overlay(tmp_path: Path, *, rm_work: bool) -> str:
    """Materialize a base-shaped overlay and return the resulting text."""
    import dataclasses

    cfg = dataclasses.replace(_make_nxp_cfg(tmp_path), rm_work=rm_work)
    src = tmp_path / "bakar-tuning-base.yml"
    src.write_text(_RM_WORK_OVERLAY, encoding="utf-8")
    rel = materialize_overlay(cfg, src)
    return (cfg.bsp_root / rel).read_text(encoding="utf-8")


def test_inject_rm_work_keeps_strip_when_rm_work_off(tmp_path: Path) -> None:
    """Default (rm_work off): the rm_work-removal block survives materialization."""
    text = _materialize_rm_work_overlay(tmp_path, rm_work=False)

    assert 'INHERIT:remove = "rm_work"' in text
    assert 'USER_CLASSES:remove = "rm_work"' in text


def test_inject_rm_work_strips_block_when_rm_work_on(tmp_path: Path) -> None:
    """rm_work=True deletes the whole block (comment + both lines), no stale comment."""
    text = _materialize_rm_work_overlay(tmp_path, rm_work=True)

    assert 'INHERIT:remove = "rm_work"' not in text
    assert 'USER_CLASSES:remove = "rm_work"' not in text
    assert "Disable rm_work while bakar" not in text
    # Untouched content stays.
    assert 'BB_NUMBER_THREADS = "4"' in text


# ---------------------------------------------------------------------------
# _inject_literal_ccache
# ---------------------------------------------------------------------------

_CCACHE_OVERLAY = (
    "local_conf_header:\n"
    "  zz-bakar-20-ccache: |\n"
    '    CCACHE_DIR = "${TOPDIR}/ccache"\n'
    '    INHERIT += "ccache"\n'
    '    CCACHE_MAXSIZE = "50G"\n'
    "    export CCACHE_MAXSIZE\n"
    '    CCACHE_DISABLE:pn-nodejs = "1"\n'
)


def test_inject_literal_ccache_host_mode_rewrites_dir(tmp_path: Path) -> None:
    """Host mode rewrites CCACHE_DIR to the effective host path; leaves the rest."""
    cfg = _make_nxp_cfg(tmp_path, host_mode=True)

    result = _inject_literal_ccache(cfg, _CCACHE_OVERLAY)

    assert f'CCACHE_DIR = "{cfg.effective_ccache_dir}"' in result
    assert "/work/ccache" not in result
    assert "${TOPDIR}/ccache" not in result  # neutral default rewritten away
    # Sibling lines untouched.
    assert 'CCACHE_MAXSIZE = "50G"' in result
    assert "export CCACHE_MAXSIZE" in result
    assert 'INHERIT += "ccache"' in result
    assert "CCACHE_DISABLE:pn-nodejs" in result


def test_inject_literal_ccache_container_mode_sets_work_path(tmp_path: Path) -> None:
    """Container mode rewrites the neutral default to the /work bind-mount target."""
    cfg = _make_nxp_cfg(tmp_path, host_mode=False)
    result = _inject_literal_ccache(cfg, _CCACHE_OVERLAY)
    assert 'CCACHE_DIR = "/work/ccache"' in result
    assert "${TOPDIR}/ccache" not in result


def test_inject_literal_ccache_uses_explicit_ccache_dir(tmp_path: Path) -> None:
    import dataclasses

    explicit = tmp_path / "my-cache"
    cfg = dataclasses.replace(_make_nxp_cfg(tmp_path, host_mode=True), ccache_dir=str(explicit))
    result = _inject_literal_ccache(cfg, _CCACHE_OVERLAY)
    assert f'CCACHE_DIR = "{cfg.effective_ccache_dir}"' in result
    assert str(explicit) in result


def test_inject_literal_ccache_uses_shared_dir(tmp_path: Path) -> None:
    import dataclasses

    cfg = dataclasses.replace(_make_nxp_cfg(tmp_path, host_mode=True), ccache_shared=True)
    result = _inject_literal_ccache(cfg, _CCACHE_OVERLAY)
    assert f'CCACHE_DIR = "{cfg.effective_ccache_dir}"' in result


def test_inject_literal_ccache_preserves_indentation(tmp_path: Path) -> None:
    """The rewritten line keeps the original 4-space indentation."""
    cfg = _make_nxp_cfg(tmp_path, host_mode=True)
    result = _inject_literal_ccache(cfg, _CCACHE_OVERLAY)
    assert f'    CCACHE_DIR = "{cfg.effective_ccache_dir}"\n' in result


def test_materialize_ccache_overlay_host_mode_rewrites_dir(tmp_path: Path) -> None:
    """materialize_overlay rewrites the ccache dir in host mode and creates it."""
    cfg = _make_nxp_cfg(tmp_path, host_mode=True)
    src = tmp_path / "bakar-tuning-ccache.yml"
    src.write_text(_CCACHE_OVERLAY, encoding="utf-8")

    rel = materialize_overlay(cfg, src)
    text = (cfg.bsp_root / rel).read_text(encoding="utf-8")

    assert f'CCACHE_DIR = "{cfg.effective_ccache_dir}"' in text
    assert "/work/ccache" not in text
    assert cfg.effective_ccache_dir.is_dir()


def test_materialize_ccache_overlay_container_mode_sets_work_path(tmp_path: Path) -> None:
    """Container mode injects the /work/ccache bind-mount target from the neutral default."""
    cfg = _make_nxp_cfg(tmp_path, host_mode=False)
    src = tmp_path / "bakar-tuning-ccache.yml"
    src.write_text(_CCACHE_OVERLAY, encoding="utf-8")

    rel = materialize_overlay(cfg, src)
    text = (cfg.bsp_root / rel).read_text(encoding="utf-8")

    assert 'CCACHE_DIR = "/work/ccache"' in text


def test_shipped_ccache_overlay_has_no_work_path() -> None:
    """The shipped ccache overlay source carries a neutral default, never a /work path.

    Guards the host-default contract: the overlay must not name a container
    /work path; the per-mode value is constructed by _inject_literal_ccache.
    """
    import importlib.resources

    text = (importlib.resources.files("bakar") / "overlays" / "bakar-tuning-ccache.yml").read_text(encoding="utf-8")
    assert "/work" not in text


# ---------------------------------------------------------------------------
# _finish_step
# ---------------------------------------------------------------------------


class _FakeStepLog:
    """Minimal RunLogger stand-in recording step_ok/step_fail calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def step_ok(self, step: str, **fields: object) -> None:
        self.calls.append(("step_ok", step, fields))

    def step_fail(self, step: str, reason: str, **fields: object) -> None:
        self.calls.append(("step_fail", step, {"reason": reason, **fields}))


def test_finish_step_run_shell_rc_zero_emits_step_ok_with_exit_code() -> None:
    log = _FakeStepLog()

    _finish_step(log, "kas_shell", 0)

    assert log.calls == [("step_ok", "kas_shell", {"exit_code": 0})]


@pytest.mark.parametrize("rc", [1, 137])
def test_finish_step_run_shell_nonzero_rc_emits_step_fail_with_reason_and_exit_code(rc: int) -> None:
    """A failing run_shell/run_shell_capture call must carry a structured exit_code,
    not just embed it in the reason string.
    """
    log = _FakeStepLog()

    _finish_step(log, "kas_shell_capture", rc)

    assert log.calls == [
        ("step_fail", "kas_shell_capture", {"reason": f"exit_code={rc}", "exit_code": rc}),
    ]
