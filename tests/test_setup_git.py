"""Tests for the git-identity action of ``bakar setup``.

Covers ``GitConfigAction``: it sets the GLOBAL git identity (never repo-local),
takes the email/name from constructor arguments, is unprivileged, and reads the
live global identity directly for ``is_satisfied``.
"""

from __future__ import annotations

import subprocess

from bakar.setup.actions.base import Action, RunCommand
from bakar.setup.actions.git import GitConfigAction


def _profile():
    """A minimal stand-in profile; this action ignores it."""
    from bakar.setup.profile import HostProfile

    return HostProfile(
        cpu_count=4,
        mem_available_gb=16.0,
        disk_free_gb=200.0,
        distro_id="arch",
        pkg_manager="pacman",
        in_docker_group=True,
        docker_installed=True,
        inotify_instances=8192,
        inotify_watches=1048576,
        swappiness=10,
        docker_nofile_soft=65536,
    )


def _fake_run(stdout: str, returncode: int = 0):
    def run(_argv, **_kwargs):
        return subprocess.CompletedProcess(_argv, returncode, stdout=stdout, stderr="")

    return run


def test_git_action_is_an_action_remediating_git_global_config() -> None:
    action = GitConfigAction("you@example.com", "Your Name")
    assert isinstance(action, Action)
    assert action.check_name == "git-global-config"
    assert action.needs_root is False


def test_operations_write_global_identity_never_repo_local() -> None:
    """Both ops use `git config --global`, never a repo-local write."""
    ops = GitConfigAction("you@example.com", "Your Name").operations()
    assert ops == [
        RunCommand(argv=["git", "config", "--global", "user.email", "you@example.com"], needs_root=False),
        RunCommand(argv=["git", "config", "--global", "user.name", "Your Name"], needs_root=False),
    ]
    for op in ops:
        assert "--global" in op.argv
        assert "--local" not in op.argv
        assert op.needs_root is False


def test_operations_use_constructor_values() -> None:
    """The email/name come verbatim from the constructor, not config reads."""
    ops = GitConfigAction("dev@bakar.test", "Dev Person").operations()
    assert ops[0].argv[-1] == "dev@bakar.test"
    assert ops[1].argv[-1] == "Dev Person"


def test_is_satisfied_true_when_both_identities_set(monkeypatch) -> None:
    monkeypatch.setattr(
        "bakar.setup.actions.git.subprocess.run",
        _fake_run("set-value\n"),
    )
    assert GitConfigAction("you@example.com", "Your Name").is_satisfied(_profile()) is True


def test_is_satisfied_false_when_email_missing(monkeypatch) -> None:
    """An unset key (non-zero exit) reads as unsatisfied."""

    def run(argv, **_kwargs):
        if argv[-1] == "user.email":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="Your Name\n", stderr="")

    monkeypatch.setattr("bakar.setup.actions.git.subprocess.run", run)
    assert GitConfigAction("you@example.com", "Your Name").is_satisfied(_profile()) is False


def test_is_satisfied_false_when_value_empty(monkeypatch) -> None:
    """An empty (whitespace) value reads as unset."""
    monkeypatch.setattr(
        "bakar.setup.actions.git.subprocess.run",
        _fake_run("   \n"),
    )
    assert GitConfigAction("you@example.com", "Your Name").is_satisfied(_profile()) is False


def test_is_satisfied_false_when_git_missing(monkeypatch) -> None:
    """A missing git binary reads as unsatisfied, never raises."""

    def run(_argv, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr("bakar.setup.actions.git.subprocess.run", run)
    assert GitConfigAction("you@example.com", "Your Name").is_satisfied(_profile()) is False


def test_describe_mentions_global_and_values() -> None:
    text = GitConfigAction("you@example.com", "Your Name").describe()
    assert "global" in text
    assert "you@example.com" in text
    assert "Your Name" in text
