"""``_clean_build_dir`` must also wipe a host-mode ``local_tmpdir_base`` override.

The override relocates the build TMPDIR to node-local disk, outside the build
directory; a clean that only removes ``bsp_root/build_dir_name`` would strand
the redirected (potentially ~200G) tmp.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

import pytest
import typer

from bakar.commands._helpers import _clean_build_dir
from bakar.config import BuildConfig

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from pathlib import Path


def _cfg(workspace: Path, *, local_tmpdir_base: str | None, host_mode: bool) -> BuildConfig:
    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",
        machine="imx8mp-var-dart",
        distro="fsl-imx-wayland",
        image="core-image-minimal",
        manifest="",
        repo_url="",
        repo_branch="scarthgap",
        kas_container_image="",
        host_mode=host_mode,
        local_tmpdir_base=local_tmpdir_base,
    )


@pytest.mark.unit
def test_clean_build_dir_removes_override_tmpdir(tmp_path: Path) -> None:
    """An active override -> the out-of-tree resolved TMPDIR is removed too."""
    workspace = tmp_path / "ws"
    local_base = tmp_path / "local-tmp"
    cfg = _cfg(workspace, local_tmpdir_base=str(local_base), host_mode=True)

    build_dir = cfg.bsp_root / cfg.build_dir_name
    (build_dir / "conf").mkdir(parents=True)
    tmpdir = cfg.resolved_tmpdir
    (tmpdir / "work").mkdir(parents=True)
    assert not tmpdir.is_relative_to(build_dir)

    _clean_build_dir(cfg)

    assert not build_dir.exists()
    assert not tmpdir.exists()


@pytest.mark.unit
def test_clean_build_dir_knob_unset_only_removes_build_dir(tmp_path: Path) -> None:
    """No override -> resolved TMPDIR sits under build_dir; nothing external is touched."""
    workspace = tmp_path / "ws"
    sibling = workspace / "nxp" / "keep-me"
    sibling.mkdir(parents=True)
    cfg = _cfg(workspace, local_tmpdir_base=None, host_mode=True)

    build_dir = cfg.bsp_root / cfg.build_dir_name
    (build_dir / "tmp").mkdir(parents=True)
    assert cfg.resolved_tmpdir.is_relative_to(build_dir)

    _clean_build_dir(cfg)

    assert not build_dir.exists()
    assert sibling.exists()


@pytest.mark.unit
def test_clean_build_dir_residue_exits_1_without_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial removal names the surviving path, never says "removed", and exits 1."""
    cfg = _cfg(tmp_path / "ws", local_tmpdir_base=None, host_mode=True)
    build_dir = cfg.bsp_root / cfg.build_dir_name
    (build_dir / "tmp").mkdir(parents=True)
    stuck = build_dir / "tmp" / "stuck"

    printed: list[str] = []
    monkeypatch.setattr("bakar.commands.console.print", lambda *a, **kw: printed.append(str(a[0])))
    monkeypatch.setattr("bakar.fsremove.parallel_rmtree", lambda root, description="": (stuck, OSError("busy")))

    with pytest.raises(typer.Exit) as excinfo:
        _clean_build_dir(cfg)

    assert excinfo.value.exit_code == 1
    out = " ".join(printed)
    assert "could not fully remove" in out
    assert "stuck" in out
    assert "busy" in out
    assert "removed" not in out.replace("could not fully remove", "")


@pytest.mark.unit
def test_remove_tree_report_returns_false_and_prints_the_leftover_without_exiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reporting function returns False and names the residue; it never exits."""
    from bakar.commands import console
    from bakar.commands._helpers import _remove_tree_report

    target = tmp_path / "tree"
    stuck = target / "stuck"
    err = OSError("busy")
    err.residue_count = 3  # ty: ignore[unresolved-attribute]
    monkeypatch.setattr("bakar.fsremove.parallel_rmtree", lambda root, description="": (stuck, err))

    with console.capture() as cap:
        result = _remove_tree_report(target, "tree")

    out = cap.get()
    assert result is False
    assert "could not fully remove" in out
    assert "stuck" in out
    assert "busy" in out
    assert "(+2 more undeletable entries)" in out
    assert "removed" not in out.replace("could not fully remove", "")


@pytest.mark.unit
def test_remove_tree_or_exit_still_exits_one_on_a_leftover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The exiting wrapper raises typer.Exit(1) when the report function says False."""
    from bakar.commands._helpers import _remove_tree_or_exit

    target = tmp_path / "tree"
    monkeypatch.setattr(
        "bakar.fsremove.parallel_rmtree", lambda root, description="": (target / "stuck", OSError("busy"))
    )

    with pytest.raises(typer.Exit) as excinfo:
        _remove_tree_or_exit(target, "tree")

    assert excinfo.value.exit_code == 1


@pytest.mark.unit
def test_clean_build_dir_absent_directory_does_not_fail(tmp_path: Path) -> None:
    """Nothing to remove is a no-op, not an error."""
    cfg = _cfg(tmp_path / "ws", local_tmpdir_base=None, host_mode=True)
    _clean_build_dir(cfg)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("]y.go", "]y.go"),
        ("[id].js", "[id].js"),
        ("x[", "x["),
        ("evil\x1b]0;pwn\x07.txt", "evil]0;pwn.txt"),
    ],
)
def test_residue_report_neutralizes_filenames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, expected: str
) -> None:
    """Build-tree filenames reach Rich markup and the terminal only after neutralize()."""
    from bakar.commands import console

    cfg = _cfg(tmp_path / "ws", local_tmpdir_base=None, host_mode=True)
    build_dir = cfg.bsp_root / cfg.build_dir_name
    (build_dir / "tmp").mkdir(parents=True)
    stuck = build_dir / "tmp" / name
    monkeypatch.setattr("bakar.fsremove.parallel_rmtree", lambda root, description="": (stuck, OSError(f"busy {name}")))

    with console.capture() as cap, pytest.raises(typer.Exit):
        _clean_build_dir(cfg)

    out = cap.get()
    assert "could not fully remove" in out
    assert expected in out
    assert "\x1b]" not in out


# ---------------------------------------------------------------------------
# Removal gate: a live build blocks the deletion, an unreadable lock does not
# ---------------------------------------------------------------------------


def _live_cfg(tmp_path: Path) -> tuple[BuildConfig, Path]:
    cfg = _cfg(tmp_path / "ws", local_tmpdir_base=None, host_mode=True)
    build_dir = cfg.bsp_root / cfg.build_dir_name
    (build_dir / "tmp").mkdir(parents=True)
    return cfg, build_dir


@pytest.mark.unit
def test_clean_build_dir_refuses_when_a_peer_holds_the_marker(tmp_path: Path) -> None:
    from bakar import build_stop
    from bakar.commands import console

    cfg, build_dir = _live_cfg(tmp_path)
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("peer-node-that-is-not-this-host")

    with console.capture() as cap, pytest.raises(typer.Exit) as excinfo:
        _clean_build_dir(cfg)

    assert excinfo.value.exit_code == 1
    assert (build_dir / "tmp").exists()
    out = "".join(cap.get().split())
    assert "peer-node-that-is-not-this-host" in out
    assert str(marker) in out
    assert "byhand" in out


@pytest.mark.unit
def test_clean_build_dir_refuses_when_a_local_build_is_live(
    tmp_path: Path, bitbake_server: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]]
) -> None:
    from bakar import build_stop
    from bakar.commands import console

    cfg, build_dir = _live_cfg(tmp_path)
    build_stop.lock_marker_path(cfg).write_text(socket.gethostname())
    with bitbake_server(build_dir, children="worker") as server:
        (build_dir / "bitbake.lock").write_text(f"{server.pid}\n")

        with console.capture() as cap, pytest.raises(typer.Exit) as excinfo:
            _clean_build_dir(cfg)

    assert excinfo.value.exit_code == 1
    assert (build_dir / "tmp").exists()
    out = "".join(cap.get().split())
    assert str(server.pid) in out
    assert "byhand" in out


@pytest.mark.unit
def test_clean_build_dir_refuses_while_a_childless_server_has_a_live_launch_record(
    tmp_path: Path,
    bitbake_server: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """bitbake initialises tasks before it starts a worker; the build is live all the same."""
    from bakar import build_stop
    from bakar.commands import console

    cfg, build_dir = _live_cfg(tmp_path)
    build_stop.lock_marker_path(cfg).write_text(socket.gethostname())
    run_dir = cfg.runs_dir / "20261017-120000-4242"
    run_dir.mkdir(parents=True)
    with bitbake_server(build_dir) as server, live_child("kas-container") as wrapper:
        (build_dir / "bitbake.lock").write_text(f"{server.pid}\n")
        build_stop.write_launch_record(run_dir, pgid=wrapper.pid, mode="host")

        with console.capture() as cap, pytest.raises(typer.Exit) as excinfo:
            _clean_build_dir(cfg)

    assert excinfo.value.exit_code == 1
    assert (build_dir / "tmp").exists()
    assert str(server.pid) in "".join(cap.get().split())


@pytest.mark.unit
def test_clean_build_dir_refuses_a_build_in_its_checkout_phase(
    tmp_path: Path,
    live_child: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]],
) -> None:
    """kas has launched and written its record, but bitbake has not created the lock yet."""
    from bakar import build_stop
    from bakar.commands import console

    cfg, build_dir = _live_cfg(tmp_path)
    assert not (build_dir / "bitbake.lock").exists()
    run_dir = cfg.runs_dir / "20261017-120000-4242"
    run_dir.mkdir(parents=True)
    with live_child("kas-container") as wrapper:
        build_stop.write_launch_record(run_dir, pgid=wrapper.pid, mode="host")

        with console.capture() as cap, pytest.raises(typer.Exit) as excinfo:
            _clean_build_dir(cfg)

    assert excinfo.value.exit_code == 1
    assert (build_dir / "tmp").exists()
    assert str(wrapper.pid) in "".join(cap.get().split())


@pytest.mark.unit
def test_clean_build_dir_ignores_an_idle_local_cooker(
    tmp_path: Path, bitbake_server: Callable[..., AbstractContextManager[subprocess.Popen[bytes]]]
) -> None:
    from bakar import build_stop

    cfg, build_dir = _live_cfg(tmp_path)
    build_stop.lock_marker_path(cfg).write_text(socket.gethostname())
    with bitbake_server(build_dir, children="service") as server:
        (build_dir / "bitbake.lock").write_text(f"{server.pid}\n")
        _clean_build_dir(cfg)

    assert not build_dir.exists()


@pytest.mark.unit
def test_clean_build_dir_warns_and_proceeds_when_unattributable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bakar import build_stop
    from bakar.commands import console

    cfg, build_dir = _live_cfg(tmp_path)
    marker = build_stop.lock_marker_path(cfg)
    refusal = build_stop.LockRefusal(reason="unattributable", marker=marker)
    monkeypatch.setattr("bakar.build_stop.lock_mutation_guard", lambda c: refusal)
    monkeypatch.setattr("bakar.steps.kas_lock.local_build_holder", lambda c: None)

    with console.capture() as cap:
        _clean_build_dir(cfg)

    assert not build_dir.exists()
    assert str(marker) in "".join(cap.get().split())


@pytest.mark.unit
def test_clean_build_dir_proceeds_when_the_gate_has_no_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, build_dir = _live_cfg(tmp_path)
    monkeypatch.setattr("bakar.build_stop.lock_mutation_guard", lambda c: None)
    monkeypatch.setattr("bakar.steps.kas_lock.local_build_holder", lambda c: None)

    _clean_build_dir(cfg)

    assert not build_dir.exists()


@pytest.mark.unit
def test_clean_build_dir_gate_runs_before_any_removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, _ = _live_cfg(tmp_path)
    events: list[str] = []

    def guard(c: BuildConfig) -> None:
        events.append("guard")

    def holder(c: BuildConfig) -> None:
        events.append("holder")

    def rmtree(root: Path, description: str = "") -> None:
        events.append("rmtree")

    monkeypatch.setattr("bakar.build_stop.lock_mutation_guard", guard)
    monkeypatch.setattr("bakar.steps.kas_lock.local_build_holder", holder)
    monkeypatch.setattr("bakar.fsremove.parallel_rmtree", rmtree)

    _clean_build_dir(cfg)

    assert events == ["guard", "holder", "rmtree"]


@pytest.mark.unit
def test_clean_build_dir_warns_and_proceeds_on_shared_inaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bakar import build_stop
    from bakar.commands import console

    cfg, build_dir = _live_cfg(tmp_path)
    marker = build_stop.lock_marker_path(cfg)
    refusal = build_stop.LockRefusal(reason="shared-inaction", marker=marker)
    monkeypatch.setattr("bakar.build_stop.lock_mutation_guard", lambda c: refusal)
    monkeypatch.setattr("bakar.steps.kas_lock.local_build_holder", lambda c: None)
    monkeypatch.setattr("bakar.steps.kas_lock._MARKER_REREAD_DELAY_S", 0)

    with console.capture() as cap:
        _clean_build_dir(cfg)

    assert not build_dir.exists()
    out = "".join(cap.get().split())
    assert "warning" in out
    assert str(marker) in out


@pytest.mark.unit
def test_clean_build_dir_rereads_after_one_failed_marker_read_and_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single OSError on the marker read must not delete a peer's build."""
    from pathlib import Path as RealPath

    from bakar import build_stop

    cfg, build_dir = _live_cfg(tmp_path)
    marker = build_stop.lock_marker_path(cfg)
    marker.write_text("peer-node-that-is-not-this-host")
    monkeypatch.setattr("bakar.mounts.is_path_on_nfs", lambda p: True)
    monkeypatch.setattr("bakar.steps.kas_lock._MARKER_REREAD_DELAY_S", 0)
    real = RealPath.read_bytes
    calls = {"n": 0}

    def flaky(self: Path) -> bytes:
        if self == marker and calls["n"] == 0:
            calls["n"] += 1
            raise OSError("EIO")
        return real(self)

    monkeypatch.setattr(RealPath, "read_bytes", flaky)

    with pytest.raises(typer.Exit) as excinfo:
        _clean_build_dir(cfg)

    assert excinfo.value.exit_code == 1
    assert calls["n"] == 1
    assert (build_dir / "tmp").exists()


@pytest.mark.unit
def test_held_locally_refusal_omits_the_marker_hint_when_no_marker_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bakar.commands import console

    cfg, build_dir = _live_cfg(tmp_path)
    monkeypatch.setattr("bakar.build_stop.lock_mutation_guard", lambda c: None)
    monkeypatch.setattr("bakar.steps.kas_lock.local_build_holder", lambda c: 4242)

    with console.capture() as cap, pytest.raises(typer.Exit) as excinfo:
        _clean_build_dir(cfg)

    assert excinfo.value.exit_code == 1
    out = "".join(cap.get().split())
    assert "4242" in out
    assert "byhand" not in out
    assert (build_dir / "tmp").exists()


@pytest.mark.unit
def test_removal_moves_the_tree_aside_so_a_marker_published_during_the_delete_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build starting mid-delete creates a fresh directory the removal never touches."""
    from bakar.fsremove import parallel_rmtree

    cfg, build_dir = _live_cfg(tmp_path)
    (build_dir / "tmp" / "work").mkdir()

    def racing_rmtree(root: Path, description: str = "") -> object:
        # A peer starts after the gate, while the delete is running.
        build_dir.mkdir()
        (build_dir / ".bakar-lock-host").write_text("peer-node")
        (build_dir / "bitbake.lock").write_text("1\n")
        return parallel_rmtree(root, description)

    monkeypatch.setattr("bakar.build_stop.lock_mutation_guard", lambda c: None)
    monkeypatch.setattr("bakar.steps.kas_lock.local_build_holder", lambda c: None)
    monkeypatch.setattr("bakar.fsremove.parallel_rmtree", racing_rmtree)

    _clean_build_dir(cfg)

    assert (build_dir / ".bakar-lock-host").read_text() == "peer-node"
    assert (build_dir / "bitbake.lock").exists()
    assert not list(build_dir.parent.glob(".bakar-trash-*"))


@pytest.mark.unit
def test_removal_falls_back_in_place_when_the_rename_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path as RealPath

    from bakar.commands import console

    cfg, build_dir = _live_cfg(tmp_path)
    monkeypatch.setattr("bakar.build_stop.lock_mutation_guard", lambda c: None)
    monkeypatch.setattr("bakar.steps.kas_lock.local_build_holder", lambda c: None)

    def no_rename(self: Path, target: object) -> None:
        raise OSError("EXDEV")

    monkeypatch.setattr(RealPath, "rename", no_rename)

    with console.capture() as cap:
        _clean_build_dir(cfg)

    assert not build_dir.exists()
    assert "in place" in cap.get()


@pytest.mark.unit
def test_second_removal_gets_its_own_trash_when_the_first_leaves_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two targets sharing a parent never collide on one trash name."""
    from bakar.commands._helpers import _remove_tree_or_exit
    from bakar.fsremove import parallel_rmtree

    first = tmp_path / "first"
    second = tmp_path / "second"
    (first / "x").mkdir(parents=True)
    (second / "y").mkdir(parents=True)
    seen: list[Path] = []

    def remover(root: Path, description: str = "") -> tuple[Path, OSError] | None:
        seen.append(root)
        if root.name.endswith("first"):
            return (root / "x", OSError("busy"))  # residue stays behind
        return parallel_rmtree(root, description)

    monkeypatch.setattr("bakar.fsremove.parallel_rmtree", remover)

    with pytest.raises(typer.Exit):
        _remove_tree_or_exit(first, "first", via_trash=True)
    _remove_tree_or_exit(second, "second", via_trash=True)

    assert [p.name.rsplit("-", 1)[-1] for p in seen] == ["first", "second"]
    assert len({p.name for p in seen}) == 2
    assert not second.exists()
    assert not seen[1].exists()
