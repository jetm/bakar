"""Tests for the ``bakar clean-cache --full`` total cold-reset path.

The full reset ports scripts/clean-all-cache.sh: it empties the shared sstate in
place (NFS-safe), wipes the per-node build dir(s), the sccache client cache, and
the local ccache dir, stops the client daemon, and resets the sccache-dist server
on PC1 (local) and each secondary over ssh.

Every subprocess call (sccache, ip, pkill, bash -c server reset, ssh) is mocked -
no real sudo, ssh, systemctl, or pkill runs. Local filesystem ops (sstate empty,
build-dir wipe, cache wipe) run for real against tmp_path, with ``HOME`` redirected
so the real ``~/.cache/sccache`` is never touched.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import bakar.commands.clean_cache as clean_cache_mod
from bakar.cli import app
from bakar.commands.clean_cache import (
    _empty_dir_in_place,
    _parse_dist_status_servers,
    _resolve_secondaries,
)

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


def _patch_full_reset_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the running-build guard and the post-reset distribution check.

    The destructive-path tests exercise the reset actions only; the guard's real
    /proc scan and the verify poll are environment-dependent, so stub both.
    """
    monkeypatch.setattr("bakar.commands.clean_cache._bitbake_build_running", lambda: (False, None))
    monkeypatch.setattr("bakar.commands.clean_cache._verify_distribution", lambda: None)


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
    _patch_full_reset_gates(monkeypatch)

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
    _patch_full_reset_gates(monkeypatch)

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
    _patch_full_reset_gates(monkeypatch)

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


def test_full_wipes_local_ccache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--full -y`` wipes the local ccache dir whole (not just an age-based evict)."""
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    monkeypatch.setenv("SSTATE_DIR", str(sstate))
    monkeypatch.setenv("HOME", str(tmp_path))
    _patch_no_secondaries(monkeypatch)
    _patch_full_reset_gates(monkeypatch)

    ccache = tmp_path / "ws" / "ccache"
    ccache.mkdir(parents=True)
    (ccache / "0" / "abc").parent.mkdir(parents=True)
    (ccache / "0" / "abc").write_bytes(b"object")

    monkeypatch.setattr(
        "bakar.commands.clean_cache.subprocess.run",
        lambda cmd, **_k: subprocess.CompletedProcess(cmd, 0),
    )

    result = runner.invoke(app, ["clean-cache", "--full", "--ccache-dir", str(ccache), "-y"])

    assert result.exit_code == 0, result.output
    assert not ccache.exists(), "local ccache dir should be wiped whole under --full"


def test_full_dry_run_lists_ccache_wipe_without_deleting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ccache dir appears in the --full plan and survives a dry run."""
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    monkeypatch.setenv("SSTATE_DIR", str(sstate))
    monkeypatch.setenv("HOME", str(tmp_path))
    _patch_no_secondaries(monkeypatch)

    ccache = tmp_path / "ws" / "ccache"
    ccache.mkdir(parents=True)
    (ccache / "keep").write_bytes(b"object")

    result = runner.invoke(app, ["clean-cache", "--full", "--ccache-dir", str(ccache), "-n"])

    assert result.exit_code == 0, result.output
    # Rich soft-wraps at the console width, and pytest-xdist's tmp_path names
    # carry an extra popen-gwN segment that pushes this path past the wrap
    # point - mid-word, with no space at the break. Strip ALL whitespace
    # (not just collapse it) so a wrapped path still matches as a substring.
    assert str(ccache).replace(" ", "") in "".join(result.output.split()), result.output
    assert ccache.exists() and (ccache / "keep").exists(), "dry run must not delete the ccache dir"


def test_full_refuses_when_bitbake_build_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--full refuses (exit 1) with a running build and runs no destructive action."""
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    (sstate / "keep.zst").write_bytes(b"payload")
    monkeypatch.setenv("SSTATE_DIR", str(sstate))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "bakar.commands.clean_cache._bitbake_build_running",
        lambda: (True, "python3 /ws/bitbake/bin/bitbake -c build core-image-minimal"),
    )

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "bakar.commands.clean_cache.subprocess.run",
        lambda cmd, **_k: calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0),
    )

    result = runner.invoke(app, ["clean-cache", "--full", "-y"])

    assert result.exit_code == 1, result.output
    assert "Refusing --full" in result.output
    assert "bitbake" in result.output
    assert calls == [], f"no destructive subprocess should run when refused: {calls}"
    assert (sstate / "keep.zst").exists(), "sstate must be untouched when refused"


def test_full_force_overrides_running_build_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--force proceeds with the reset even when a build is detected as running."""
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    monkeypatch.setenv("SSTATE_DIR", str(sstate))
    monkeypatch.setenv("HOME", str(tmp_path))
    _patch_no_secondaries(monkeypatch)
    monkeypatch.setattr("bakar.commands.clean_cache._bitbake_build_running", lambda: (True, "bitbake"))
    monkeypatch.setattr("bakar.commands.clean_cache._verify_distribution", lambda: None)

    build = tmp_path / "build-x"
    build.mkdir()
    monkeypatch.setattr(
        "bakar.commands.clean_cache.subprocess.run",
        lambda cmd, **_k: subprocess.CompletedProcess(cmd, 0),
    )

    result = runner.invoke(app, ["clean-cache", "--full", "--build-dir", str(build), "-y", "--force"])

    assert result.exit_code == 0, result.output
    assert not build.exists(), "with --force the reset runs and wipes the build dir"


def test_full_dry_run_not_blocked_by_running_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dry run prints the plan even while a build runs (guard applies to real runs only)."""
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    monkeypatch.setenv("SSTATE_DIR", str(sstate))
    monkeypatch.setenv("HOME", str(tmp_path))
    _patch_no_secondaries(monkeypatch)
    monkeypatch.setattr("bakar.commands.clean_cache._bitbake_build_running", lambda: (True, "bitbake"))

    result = runner.invoke(app, ["clean-cache", "--full", "-n"])

    assert result.exit_code == 0, result.output
    assert "Plan:" in result.output
    assert "Refusing" not in result.output


def test_full_verifies_distribution_reports_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After the reset, a reachable scheduler with a server logs distribution OK."""
    from types import SimpleNamespace

    sstate = tmp_path / "sstate"
    sstate.mkdir()
    monkeypatch.setenv("SSTATE_DIR", str(sstate))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SECONDARY_NODES", raising=False)
    monkeypatch.setattr("bakar.commands.clean_cache._bitbake_build_running", lambda: (False, None))
    monkeypatch.setattr("bakar.commands.clean_cache.shutil.which", lambda _n: "/usr/bin/sccache")
    monkeypatch.setattr("bakar.commands.clean_cache.time.sleep", lambda *_a: None)
    monkeypatch.setattr(
        "bakar.commands.clean_cache.subprocess.run",
        lambda cmd, **_k: subprocess.CompletedProcess(cmd, 0, stdout=""),
    )
    monkeypatch.setattr(
        "bakar.probes.probe_cluster",
        lambda *a, **k: SimpleNamespace(
            reachable=True, capacity=SimpleNamespace(num_servers=2, num_cpus=53, in_progress=0), error=None
        ),
    )

    build = tmp_path / "build-x"
    build.mkdir()
    result = runner.invoke(app, ["clean-cache", "--full", "--build-dir", str(build), "-y"])

    assert result.exit_code == 0, result.output
    assert "distribution OK" in result.output


def test_full_verify_distribution_warns_when_no_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the scheduler reports no server after the reset, a warning is printed."""
    from types import SimpleNamespace

    sstate = tmp_path / "sstate"
    sstate.mkdir()
    monkeypatch.setenv("SSTATE_DIR", str(sstate))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SECONDARY_NODES", raising=False)
    monkeypatch.setattr("bakar.commands.clean_cache._bitbake_build_running", lambda: (False, None))
    monkeypatch.setattr("bakar.commands.clean_cache.shutil.which", lambda _n: "/usr/bin/sccache")
    monkeypatch.setattr("bakar.commands.clean_cache.time.sleep", lambda *_a: None)
    monkeypatch.setattr(
        "bakar.commands.clean_cache.subprocess.run",
        lambda cmd, **_k: subprocess.CompletedProcess(cmd, 0, stdout=""),
    )
    monkeypatch.setattr(
        "bakar.probes.probe_cluster",
        lambda *a, **k: SimpleNamespace(reachable=False, capacity=None, error="unreachable"),
    )

    build = tmp_path / "build-x"
    build.mkdir()
    result = runner.invoke(app, ["clean-cache", "--full", "--build-dir", str(build), "-y"])

    assert result.exit_code == 0, result.output
    assert "degraded" in result.output


def test_clean_cache_ctx_carries_every_flag_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every _CleanCacheCtx field equals the flag clean-cache was invoked with.

    The rest of this file asserts on printed output and filesystem effects, none
    of which would notice a field transposed or dropped while packing the ten
    parameters into the context. This one captures the context object itself and
    compares it field-by-field against a flag set where no value is the default.
    """
    import dataclasses

    import bakar.commands.clean_cache as cc

    captured: list[cc._CleanCacheCtx] = []
    monkeypatch.setattr(cc, "_clean_cache_impl", captured.append)

    sstate = tmp_path / "ctx-sstate"
    ccache = tmp_path / "ctx-ccache"
    build = tmp_path / "ctx-build"

    result = runner.invoke(
        app,
        [
            "clean-cache",
            "--older-than",
            "7",
            "--sstate-dir",
            str(sstate),
            "--ccache-dir",
            str(ccache),
            "--build-dir",
            str(build),
            "--full",
            "--no-sstate",
            "--no-ccache",
            "--yes",
            "--dry-run",
            "--force",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1, f"expected one ctx, got {len(captured)}"

    expected = {
        "older_than": 7,
        "sstate_dir": sstate,
        "ccache_dir": ccache,
        "build_dir": build,
        "full": True,
        "sstate": False,
        "ccache": False,
        "yes": True,
        "dry_run": True,
        "force": True,
    }
    ctx = captured[0]
    for field, want in expected.items():
        got = getattr(ctx, field)
        assert got == want, f"_CleanCacheCtx.{field}: expected {want!r}, got {got!r}"
    # Guards against a field being added to the dataclass but left unasserted.
    assert {f.name for f in dataclasses.fields(ctx)} == set(expected)


@pytest.mark.parametrize(
    ("flag", "field"),
    [
        ("--full", "full"),
        ("--no-sstate", "sstate"),
        ("--no-ccache", "ccache"),
        ("--yes", "yes"),
        ("--dry-run", "dry_run"),
        ("--force", "force"),
    ],
)
def test_clean_cache_ctx_boolean_flags_are_not_transposed(
    flag: str,
    field: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One boolean flag at a time moves exactly one _CleanCacheCtx field.

    The all-flipped test above cannot catch a transposition between two booleans,
    because flipping every one of them off its default gives them all the same
    value. Setting one flag per invocation pins each field to its own flag.
    """
    import bakar.commands.clean_cache as cc

    captured: list[cc._CleanCacheCtx] = []
    monkeypatch.setattr(cc, "_clean_cache_impl", captured.append)

    result = runner.invoke(app, ["clean-cache", flag])
    assert result.exit_code == 0, result.output

    defaults = {"full": False, "sstate": True, "ccache": True, "yes": False, "dry_run": False, "force": False}
    expected = {**defaults, field: not defaults[field]}
    ctx = captured[0]
    for name, want in expected.items():
        got = getattr(ctx, name)
        assert got == want, f"{flag} -> _CleanCacheCtx.{name}: expected {want!r}, got {got!r}"


# ---------------------------------------------------------------------------
# Leftovers: every step runs, every leftover is reported, exit 1 at the end
# ---------------------------------------------------------------------------


def _stuck_rmtree(stuck: set[Path]):
    """Fake ``parallel_rmtree``: report residue for roots in *stuck*, else really remove.

    Patching the residue (rather than chmod-ing a tree) keeps the tests valid when
    the suite runs as root, where nothing is undeletable.
    """
    import shutil

    def _fake(root: Path, description: str = "Removing"):
        if root in stuck:
            return (root / "stuck.o", OSError("Device or resource busy"))
        shutil.rmtree(root, ignore_errors=True)
        return None

    return _fake


def _full_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[list[str]]]:
    """Common --full setup; returns (sstate, recorded subprocess calls)."""
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    monkeypatch.setenv("SSTATE_DIR", str(sstate))
    monkeypatch.setenv("HOME", str(tmp_path))
    _patch_no_secondaries(monkeypatch)
    _patch_full_reset_gates(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "bakar.commands.clean_cache.subprocess.run",
        lambda cmd, **_k: calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0),
    )
    return sstate, calls


def _flat(output: str) -> str:
    return "".join(output.split())


def test_full_reset_runs_every_step_after_a_build_dir_leftover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A build dir that cannot be fully removed does not stop the later steps."""
    _sstate, calls = _full_env(tmp_path, monkeypatch)
    build1 = tmp_path / "build-one"
    build1.mkdir()
    ccache = tmp_path / "ws" / "ccache"
    ccache.mkdir(parents=True)
    monkeypatch.setattr("bakar.fsremove.parallel_rmtree", _stuck_rmtree({build1}))

    result = runner.invoke(
        app, ["clean-cache", "--full", "--build-dir", str(build1), "--ccache-dir", str(ccache), "-y"]
    )

    assert result.exit_code == 1, result.output
    assert not ccache.exists(), "later ccache wipe must still run"
    assert ["pkill", "-f", "^/usr/bin/sccache$"] in calls, calls
    assert any(c[:2] == ["bash", "-c"] and "sccache-server" in c[2] for c in calls), calls
    assert "could not fully remove" in result.output


def test_full_reset_exits_one_with_leftover_after_all_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The summary line follows the verification and names the leftover count."""
    _sstate, _calls = _full_env(tmp_path, monkeypatch)
    build = tmp_path / "build-one"
    build.mkdir()
    verified: list[str] = []
    monkeypatch.setattr("bakar.commands.clean_cache._verify_distribution", lambda: verified.append("v"))
    monkeypatch.setattr("bakar.fsremove.parallel_rmtree", _stuck_rmtree({build}))

    result = runner.invoke(app, ["clean-cache", "--full", "--build-dir", str(build), "-y"])

    assert result.exit_code == 1, result.output
    assert verified == ["v"], "distribution verification must still run"
    flat = _flat(result.output)
    assert "stuck.o" in flat
    assert "cold-resetfinishedwith1leftover(s);seeabove" in flat
    assert "cold-reset:cold-resetcomplete" not in flat


def test_full_reset_reports_ccache_and_cache_leftover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ccache and sccache client cache leftovers are printed and counted, not ignored."""
    _sstate, _calls = _full_env(tmp_path, monkeypatch)
    cache = tmp_path / ".cache" / "sccache"
    cache.mkdir(parents=True)
    ccache = tmp_path / "ws" / "ccache"
    ccache.mkdir(parents=True)
    monkeypatch.setattr("bakar.fsremove.parallel_rmtree", _stuck_rmtree({cache, ccache}))

    result = runner.invoke(app, ["clean-cache", "--full", "--ccache-dir", str(ccache), "-y"])

    assert result.exit_code == 1, result.output
    flat = _flat(result.output)
    assert flat.count("couldnotfullyremove") == 2, result.output
    assert "cold-resetfinishedwith2leftover(s);seeabove" in flat
    assert cache.exists()
    assert ccache.exists()


def test_full_reset_keeps_sstate_root_with_leftover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sstate child that cannot be removed is reported; the root survives; later steps run."""
    sstate, calls = _full_env(tmp_path, monkeypatch)
    root_inode = sstate.stat().st_ino
    stuck_child = sstate / "ab"
    stuck_child.mkdir()
    (sstate / "loose.zst").write_bytes(b"x")
    real_remove = clean_cache_mod._remove_child
    monkeypatch.setattr(
        "bakar.commands.clean_cache._remove_child",
        lambda c: (c / "stuck.o", OSError("Device or resource busy")) if c == stuck_child else real_remove(c),
    )

    result = runner.invoke(app, ["clean-cache", "--full", "-y"])

    assert result.exit_code == 1, result.output
    assert sstate.is_dir()
    assert sstate.stat().st_ino == root_inode
    assert not (sstate / "loose.zst").exists(), "removable children still go"
    flat = _flat(result.output)
    assert "couldnotfullyremove" in flat
    assert "stuck.o" in flat
    assert "sharedsstateemptied" not in flat
    assert any(c[:2] == ["bash", "-c"] for c in calls), "later steps must still run"


def test_full_reset_clean_run_reports_no_leftover_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing left behind: no leftover line, verification runs, exit 0."""
    sstate, _calls = _full_env(tmp_path, monkeypatch)
    (sstate / "old.zst").write_bytes(b"x")
    (sstate / "dir").mkdir()
    build = tmp_path / "build-one"
    build.mkdir()
    verified: list[str] = []
    monkeypatch.setattr("bakar.commands.clean_cache._verify_distribution", lambda: verified.append("v"))

    result = runner.invoke(app, ["clean-cache", "--full", "--build-dir", str(build), "-y"])

    assert result.exit_code == 0, result.output
    assert "could not fully remove" not in result.output
    assert "leftover" not in result.output
    assert verified == ["v"]
    assert list(sstate.iterdir()) == []
    assert not build.exists()


def test_full_reset_dry_run_plan_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dry-run plan lists the same actions in the same order and removes nothing."""
    sstate, calls = _full_env(tmp_path, monkeypatch)
    (sstate / "keep.zst").write_bytes(b"x")
    build = tmp_path / "build-one"
    build.mkdir()
    ccache = tmp_path / "ws" / "ccache"
    ccache.mkdir(parents=True)

    result = runner.invoke(app, ["clean-cache", "--full", "--build-dir", str(build), "--ccache-dir", str(ccache), "-n"])

    assert result.exit_code == 0, result.output
    plan = _flat(result.output.split("Plan:", 1)[1])
    expected = [
        "stophashserv/prservdaemons(ifrunning)",
        f"emptyinplace(keeproot):{sstate}",
        f"rm-rf(local):{build}",
        f"rm-rf(local):{tmp_path / '.cache' / 'sccache'}",
        f"rm-rf(localccache):{ccache}",
        "pkill-f'^/usr/bin/sccache$'",
    ]
    pos = 0
    for item in expected:
        found = plan.find(_flat(item), pos)
        assert found >= 0, f"{item!r} missing or out of order in plan: {plan}"
        pos = found + len(_flat(item))
    assert "resetsecondarybuildservers" in plan
    assert calls == []
    assert (sstate / "keep.zst").exists()
    assert build.is_dir()
    assert ccache.is_dir()


# ---------------------------------------------------------------------------
# K9: unlistable root, concurrent removal, symlink swap, symlinked cache roots,
# untrusted entry names
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root can list a mode-000 directory")
def test_full_reset_unlistable_sstate_root_runs_later_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mode-000 sstate root is one leftover; every later step still runs and the exit is 1."""
    sstate, calls = _full_env(tmp_path, monkeypatch)
    (sstate / "obj.zst").write_bytes(b"x")
    build = tmp_path / "build-one"
    build.mkdir()
    sstate.chmod(0)
    try:
        result = runner.invoke(app, ["clean-cache", "--full", "--build-dir", str(build), "-y"])
    finally:
        sstate.chmod(0o755)

    assert result.exit_code == 1, result.output
    assert not build.exists(), "build-dir wipe after the sstate step must still run"
    assert ["pkill", "-f", "^/usr/bin/sccache$"] in calls, calls
    assert any(c[:2] == ["bash", "-c"] for c in calls), calls
    flat = _flat(result.output)
    assert "couldnotfullyremove" in flat
    assert "cold-resetfinishedwith1leftover(s);seeabove" in flat


def test_full_reset_step_raising_oserror_is_counted_and_loop_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected OSError inside a step is printed, counted, and the loop goes on."""
    _sstate, calls = _full_env(tmp_path, monkeypatch)

    def _boom(_p: Path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("bakar.commands.clean_cache._empty_dir_in_place", _boom)

    result = runner.invoke(app, ["clean-cache", "--full", "-y"])

    assert result.exit_code == 1, result.output
    assert any(c[:2] == ["bash", "-c"] for c in calls), calls
    flat = _flat(result.output)
    assert "cold-resetstepfailed" in flat
    assert "cold-resetfinishedwith1leftover(s);seeabove" in flat


def test_empty_dir_in_place_concurrently_removed_child_is_not_leftover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entries another node removed between listing and deletion are already gone, not residue."""
    root = tmp_path / "sstate"
    root.mkdir()
    (root / "gone").write_text("x")
    (root / "d").mkdir()
    (root / "d" / "obj").write_text("x")
    real_iterdir = Path.iterdir

    def _racing_iterdir(self: Path):
        items = list(real_iterdir(self))
        if self == root:
            (root / "gone").unlink()
            (root / "d" / "obj").unlink()
            (root / "d").rmdir()
        return iter(items)

    monkeypatch.setattr(Path, "iterdir", _racing_iterdir)

    assert _empty_dir_in_place(root) == []


@pytest.mark.parametrize("swapped", ["aa", "aa/bb"], ids=["top-level", "nested"])
def test_empty_dir_in_place_swapped_symlink_deletes_nothing_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swapped: str
) -> None:
    """A dir swapped for a symlink after listing never redirects the delete outside the tree.

    The top-level swap is seen by the child's lstat; the nested one is met by the
    descriptor walk, which unlinks the link without following it.
    """
    root = tmp_path / "sstate"
    (root / "aa" / "bb").mkdir(parents=True)
    (root / "aa" / "bb" / "obj").write_text("x")
    victim = tmp_path / "fakehome"
    (victim / "bb").mkdir(parents=True)
    (victim / "precious").write_text("keep me")
    (victim / "bb" / "precious").write_text("keep me")
    real_apply = clean_cache_mod.parallel_apply
    target = root / swapped

    def _racing_apply(items, fn, description, **kw):
        target.rename(tmp_path / "moved")
        target.symlink_to(victim)
        return real_apply(items, fn, description, **kw)

    monkeypatch.setattr("bakar.commands.clean_cache.parallel_apply", _racing_apply)

    assert _empty_dir_in_place(root) == []

    assert (victim / "precious").read_text() == "keep me"
    assert (victim / "bb" / "precious").read_text() == "keep me"
    assert list(root.iterdir()) == []


def test_full_reset_symlinked_ccache_root_keeps_link_and_empties_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked ccache root is emptied through the link, which survives."""
    _full_env(tmp_path, monkeypatch)
    target = tmp_path / "real-ccache"
    (target / "a").mkdir(parents=True)
    (target / "a" / "obj").write_bytes(b"x")
    (target / "stats").write_bytes(b"y")
    link = tmp_path / "ccache-link"
    link.symlink_to(target)

    result = runner.invoke(app, ["clean-cache", "--full", "--ccache-dir", str(link), "-y"])

    assert result.exit_code == 0, result.output
    assert link.is_symlink()
    assert target.is_dir()
    assert list(target.iterdir()) == [], "the cache behind the link must be cold"


def test_wipe_local_dir_symlinked_sccache_cache_root_empties_target(tmp_path: Path) -> None:
    """The sccache client cache step takes the same symlink-resolving path."""
    target = tmp_path / "real-sccache"
    target.mkdir()
    (target / "obj").write_bytes(b"x")
    link = tmp_path / "sccache-link"
    link.symlink_to(target)

    assert clean_cache_mod._wipe_local_dir(link) == 0
    assert link.is_symlink()
    assert list(target.iterdir()) == []


def test_full_reset_escape_in_entry_name_reaches_no_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ESC in a directory name never reaches the progress description or the leftover line."""
    _full_env(tmp_path, monkeypatch)
    sstate = tmp_path / "sstate"
    evil = "evil\x1b]0;x"
    stuck = sstate / evil
    stuck.mkdir()
    descriptions: list[str] = []
    real_apply = clean_cache_mod.parallel_apply

    def _recording_apply(items, fn, description, **kw):
        descriptions.append(description)
        return real_apply(items, fn, description, **kw)

    monkeypatch.setattr("bakar.commands.clean_cache.parallel_apply", _recording_apply)
    real_remove = clean_cache_mod._remove_child
    monkeypatch.setattr(
        "bakar.commands.clean_cache._remove_child",
        lambda c: (c / evil, OSError("busy")) if c == stuck else real_remove(c),
    )

    result = runner.invoke(app, ["clean-cache", "--full", "-y"])

    assert result.exit_code == 1, result.output
    assert descriptions, "the sstate step must go through the pool"
    assert all("\x1b" not in d for d in descriptions), descriptions
    assert "\x1b" not in result.output
    assert "couldnotfullyremove" in _flat(result.output)


def test_empty_dir_in_place_escape_in_root_name_stays_out_of_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Rich progress description carries the root name neutralized."""
    root = tmp_path / "evil\x1b]0;x"
    root.mkdir()
    (root / "obj").write_bytes(b"x")
    descriptions: list[str] = []
    real_apply = clean_cache_mod.parallel_apply

    def _recording_apply(items, fn, description, **kw):
        descriptions.append(description)
        return real_apply(items, fn, description, **kw)

    monkeypatch.setattr("bakar.commands.clean_cache.parallel_apply", _recording_apply)

    assert _empty_dir_in_place(root) == []
    assert descriptions == ["Emptying evil]0;x/"], descriptions
