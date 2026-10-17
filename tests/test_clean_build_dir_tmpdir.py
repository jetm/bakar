"""``_clean_build_dir`` must also wipe a host-mode ``local_tmpdir_base`` override.

The override relocates the build TMPDIR to node-local disk, outside the build
directory; a clean that only removes ``bsp_root/build_dir_name`` would strand
the redirected (potentially ~200G) tmp.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import typer

from bakar.commands._helpers import _clean_build_dir
from bakar.config import BuildConfig

if TYPE_CHECKING:
    import subprocess
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
    err.residue_count = 3  # type: ignore[attr-defined]
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bitbake_child: subprocess.Popen[bytes]
) -> None:
    from bakar import build_stop
    from bakar.commands import console

    cfg, build_dir = _live_cfg(tmp_path)
    build_stop.lock_marker_path(cfg).write_text(socket.gethostname())
    (build_dir / "bitbake.lock").write_text(f"{bitbake_child.pid}\n")
    procs = SimpleNamespace(all_pids={bitbake_child.pid, bitbake_child.pid + 1})
    monkeypatch.setattr("bakar.proc_tree._collect_build_pids", lambda topdir, pgid, *a, **kw: procs)

    with console.capture() as cap, pytest.raises(typer.Exit) as excinfo:
        _clean_build_dir(cfg)

    assert excinfo.value.exit_code == 1
    assert (build_dir / "tmp").exists()
    out = "".join(cap.get().split())
    assert str(bitbake_child.pid) in out
    assert "byhand" in out


@pytest.mark.unit
def test_clean_build_dir_ignores_an_idle_local_cooker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bitbake_child: subprocess.Popen[bytes]
) -> None:
    from bakar import build_stop

    cfg, build_dir = _live_cfg(tmp_path)
    build_stop.lock_marker_path(cfg).write_text(socket.gethostname())
    (build_dir / "bitbake.lock").write_text(f"{bitbake_child.pid}\n")
    procs = SimpleNamespace(all_pids={bitbake_child.pid})
    monkeypatch.setattr("bakar.proc_tree._collect_build_pids", lambda topdir, pgid, *a, **kw: procs)

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
