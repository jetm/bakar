"""Tests for the tool-install and image-pull actions of ``bakar setup``.

Covers ``KasInstallAction`` (kas always via ``uv tool install``, never a distro
package), ``DockerPullAction`` (image comes from a constructor argument, not
``resolve()``), and the advisory-only ``docker_engine_advice`` helper (text on
every distro, including unknown, and never an Action).
"""

from __future__ import annotations

from bakar.setup.actions.base import Action, RunCommand
from bakar.setup.actions.tools import (
    DockerPullAction,
    KasInstallAction,
    docker_engine_advice,
)


def _profile():
    """A minimal stand-in profile; these actions ignore most fields."""
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


def test_kas_action_is_an_action_remediating_host_tools() -> None:
    action = KasInstallAction()
    assert isinstance(action, Action)
    assert action.check_name == "host-tools"
    assert action.needs_root is False


def test_kas_installs_via_uv_tool_never_a_distro_package() -> None:
    """kas always installs through `uv tool install`, never pacman/apt/dnf."""
    ops = KasInstallAction().operations()
    assert ops == [RunCommand(argv=["uv", "tool", "install", "kas"], needs_root=False)]
    argv = ops[0].argv
    assert argv[:3] == ["uv", "tool", "install"]
    for pkg_tool in ("pacman", "apt", "apt-get", "dnf"):
        assert pkg_tool not in argv


def test_kas_is_satisfied_when_kas_on_path(monkeypatch) -> None:
    monkeypatch.setattr("bakar.setup.actions.tools.shutil.which", lambda _name: "/usr/bin/kas")
    assert KasInstallAction().is_satisfied(_profile()) is True


def test_kas_not_satisfied_when_kas_absent(monkeypatch) -> None:
    monkeypatch.setattr("bakar.setup.actions.tools.shutil.which", lambda _name: None)
    assert KasInstallAction().is_satisfied(_profile()) is False


def test_docker_pull_uses_constructor_image_not_resolve() -> None:
    """The image is taken verbatim from the constructor argument."""
    action = DockerPullAction("jetm/kas-build-env:latest")
    assert isinstance(action, Action)
    assert action.check_name == "container-image"
    assert action.needs_root is False
    ops = action.operations()
    assert ops == [RunCommand(argv=["docker", "pull", "jetm/kas-build-env:latest"], needs_root=False)]


def test_docker_pull_unsatisfied_so_plan_decides() -> None:
    """The profile carries no image-presence field, so is_satisfied is False."""
    assert DockerPullAction("img:tag").is_satisfied(_profile()) is False


def test_docker_engine_advice_returns_per_distro_command() -> None:
    assert "pacman -S docker" in docker_engine_advice("pacman")
    assert "apt-get install" in docker_engine_advice("apt")
    assert "dnf install" in docker_engine_advice("dnf")


def test_docker_engine_advice_degrades_to_url_on_unknown_distro() -> None:
    """An unknown/None pkg manager yields advisory text, never an exception."""
    advice = docker_engine_advice(None)
    assert "docs.docker.com" in advice
    assert "docs.docker.com" in docker_engine_advice("zypper")


def test_docker_engine_advice_is_text_not_an_action() -> None:
    """The advisory path produces a plain string, never an Action object."""
    advice = docker_engine_advice(None)
    assert isinstance(advice, str)
    assert not isinstance(advice, Action)
