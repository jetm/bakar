"""Tests for the ``bakar clean-cache --full`` total cold-reset path.

The full reset ports scripts/clean-all-cache.sh: it empties the shared sstate in
place (NFS-safe), wipes the per-node build dir(s) and the sccache client cache,
stops the client daemon, and resets the sccache-dist server on PC1 (local) and
each secondary over ssh.

Every subprocess call (sccache, ip, pkill, bash -c server reset, ssh) is mocked -
no real sudo, ssh, systemctl, or pkill runs. Local filesystem ops (sstate empty,
build-dir wipe, cache wipe) run for real against tmp_path, with ``HOME`` redirected
so the real ``~/.cache/sccache`` is never touched.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from bakar.cli import app
from bakar.commands.clean_cache import (
    _empty_dir_in_place,
    _parse_dist_status_servers,
    _resolve_secondaries,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

runner = CliRunner()


# ---------------------------------------------------------------------------
# _resolve_secondaries
# ---------------------------------------------------------------------------


def test_resolve_secondaries_env_override_splits(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SECONDARY_NODES`` wins and is space-split, bypassing sccache entirely."""
    monkeypatch.setenv("SECONDARY_NODES", "host-a  host-b   host-c")

    def _boom(*_a, **_k):  # sccache/which must not be consulted
        raise AssertionError("sccache should not be invoked when SECONDARY_NODES is set")

    monkeypatch.setattr("bakar.commands.clean_cache.shutil.which", _boom)

    assert _resolve_secondaries() == ["host-a", "host-b", "host-c"]


def test_resolve_secondaries_sccache_absent_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``SECONDARY_NODES`` and no sccache on PATH -> []."""
    monkeypatch.delenv("SECONDARY_NODES", raising=False)
    monkeypatch.setattr("bakar.commands.clean_cache.shutil.which", lambda _n: None)

    assert _resolve_secondaries() == []


def test_resolve_secondaries_parses_dist_status_filters_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """A synthetic --dist-status JSON yields the remote hosts, local IPs filtered out."""
    monkeypatch.delenv("SECONDARY_NODES", raising=False)
    monkeypatch.setattr("bakar.commands.clean_cache.shutil.which", lambda _n: "/usr/bin/sccache")

    status = {
        "SchedulerStatus": [
            "ok",
            {
                "servers": [
                    {"id": "192.168.8.172:10501"},  # local -> dropped
                    {"id": "10.42.0.2:10501"},  # remote -> kept
                    {"id": "10.42.0.3:10501"},  # remote -> kept
                ]
            },
        ]
    }

    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["sccache", "--dist-status"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(status), stderr="")
        if cmd[:2] == ["ip", "-o"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="1: lo    inet 127.0.0.1/8 scope host lo\n2: eth0    inet 192.168.8.172/24 scope global eth0\n",
                stderr="",
            )
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr("bakar.commands.clean_cache.subprocess.run", fake_run)

    assert _resolve_secondaries() == ["10.42.0.2", "10.42.0.3"]


# ---------------------------------------------------------------------------
# _parse_dist_status_servers
# ---------------------------------------------------------------------------


def test_parse_dist_status_dedupes_and_strips_port() -> None:
    """Ports are stripped, dupes collapsed, order preserved."""
    status = json.dumps(
        {
            "SchedulerStatus": [
                None,
                {"servers": [{"id": "10.0.0.5:1"}, {"id": "10.0.0.5:2"}, {"id": "10.0.0.6:1"}]},
            ]
        }
    )
    assert _parse_dist_status_servers(status, set()) == ["10.0.0.5", "10.0.0.6"]


def test_parse_dist_status_malformed_json_returns_empty() -> None:
    """Non-JSON input is swallowed to []."""
    assert _parse_dist_status_servers("not json", set()) == []


# ---------------------------------------------------------------------------
# _empty_dir_in_place (NFS-safe wipe)
# ---------------------------------------------------------------------------


def test_empty_dir_in_place_keeps_root_removes_children(tmp_path: Path) -> None:
    """All children go; the root dir (and its inode) survives."""
    root = tmp_path / "sstate"
    root.mkdir()
    root_inode = root.stat().st_ino
    (root / "file.zst").write_bytes(b"x")
    sub = root / "ab"
    sub.mkdir()
    (sub / "nested.tar").write_bytes(b"y")

    _empty_dir_in_place(root)

    assert root.is_dir(), "root dir must be preserved (NFS export inode)"
    assert root.stat().st_ino == root_inode, "root inode changed - NFS mounts would break"
    assert list(root.iterdir()) == [], "root should be empty after the in-place wipe"


def test_empty_dir_in_place_creates_missing_root(tmp_path: Path) -> None:
    """A missing sstate dir is created (bakar doctor needs it to exist)."""
    root = tmp_path / "does-not-exist-yet"
    _empty_dir_in_place(root)
    assert root.is_dir()


# ---------------------------------------------------------------------------
# CliRunner: --full dry-run and real run
# ---------------------------------------------------------------------------


def _patch_no_secondaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECONDARY_NODES", raising=False)
    monkeypatch.setattr("bakar.commands.clean_cache.shutil.which", lambda _n: None)


def test_full_dry_run_prints_plan_and_runs_no_destructive_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--full --dry-run`` prints the plan and calls NO destructive subprocess.

    ``SECONDARY_NODES`` is deliberately left unset and ``sccache`` is reported as
    present on PATH: resolving secondaries would otherwise require a live
    ``sccache --dist-status`` call, which a dry-run must never trigger (P1-2).
    The plan therefore names the deferred step generically instead of printing
    resolved node names.
    """
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    (sstate / "keep.zst").write_bytes(b"payload")
    monkeypatch.setenv("SSTATE_DIR", str(sstate))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SECONDARY_NODES", raising=False)
    monkeypatch.setattr("bakar.commands.clean_cache.shutil.which", lambda _n: "/usr/bin/sccache")

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "bakar.commands.clean_cache.subprocess.run",
        lambda cmd, **_k: calls.append(list(cmd)),
    )

    build = tmp_path / "build-qemuarm64"
    build.mkdir()

    result = runner.invoke(app, ["clean-cache", "--full", "--build-dir", str(build), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output, result.output
    assert "secondary" in result.output.lower(), result.output
    assert "sccache-server" in result.output, result.output
    assert calls == [], f"dry-run must not invoke any subprocess (including sccache --dist-status): {calls}"
    assert (sstate / "keep.zst").exists(), "dry-run must not empty sstate"
    assert build.is_dir(), "dry-run must not wipe the build dir"


def test_full_dry_run_missing_sstate_dir_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No resolvable SSTATE_DIR must error and exit 2, not silently fall back to a default path."""
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    monkeypatch.setattr("bakar.commands.clean_cache._state._USER_CONFIG", None)
    monkeypatch.setenv("HOME", str(tmp_path))

    result = runner.invoke(app, ["clean-cache", "--full", "--dry-run"])

    assert result.exit_code == 2, result.output
    assert "SSTATE_DIR not set" in result.output, result.output


def test_full_yes_empties_sstate_resets_server_and_ssh_secondaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--full -y`` empties sstate in place, resets the server, and ssh's each secondary."""
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    sstate_inode = sstate.stat().st_ino
    (sstate / "old.zst").write_bytes(b"payload")
    monkeypatch.setenv("SSTATE_DIR", str(sstate))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SECONDARY_NODES", "10.42.0.2")

    # A populated sccache client cache under the redirected HOME.
    cache = tmp_path / ".cache" / "sccache"
    cache.mkdir(parents=True)
    (cache / "obj").write_bytes(b"cached")

    build = tmp_path / "build-qemuarm64"
    build.mkdir()
    (build / "tmp").mkdir()

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "bakar.commands.clean_cache.subprocess.run",
        lambda cmd, **_k: calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0),
    )

    result = runner.invoke(app, ["clean-cache", "--full", "--build-dir", str(build), "-y"])

    assert result.exit_code == 0, result.output

    # NFS-safety: the sstate ROOT dir survives (same inode); its contents are gone.
    assert sstate.is_dir(), "sstate root must not be removed"
    assert sstate.stat().st_ino == sstate_inode, "sstate root inode changed - NFS regression"
    assert not (sstate / "old.zst").exists(), "sstate contents should be emptied"

    # Local wipes really happened.
    assert not build.exists(), "build dir should be wiped"
    assert not cache.exists(), "sccache client cache should be wiped"

    # Daemon stop + local server reset + secondary ssh all issued.
    assert ["pkill", "-f", "^/usr/bin/sccache$"] in calls, calls
    assert any(c[:2] == ["bash", "-c"] and "sccache-server" in c[2] for c in calls), calls
    ssh_calls = [c for c in calls if c[:1] == ["ssh"]]
    assert ssh_calls, f"expected an ssh reset to the secondary: {calls}"
    ssh = ssh_calls[0]
    assert "10.42.0.2" in ssh, ssh
    assert str(build) in ssh[-1], ssh  # remote wipes the per-node build dir
    assert "sccache-server" in ssh[-1], ssh


def test_full_no_secondaries_resets_pc1_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no secondaries detected, only PC1 (local bash -c) is reset - no ssh."""
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    monkeypatch.setenv("SSTATE_DIR", str(sstate))
    monkeypatch.setenv("HOME", str(tmp_path))
    _patch_no_secondaries(monkeypatch)

    build = tmp_path / "build-x"
    build.mkdir()

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "bakar.commands.clean_cache.subprocess.run",
        lambda cmd, **_k: calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0),
    )

    result = runner.invoke(app, ["clean-cache", "--full", "--build-dir", str(build), "-y"])

    assert result.exit_code == 0, result.output
    assert not any(c[:1] == ["ssh"] for c in calls), f"no ssh expected without secondaries: {calls}"
    assert any(c[:2] == ["bash", "-c"] for c in calls), calls
    assert "reset PC1 only" in result.output, result.output


def test_full_stops_daemons_before_emptying_sstate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """hashserv/prserv are stopped before the sstate wipe runs (P1-12 ordering)."""
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    (sstate / "old.zst").write_bytes(b"payload")
    monkeypatch.setenv("SSTATE_DIR", str(sstate))
    monkeypatch.setenv("HOME", str(tmp_path))
    _patch_no_secondaries(monkeypatch)

    build = tmp_path / "build-x"
    build.mkdir()

    order: list[str] = []
    monkeypatch.setattr("bakar.hashserv.stop", lambda _state_key: order.append("hashserv") or True)
    monkeypatch.setattr(
        "bakar.prserv.stop",
        lambda _state_key, *, binary_root, bind_host="localhost": order.append("prserv") or False,
    )

    import bakar.commands.clean_cache as clean_cache_mod

    real_empty = clean_cache_mod._empty_dir_in_place

    def _tracked_empty(path):
        order.append("empty_sstate")
        return real_empty(path)

    monkeypatch.setattr("bakar.commands.clean_cache._empty_dir_in_place", _tracked_empty)
    monkeypatch.setattr(
        "bakar.commands.clean_cache.subprocess.run",
        lambda cmd, **_k: subprocess.CompletedProcess(cmd, 0),
    )

    result = runner.invoke(app, ["clean-cache", "--full", "--build-dir", str(build), "-y"])

    assert result.exit_code == 0, result.output
    assert order == ["hashserv", "prserv", "empty_sstate"], order


def test_remote_reset_cmd_shell_quotes_build_dirs() -> None:
    """Build dir paths are shell-quoted so a path with spaces/metacharacters is safe (P1-3b)."""
    import shlex
    from pathlib import Path

    from bakar.commands.clean_cache import _remote_reset_cmd

    dangerous = Path("/tmp/build dir; rm -rf other")
    cmd = _remote_reset_cmd([dangerous], "echo reset")

    assert f"rm -rf {shlex.quote(str(dangerous))}; " in cmd, cmd
    assert cmd.endswith("echo reset")
