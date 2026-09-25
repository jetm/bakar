"""Tests for the bounded cache-mount readiness probe in ``bakar.mounts``.

Covers ``probe_statfs``'s concurrent, deadline-bounded child processes (ready,
unresponsive, error, missing, and the forced ``LC_ALL=C`` locale), the
``/etc/fstab`` NFS-declaration reader ``fstab_nfs_entry``, and
:meth:`CacheMountStatus.blocking` via ``assess_cache_mounts``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import bakar.mounts as mounts_module
from bakar.mounts import (
    CacheMountStatus,
    _mount_entry_in,
    assess_cache_mounts,
    fstab_nfs_entry,
    is_path_on_nfs,
    probe_statfs,
)

pytestmark = pytest.mark.unit


def _write_stub_stat(tmp_path: Path, script_body: str) -> Path:
    """Write an executable ``stat`` stub first on PATH and return its bin dir."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "stat"
    stub.write_text(f"#!/bin/sh\n{script_body}\n")
    stub.chmod(0o755)
    return bin_dir


def _prepend_path(monkeypatch: pytest.MonkeyPatch, bin_dir: Path) -> None:
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


# --- probe_statfs -----------------------------------------------------------


def test_probe_statfs_exiting_stub_gives_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bin_dir = _write_stub_stat(tmp_path, "exit 0")
    _prepend_path(monkeypatch, bin_dir)

    target = tmp_path / "cache"
    results = probe_statfs([target], deadline_s=5.0)

    assert results[target] == ("ready", "")


def test_probe_statfs_wedged_stub_gives_unresponsive_within_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stub sleeping far past its deadline is killed and abandoned - the
    call returns quickly rather than blocking for the full sleep duration."""
    bin_dir = _write_stub_stat(tmp_path, "sleep 60")
    _prepend_path(monkeypatch, bin_dir)

    target = tmp_path / "cache"
    start = time.monotonic()
    results = probe_statfs([target], deadline_s=0.3)
    elapsed = time.monotonic() - start

    assert results[target] == ("unresponsive", "")
    assert elapsed < 2.0


def test_probe_statfs_error_stub_carries_stderr_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bin_dir = _write_stub_stat(tmp_path, 'echo "Connection timed out" >&2\nexit 1')
    _prepend_path(monkeypatch, bin_dir)

    target = tmp_path / "cache"
    results = probe_statfs([target], deadline_s=5.0)

    state, detail = results[target]
    assert state == "error"
    assert "Connection timed out" in detail


def test_probe_statfs_enoent_stub_gives_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bin_dir = _write_stub_stat(tmp_path, 'echo "No such file or directory" >&2\nexit 1')
    _prepend_path(monkeypatch, bin_dir)

    target = tmp_path / "cache"
    results = probe_statfs([target], deadline_s=5.0)

    state, detail = results[target]
    assert state == "missing"
    assert "No such file or directory" in detail


def test_probe_statfs_forces_lc_all_c(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The stub prints the English ENOENT text only under LC_ALL=C - proving
    the child actually received the forced locale, regardless of the outer
    test process's own LC_ALL."""
    bin_dir = _write_stub_stat(
        tmp_path,
        'if [ "$LC_ALL" = "C" ]; then\n'
        '  echo "No such file or directory" >&2\n'
        "else\n"
        '  echo "archivo o directorio no encontrado" >&2\n'
        "fi\n"
        "exit 1",
    )
    _prepend_path(monkeypatch, bin_dir)
    monkeypatch.setenv("LC_ALL", "es_ES.UTF-8")

    target = tmp_path / "cache"
    results = probe_statfs([target], deadline_s=5.0)

    state, detail = results[target]
    assert state == "missing"
    assert "No such file or directory" in detail


# --- fstab_nfs_entry ---------------------------------------------------------


def test_fstab_nfs_entry_resolves_via_parent_mountpoint() -> None:
    fstab = "nas:/export /mnt/share nfs4 rw,hard,timeo=600 0 0\n"
    entry = fstab_nfs_entry(fstab, Path("/mnt/share/sstate"))
    assert entry == ("nas:/export", "/mnt/share")


def test_fstab_nfs_entry_skips_commented_line() -> None:
    fstab = "# nas:/export /mnt/share nfs4 rw,hard,timeo=600 0 0\n"
    entry = fstab_nfs_entry(fstab, Path("/mnt/share/sstate"))
    assert entry is None


# --- classification is filesystem-I/O-free for the input path (Bug 2) ------


def _boom(*args: object, **kwargs: object) -> None:
    raise AssertionError("must not touch the filesystem to classify the input path")


def test_mount_entry_in_never_stats_the_input_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_mount_entry_in`` must classify without calling ``Path.resolve``,
    ``Path.stat``, or ``Path.lstat`` on the input path - each of those can
    hang indefinitely on a path component under a wedged NFS mount, which is
    exactly the hang ``probe_statfs`` exists to avoid."""
    monkeypatch.setattr(Path, "resolve", _boom)
    monkeypatch.setattr(Path, "stat", _boom)
    monkeypatch.setattr(Path, "lstat", _boom)

    table = "nas:/export /mnt/share nfs4 rw,hard 0 0\n"
    entry = _mount_entry_in(table, Path("/mnt/share/sstate"))
    assert entry is not None
    assert entry[2] == "nfs4"


def test_is_path_on_nfs_never_stats_the_input_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same guarantee for ``is_path_on_nfs``, which backs the lock-ownership
    gate (``nfs-safe-lock-clearing`` spec)."""
    monkeypatch.setattr(Path, "resolve", _boom)
    monkeypatch.setattr(Path, "stat", _boom)
    monkeypatch.setattr(Path, "lstat", _boom)

    def fake_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if str(self) == "/proc/mounts":
            return "nas:/export /mnt/share nfs4 rw,hard 0 0\n"
        raise OSError("unexpected read")

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert is_path_on_nfs(Path("/mnt/share/sstate")) is True


def test_fstab_nfs_entry_never_stats_the_input_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same guarantee for ``fstab_nfs_entry``."""
    monkeypatch.setattr(Path, "resolve", _boom)
    monkeypatch.setattr(Path, "stat", _boom)
    monkeypatch.setattr(Path, "lstat", _boom)

    fstab = "nas:/export /mnt/share nfs4 rw,hard,timeo=600 0 0\n"
    entry = fstab_nfs_entry(fstab, Path("/mnt/share/sstate"))
    assert entry == ("nas:/export", "/mnt/share")


def test_fstab_nfs_entry_skips_blank_lines() -> None:
    fstab = "\n   \nnas:/export /mnt/share nfs4 rw,hard,timeo=600 0 0\n"
    entry = fstab_nfs_entry(fstab, Path("/mnt/share/sstate"))
    assert entry == ("nas:/export", "/mnt/share")


def test_fstab_nfs_entry_is_component_wise_not_string_prefix() -> None:
    """``/mnt/share`` must not match ``/mnt/shared-other/x`` - covering is
    component-wise (``is_relative_to``), not a string prefix test."""
    fstab = "nas:/export /mnt/share nfs4 rw 0 0\n"
    entry = fstab_nfs_entry(fstab, Path("/mnt/shared-other/x"))
    assert entry is None


def test_fstab_nfs_entry_ignores_non_nfs_covering_entry() -> None:
    fstab = "/dev/sda1 /mnt/share ext4 defaults 0 1\n"
    entry = fstab_nfs_entry(fstab, Path("/mnt/share/sstate"))
    assert entry is None


def test_fstab_nfs_entry_longer_non_nfs_entry_wins_over_shorter_nfs_entry() -> None:
    """The longest covering entry is chosen across ALL fstypes before the NFS
    test: a local mount nested under an NFS share makes paths beneath it local,
    so the shorter NFS entry must not be reported for them."""
    fstab = "nas:/export /mnt/share nfs4 rw 0 0\n/dev/sdb1 /mnt/share/local ext4 defaults 0 2\n"
    assert fstab_nfs_entry(fstab, Path("/mnt/share/local/cache")) is None
    assert fstab_nfs_entry(fstab, Path("/mnt/share/sstate")) == ("nas:/export", "/mnt/share")


def test_fstab_nfs_entry_longer_nfs_entry_wins_over_shorter_local_entry() -> None:
    fstab = "/dev/sda1 /mnt ext4 defaults 0 1\nnas:/export /mnt/share nfs rw 0 0\n"
    assert fstab_nfs_entry(fstab, Path("/mnt/share/sstate")) == ("nas:/export", "/mnt/share")


# --- assess_cache_mounts / CacheMountStatus.blocking -------------------------


def _patch_reads(monkeypatch: pytest.MonkeyPatch, contents: dict[str, str]) -> None:
    """Patch ``Path.read_text`` so reads of the given absolute paths return
    fixed content, leaving every other path's real ``read_text`` behavior
    untouched."""
    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(self) in contents:
            return contents[str(self)]
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)


def _patch_probe(monkeypatch: pytest.MonkeyPatch, results: dict[Path, tuple[str, str]]) -> None:
    monkeypatch.setattr(mounts_module, "probe_statfs", lambda paths, *, deadline_s: results)


def test_assess_cache_mounts_declared_nfs_resolving_to_ext4_is_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = Path("/mnt/share/sstate")
    _patch_probe(monkeypatch, {target: ("ready", "")})
    _patch_reads(
        monkeypatch,
        {
            "/proc/mounts": f"/dev/sda1 {target} ext4 rw 0 0\n",
            "/etc/fstab": "nas:/export /mnt/share nfs4 rw,hard 0 0\n",
        },
    )

    statuses = assess_cache_mounts([("sstate", target, True)])

    assert len(statuses) == 1
    status = statuses[0]
    assert isinstance(status, CacheMountStatus)
    assert status.state == "ready"
    assert status.fstype == "ext4"
    assert status.declared_nfs is True
    assert status.blocking is True


def test_assess_cache_mounts_ready_nfs_is_not_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    target = Path("/mnt/share/sstate")
    _patch_probe(monkeypatch, {target: ("ready", "")})
    _patch_reads(
        monkeypatch,
        {
            "/proc/mounts": f"nas:/export {target} nfs4 rw,hard 0 0\n",
            "/etc/fstab": "nas:/export /mnt/share nfs4 rw,hard 0 0\n",
        },
    )

    statuses = assess_cache_mounts([("sstate", target, True)])

    status = statuses[0]
    assert status.fstype == "nfs4"
    assert status.declared_nfs is True
    assert status.blocking is False


def test_assess_cache_mounts_missing_not_declared_nfs_is_not_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = Path("/home/user/dl-dir")
    _patch_probe(monkeypatch, {target: ("missing", "No such file or directory")})
    _patch_reads(
        monkeypatch,
        {
            "/proc/mounts": "",
            "/etc/fstab": "",
        },
    )

    statuses = assess_cache_mounts([("dl-dir", target, False)])

    status = statuses[0]
    assert status.state == "missing"
    assert status.declared_nfs is False
    assert status.blocking is False


def test_assess_cache_mounts_unresponsive_still_names_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresponsive target now still runs the mount/fstab lookups.

    ``_mount_entry_in``/``fstab_nfs_entry`` normalize their input path with
    :func:`bakar.mounts._lexical_normalize`, which touches no filesystem
    state at all - so there is no hang risk left to defend against by
    skipping them for an unresponsive/errored target, and running them
    unconditionally means the reported status still names the mount source
    or fstab entry rather than "server unknown" for a share whose server is
    in fact known.
    """
    target = Path("/mnt/share/sstate")
    _patch_probe(monkeypatch, {target: ("unresponsive", "")})
    _patch_reads(
        monkeypatch,
        {
            "/proc/mounts": f"nas:/export {target} nfs4 rw,hard 0 0\n",
            "/etc/fstab": "nas:/export /mnt/share nfs4 rw,hard 0 0\n",
        },
    )

    statuses = assess_cache_mounts([("sstate", target, True)])

    status = statuses[0]
    assert status.state == "unresponsive"
    assert status.fstype == "nfs4"
    assert status.source == "nas:/export"
    assert status.declared_nfs is True
    assert status.blocking is True


def test_assess_cache_mounts_error_still_names_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same as above, for the ``error`` state."""
    target = Path("/mnt/share/sstate")
    _patch_probe(monkeypatch, {target: ("error", "some stat error")})
    _patch_reads(
        monkeypatch,
        {
            "/proc/mounts": f"nas:/export {target} nfs4 rw,hard 0 0\n",
            "/etc/fstab": "nas:/export /mnt/share nfs4 rw,hard 0 0\n",
        },
    )

    statuses = assess_cache_mounts([("sstate", target, True)])

    status = statuses[0]
    assert status.state == "error"
    assert status.source == "nas:/export"
    assert status.blocking is True


def test_assess_cache_mounts_noncritical_unresponsive_blocks_like_critical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-critical (e.g. ccache) target in an unresponsive state now BLOCKS
    the build exactly like a critical target - the cache-mount-readiness
    spec's "any effective cache directory" wording carries no criticality
    carve-out. ``critical`` is still stored on the status (read by
    ``check_shared_cache_mounts`` and other tests) but no longer gates
    ``.blocking``."""
    critical_target = Path("/mnt/share/sstate")
    noncritical_target = Path("/mnt/share/ccache")
    _patch_probe(
        monkeypatch,
        {
            critical_target: ("unresponsive", ""),
            noncritical_target: ("unresponsive", ""),
        },
    )
    _patch_reads(monkeypatch, {"/proc/mounts": "", "/etc/fstab": ""})

    statuses = assess_cache_mounts(
        [
            ("sstate", critical_target, True),
            ("ccache", noncritical_target, False),
        ]
    )

    by_label = {s.label: s for s in statuses}
    assert by_label["sstate"].critical is True
    assert by_label["sstate"].blocking is True

    assert by_label["ccache"].critical is False
    assert by_label["ccache"].blocking is True


# --- bounded, symlink-aware resolution (Bug 1) -------------------------------
#
# The prior ``_lexical_normalize``-only implementation silently dropped
# symlink resolution outright: a symlinked build/cache directory whose target
# lives on NFS matched the symlink's own (local) mountpoint instead, fail-
# opening the lock-deletion guard. These two tests cover the fix directly at
# the ``bakar.mounts`` layer: a real symlink must still resolve to its real
# target's mount entry, and a resolution child that never returns must fail
# closed (``None``/undetermined) within the bounded deadline rather than
# hanging the caller.


def test_mount_entry_in_follows_a_real_symlink_to_its_own_mountpoint(tmp_path: Path) -> None:
    """A symlinked directory must classify via its TARGET's mountpoint, not
    the symlink's own location - the exact case the lexical-only regression
    silently dropped (fail-opening the lock-deletion guard)."""
    real_target = tmp_path / "nfs-target"
    real_target.mkdir()
    link = tmp_path / "build-dir"
    link.symlink_to(real_target)

    table = f"nas:/export {real_target} nfs4 rw,hard 0 0\n/dev/sda1 {tmp_path} ext4 rw 0 0\n"

    entry = _mount_entry_in(table, link)

    assert entry is not None
    assert entry[2] == "nfs4"
    assert entry[0] == "nas:/export"


def test_is_path_on_nfs_follows_a_real_symlink_to_nfs_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same guarantee through the public tri-state classifier that backs the
    lock-deletion guard directly (``nfs-safe-lock-clearing`` spec)."""
    real_target = tmp_path / "nfs-target"
    real_target.mkdir()
    link = tmp_path / "build-dir"
    link.symlink_to(real_target)

    table = f"nas:/export {real_target} nfs4 rw,hard 0 0\n/dev/sda1 {tmp_path} ext4 rw 0 0\n"
    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if str(self) == "/proc/mounts":
            return table
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert is_path_on_nfs(link) is True


def test_mount_entry_in_follows_a_real_symlink_through_a_missing_subdirectory(tmp_path: Path) -> None:
    """The residual gap this covers: plain ``realpath`` fails ENOENT as soon
    as it reaches a missing component, BEFORE it has resolved an earlier
    symlink in the same path - so a not-yet-created leaf under a symlinked
    directory must still classify via the symlink's real target, not its own
    (unfollowed, local) spelling. ``link`` exists and resolves to an NFS
    target; ``newsubdir`` beneath it does not exist yet."""
    real_target = tmp_path / "nfs-target"
    real_target.mkdir()
    link = tmp_path / "cache-link"
    link.symlink_to(real_target)
    missing_leaf = link / "newsubdir" / "sstate"

    table = f"nas:/export {real_target} nfs4 rw,hard 0 0\n/dev/sda1 {tmp_path} ext4 rw 0 0\n"

    entry = _mount_entry_in(table, missing_leaf)

    assert entry is not None
    assert entry[2] == "nfs4"
    assert entry[0] == "nas:/export"


def test_is_path_on_nfs_follows_a_real_symlink_through_a_missing_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same guarantee through the public tri-state classifier: a missing
    component past a real, NFS-target symlink must report ``True`` (on NFS),
    never ``False`` (confirmed local via the symlink's own lexical spelling)
    - the exact misclassification that could let a daemon create SQLite state
    on NFS or let the lock-deletion guard treat a network directory as
    confirmed-local."""
    real_target = tmp_path / "nfs-target"
    real_target.mkdir()
    link = tmp_path / "cache-link"
    link.symlink_to(real_target)
    missing_leaf = link / "newsubdir" / "sstate"

    table = f"nas:/export {real_target} nfs4 rw,hard 0 0\n/dev/sda1 {tmp_path} ext4 rw 0 0\n"
    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if str(self) == "/proc/mounts":
            return table
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert is_path_on_nfs(missing_leaf) is True


def _write_stub_realpath_wedged(tmp_path: Path) -> Path:
    """Executable ``realpath`` stub that never exits - mirrors how
    ``test_probe_statfs_wedged_stub_gives_unresponsive_within_deadline`` above
    simulates a stuck child for ``stat -f``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "realpath"
    stub.write_text("#!/bin/sh\nsleep 60\n")
    stub.chmod(0o755)
    return bin_dir


def test_mount_entry_in_wedged_resolution_fails_closed_within_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``realpath`` child that never returns must not hang the caller - the
    classification fails closed to ``None`` (undetermined) once the bounded
    deadline passes, and the call must return well before the child's own
    sleep duration."""
    bin_dir = _write_stub_realpath_wedged(tmp_path)
    _prepend_path(monkeypatch, bin_dir)
    monkeypatch.setattr(mounts_module, "CACHE_PROBE_DEADLINE_S", 0.3)

    table = "nas:/export /mnt/share nfs4 rw,hard 0 0\n"
    start = time.monotonic()
    entry = _mount_entry_in(table, Path("/mnt/share/sstate"))
    elapsed = time.monotonic() - start

    assert entry is None
    assert elapsed < 2.0


def test_is_path_on_nfs_wedged_resolution_is_undetermined_not_local(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same guarantee through the public classifier: a wedged resolution must
    surface as ``None`` (undetermined, fail-closed) - NEVER ``False``
    (confirmed local), which would license the lock-deletion guard to unlink a
    peer's live lock."""
    bin_dir = _write_stub_realpath_wedged(tmp_path)
    _prepend_path(monkeypatch, bin_dir)
    monkeypatch.setattr(mounts_module, "CACHE_PROBE_DEADLINE_S", 0.3)

    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if str(self) == "/proc/mounts":
            return "nas:/export /mnt/share nfs4 rw,hard 0 0\n"
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    start = time.monotonic()
    result = is_path_on_nfs(Path("/mnt/share/sstate"))
    elapsed = time.monotonic() - start

    assert result is None
    assert elapsed < 2.0


# --- non-ENOENT resolution failures fail closed, not lexical (Bug 1) --------
#
# The narrow ``_lexical_normalize`` fallback in ``_resolve_bounded`` must only
# fire for the benign "path genuinely does not exist (yet)" case (ENOENT).
# Permission-denied, a symlink loop, or any other error must fail closed to
# ``None`` - the same fail-open bug this change already fixed once (dropping
# symlink resolution outright), reintroduced through this second code path.


def _write_stub_realpath_erroring(tmp_path: Path, stderr_line: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "realpath"
    stub.write_text(f'#!/bin/sh\necho "{stderr_line}" >&2\nexit 1\n')
    stub.chmod(0o755)
    return bin_dir


def test_resolve_bounded_permission_denied_fails_closed_to_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_dir = _write_stub_realpath_erroring(tmp_path, "realpath: x: Permission denied")
    _prepend_path(monkeypatch, bin_dir)

    target = tmp_path / "cache"
    results = mounts_module._resolve_bounded([target], deadline_s=5.0)

    assert results[target] is None


def test_resolve_bounded_symlink_loop_fails_closed_to_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bin_dir = _write_stub_realpath_erroring(tmp_path, "realpath: loopa: Too many levels of symbolic links")
    _prepend_path(monkeypatch, bin_dir)

    target = tmp_path / "cache"
    results = mounts_module._resolve_bounded([target], deadline_s=5.0)

    assert results[target] is None


def _write_stub_realpath_enoent_then_m_succeeds(tmp_path: Path, resolved_path: str) -> Path:
    """``realpath`` stub: ENOENT without ``-m``, resolves to ``resolved_path``
    when called WITH ``-m`` - mirrors the real GNU coreutils plain-vs-``-m``
    split :func:`_resolve_bounded`'s retry now relies on."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "realpath"
    stub.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ]; then\n'
        f'  echo "{resolved_path}"\n'
        "  exit 0\n"
        "fi\n"
        'echo "realpath: x: No such file or directory" >&2\n'
        "exit 1\n"
    )
    stub.chmod(0o755)
    return bin_dir


def test_resolve_bounded_enoent_retries_with_m_and_uses_its_resolved_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An ENOENT exit from the plain invocation must retry with
    ``realpath -m`` - in the same bounded mechanism, not a second unbounded
    call - rather than immediately falling back to the lexical spelling, and
    use whatever path that retry resolves."""
    resolved = str(tmp_path / "nfs-target" / "newsubdir" / "sstate")
    bin_dir = _write_stub_realpath_enoent_then_m_succeeds(tmp_path, resolved)
    _prepend_path(monkeypatch, bin_dir)

    target = tmp_path / "cache-link" / "newsubdir" / "sstate"
    results = mounts_module._resolve_bounded([target], deadline_s=5.0)

    assert results[target] == Path(resolved)


def test_resolve_bounded_enoent_then_m_also_failing_fails_closed_to_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the ``-m`` retry ALSO fails, the function must fail closed to
    ``None`` rather than falling back to the lexical guess - the exact
    fail-open :func:`_resolve_bounded` exists to close, now reachable through
    a second code path (the retry) if it silently trusted the guess here."""
    bin_dir = _write_stub_realpath_erroring(tmp_path, "realpath: x: No such file or directory")
    _prepend_path(monkeypatch, bin_dir)

    target = tmp_path / "cache"
    results = mounts_module._resolve_bounded([target], deadline_s=5.0)

    assert results[target] is None


def test_is_path_on_nfs_permission_denied_resolution_is_undetermined_not_local(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Through the public classifier: a permission-denied resolution must
    surface as undetermined - never as confirmed-local via the symlink's own
    unresolved spelling, which would license the lock-deletion guard to
    unlink a peer's live lock."""
    bin_dir = _write_stub_realpath_erroring(tmp_path, "realpath: x: Permission denied")
    _prepend_path(monkeypatch, bin_dir)

    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if str(self) == "/proc/mounts":
            return f"/dev/sda1 {tmp_path} ext4 rw 0 0\n"
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert is_path_on_nfs(tmp_path / "cache") is None


# --- duplicate target paths don't leak a subprocess/fds (Bug 4) -------------


def test_probe_statfs_duplicate_paths_both_get_a_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bin_dir = _write_stub_stat(tmp_path, "exit 0")
    _prepend_path(monkeypatch, bin_dir)

    target = tmp_path / "cache"
    results = probe_statfs([target, target], deadline_s=5.0)

    assert results[target] == ("ready", "")


def test_probe_statfs_duplicate_paths_spawn_only_one_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A duplicate path must not spawn a second ``stat`` child - the second
    ``Popen`` used to silently overwrite the first in the ``procs`` dict,
    leaking a subprocess and two fds per duplicate."""
    call_log = tmp_path / "calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "stat"
    stub.write_text(f'#!/bin/sh\necho called >> "{call_log}"\nexit 0\n')
    stub.chmod(0o755)
    _prepend_path(monkeypatch, bin_dir)

    target = tmp_path / "cache"
    probe_statfs([target, target, target], deadline_s=5.0)

    assert call_log.read_text().count("called") == 1


def test_resolve_bounded_duplicate_paths_both_get_a_result(tmp_path: Path) -> None:
    target = tmp_path / "cache"

    results = mounts_module._resolve_bounded([target, target], deadline_s=5.0)

    assert target in results
    assert results[target] is not None


def test_resolve_bounded_duplicate_paths_spawn_only_one_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    call_log = tmp_path / "realpath-calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "realpath"
    stub.write_text(f'#!/bin/sh\necho called >> "{call_log}"\nshift\nprintf "%s\\n" "$1"\n')
    stub.chmod(0o755)
    _prepend_path(monkeypatch, bin_dir)

    target = tmp_path / "cache"
    mounts_module._resolve_bounded([target, target], deadline_s=5.0)

    assert call_log.read_text().count("called") == 1


# --- escaped mount-table/fstab paths are decoded before comparison (Bug 3) --


def test_mount_entry_for_target_matches_escaped_space_mountpoint() -> None:
    """``/proc/mounts`` octal-escapes a space in the mountpoint as ``\\040`` -
    the parser must decode it before comparing, or a real space-containing
    cache directory never matches its own entry."""
    table = "nas:/export /mnt/yocto\\040cache nfs4 rw,hard 0 0\n"
    entry = mounts_module._mount_entry_for_target(table, Path("/mnt/yocto cache/sstate"))
    assert entry is not None
    assert entry[0] == "nas:/export"
    assert entry[1] == "/mnt/yocto cache"
    assert entry[2] == "nfs4"


def test_fstab_nfs_entry_for_target_matches_escaped_space_mountpoint() -> None:
    fstab = "nas:/export /mnt/yocto\\040cache nfs4 rw,hard,timeo=600 0 0\n"
    entry = mounts_module._fstab_nfs_entry_for_target(fstab, Path("/mnt/yocto cache/sstate"))
    assert entry == ("nas:/export", "/mnt/yocto cache")


def test_fstab_nfs_entry_public_wrapper_matches_escaped_space_mountpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same guarantee through the public, resolving entry point."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "realpath"
    stub.write_text("#!/bin/sh\nshift\nprintf '%s\\n' \"$1\"\n")
    stub.chmod(0o755)
    _prepend_path(monkeypatch, bin_dir)

    fstab = "nas:/export /mnt/yocto\\040cache nfs4 rw,hard,timeo=600 0 0\n"
    entry = fstab_nfs_entry(fstab, Path("/mnt/yocto cache/sstate"))
    assert entry == ("nas:/export", "/mnt/yocto cache")


# --- probe and resolution batches run concurrently, not sequentially (Bug 5)


def test_assess_cache_mounts_wedged_probe_and_resolution_return_within_one_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A wedged ``stat -f`` AND a wedged ``realpath`` together must still
    return within roughly ONE shared deadline, not the sum of two - the two
    batches now run concurrently rather than back-to-back."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("stat", "realpath"):
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nsleep 60\n")
        stub.chmod(0o755)
    _prepend_path(monkeypatch, bin_dir)

    target = tmp_path / "cache"

    start = time.monotonic()
    statuses = assess_cache_mounts([("sstate", target, True)], deadline_s=0.3)
    elapsed = time.monotonic() - start

    assert statuses[0].state == "unresponsive"
    # Sequential execution would need at least two full poll loops back to
    # back before either bounded reap even starts; a generous ceiling well
    # under that sum proves the two batches overlapped.
    assert elapsed < 1.5
