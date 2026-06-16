"""Tests for the docker-daemon remediation actions.

The load-bearing assertions per the task: the daemon.json merge preserves
pre-existing unrelated keys, uses a ``python3 -c`` round-trip (never
sed/echo/jq), backs up to ``daemon.json.bakar.bak``, and the usermod action
warns about re-login. The merge is verified by executing the embedded python3
script against a temp daemon.json with the system paths monkeypatched in.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

from bakar.setup.actions import docker
from bakar.setup.actions.base import RunCommand
from bakar.setup.profile import HostProfile

if TYPE_CHECKING:
    from pathlib import Path


def _profile(*, nofile: int | None = None, in_group: bool = False) -> HostProfile:
    return HostProfile(
        cpu_count=4,
        mem_available_gb=16.0,
        disk_free_gb=200.0,
        distro_id="arch",
        pkg_manager="pacman",
        in_docker_group=in_group,
        docker_installed=True,
        inotify_instances=8192,
        inotify_watches=1048576,
        swappiness=10,
        docker_nofile_soft=nofile,
    )


def _run_merge_script(action_op: RunCommand, daemon_path: Path, backup_path: Path) -> None:
    """Execute a merge RunCommand's python3 script against temp paths.

    Rewrites the hardcoded ``/etc/docker`` paths in the embedded script to the
    temp ones so the round-trip can run unprivileged in the test sandbox.
    """
    assert action_op.argv[:2] == ["python3", "-c"]
    script = action_op.argv[2]
    # Replace the longer (backup) path first: _DAEMON_JSON is a prefix of
    # _DAEMON_JSON_BACKUP, so a prefix-first replace would corrupt the backup
    # path string.
    script = script.replace(docker._DAEMON_JSON_BACKUP, str(backup_path))
    script = script.replace(docker._DAEMON_JSON, str(daemon_path))
    subprocess.run([sys.executable, "-c", script], check=True)


def test_ulimits_merge_uses_python3_round_trip() -> None:
    """The daemon.json merge is a python3 -c command, not sed/echo/jq."""
    op = docker.DockerUlimitsAction().operations()[0]
    assert isinstance(op, RunCommand)
    assert op.argv[0] == "python3"
    assert op.argv[1] == "-c"
    assert op.needs_root is True
    joined = " ".join(op.argv)
    for forbidden in ("sed", "echo", "jq"):
        assert forbidden not in joined


def test_ulimits_merge_preserves_preexisting_keys(tmp_path: Path) -> None:
    """Merging nofile leaves unrelated daemon.json keys untouched."""
    daemon = tmp_path / "daemon.json"
    backup = tmp_path / "daemon.json.bakar.bak"
    daemon.write_text(json.dumps({"bip": "172.30.0.1/24", "dns": ["1.1.1.1"]}))

    _run_merge_script(docker.DockerUlimitsAction().operations()[0], daemon, backup)

    result = json.loads(daemon.read_text())
    assert result["bip"] == "172.30.0.1/24"
    assert result["dns"] == ["1.1.1.1"]
    assert result["default-ulimits"]["nofile"]["Soft"] == docker.NOFILE_SOFT
    assert result["default-ulimits"]["nofile"]["Hard"] == docker._NOFILE_HARD
    # A pre-existing file is backed up before the write.
    assert json.loads(backup.read_text()) == {"bip": "172.30.0.1/24", "dns": ["1.1.1.1"]}


def test_storage_driver_merge_preserves_preexisting_keys(tmp_path: Path) -> None:
    """Merging storage-driver leaves unrelated daemon.json keys untouched."""
    daemon = tmp_path / "daemon.json"
    backup = tmp_path / "daemon.json.bakar.bak"
    daemon.write_text(json.dumps({"default-ulimits": {"nofile": {"Soft": 1024, "Hard": 2048}}}))

    _run_merge_script(docker.DockerStorageDriverAction().operations()[0], daemon, backup)

    result = json.loads(daemon.read_text())
    assert result["storage-driver"] == "overlay2"
    assert result["default-ulimits"]["nofile"]["Soft"] == 1024


def test_merge_creates_file_when_absent(tmp_path: Path) -> None:
    """An absent daemon.json yields a valid file and no backup."""
    daemon = tmp_path / "daemon.json"
    backup = tmp_path / "daemon.json.bakar.bak"

    _run_merge_script(docker.DockerUlimitsAction().operations()[0], daemon, backup)

    result = json.loads(daemon.read_text())
    assert result["default-ulimits"]["nofile"]["Soft"] == docker.NOFILE_SOFT
    assert not backup.exists()


def test_second_merge_keeps_the_original_backup(tmp_path: Path) -> None:
    """Two merges in one setup pass must leave the .bak holding the ORIGINAL file,
    not the version the first merge already modified."""
    daemon = tmp_path / "daemon.json"
    backup = tmp_path / "daemon.json.bakar.bak"
    original = {"bip": "172.30.0.1/24"}
    daemon.write_text(json.dumps(original))

    _run_merge_script(docker.DockerUlimitsAction().operations()[0], daemon, backup)
    _run_merge_script(docker.DockerStorageDriverAction().operations()[0], daemon, backup)

    # The backup is the pristine original, not the post-first-merge file.
    assert json.loads(backup.read_text()) == original
    # The live daemon.json carries both merges.
    result = json.loads(daemon.read_text())
    assert result["storage-driver"] == "overlay2"
    assert result["default-ulimits"]["nofile"]["Soft"] == docker.NOFILE_SOFT


def test_ulimits_is_satisfied_reads_profile_nofile() -> None:
    """is_satisfied reads the live nofile soft from the profile."""
    action = docker.DockerUlimitsAction()
    assert action.is_satisfied(_profile(nofile=65536)) is True
    assert action.is_satisfied(_profile(nofile=1024)) is False
    assert action.is_satisfied(_profile(nofile=None)) is False


def test_storage_driver_always_runs() -> None:
    """The storage-driver merge is idempotent and never skipped."""
    assert docker.DockerStorageDriverAction().is_satisfied(_profile()) is False


def test_daemon_action_enables_docker() -> None:
    """The docker-daemon action runs systemctl enable --now docker."""
    action = docker.DockerDaemonAction()
    assert action.check_name == "docker-daemon"
    op = action.operations()[0]
    assert isinstance(op, RunCommand)
    assert op.argv == ["systemctl", "enable", "--now", "docker"]
    assert op.needs_root is True


def test_group_action_warns_about_relogin() -> None:
    """usermod describe() and a notice op warn that re-login is required."""
    action = docker.DockerGroupAction(username="builder")
    assert "login" in action.describe().lower()
    ops = action.operations()
    usermod = ops[0]
    assert isinstance(usermod, RunCommand)
    assert usermod.argv == ["usermod", "-aG", "docker", "builder"]
    notice = " ".join(op.argv[-1] for op in ops if isinstance(op, RunCommand) and op.argv[0] == "echo")
    assert "log out" in notice.lower()


def test_group_action_is_satisfied_when_already_member() -> None:
    """The group action is satisfied when the user is already in the group."""
    action = docker.DockerGroupAction(username="builder")
    assert action.is_satisfied(_profile(in_group=True)) is True
    assert action.is_satisfied(_profile(in_group=False)) is False
