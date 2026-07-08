"""Tests for the tool-install and image-pull actions of ``bakar setup``.

Covers ``KasInstallAction`` (kas always via ``uv tool install``, never a distro
package), ``DockerPullAction`` (image comes from a constructor argument, not
``resolve()``), the advisory-only ``docker_engine_advice`` helper (text on
every distro, including unknown, and never an Action), and the buildtools
provisioning pair ``BuildtoolsInstallAction`` / ``BuildtoolsConfigPersistAction``
(detection reused from ``detect_buildtools``, install argv shape, and
global-config persistence).
"""

from __future__ import annotations

from pathlib import Path

from bakar.diagnostics import BuildtoolsToolchain
from bakar.setup.actions.base import Action, RunCommand
from bakar.setup.actions.tools import (
    DEFAULT_BUILDTOOLS_DIR,
    BuildtoolsConfigPersistAction,
    BuildtoolsInstallAction,
    DockerPullAction,
    KasInstallAction,
    docker_engine_advice,
)
from tests.conftest import make_host_profile


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
    assert KasInstallAction().is_satisfied(make_host_profile()) is True


def test_kas_not_satisfied_when_kas_absent(monkeypatch) -> None:
    monkeypatch.setattr("bakar.setup.actions.tools.shutil.which", lambda _name: None)
    assert KasInstallAction().is_satisfied(make_host_profile()) is False


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
    assert DockerPullAction("img:tag").is_satisfied(make_host_profile()) is False


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


def _toolchain(present: bool) -> BuildtoolsToolchain:
    return BuildtoolsToolchain(present=present, detail="test")


# ---------------------------------------------------------------------------
# BuildtoolsInstallAction
# ---------------------------------------------------------------------------


def test_buildtools_install_is_an_action_remediating_host_preflight() -> None:
    action = BuildtoolsInstallAction(install_buildtools="/ws/oe-core/scripts/install-buildtools")
    assert isinstance(action, Action)
    assert action.check_name == "host-preflight"
    assert action.needs_root is False


def test_buildtools_install_default_dir_is_host_level() -> None:
    """No install_dir given -> the host-level default under $HOME."""
    action = BuildtoolsInstallAction(install_buildtools="/ws/install-buildtools")
    assert action.install_dir == DEFAULT_BUILDTOOLS_DIR
    assert Path.home() / ".local" / "share" / "bakar" / "buildtools" == DEFAULT_BUILDTOOLS_DIR


def test_buildtools_install_operations_have_install_dir_argv() -> None:
    """operations() runs `install-buildtools -d <install_dir>`, unprivileged."""
    script = "/ws/openembedded-core/scripts/install-buildtools"
    install_dir = Path("/opt/bakar/bt")
    ops = BuildtoolsInstallAction(install_buildtools=script, install_dir=install_dir).operations()
    assert ops == [RunCommand(argv=[script, "-d", str(install_dir)], needs_root=False)]
    assert ops[0].needs_root is False


def test_buildtools_install_satisfied_when_detector_present(monkeypatch) -> None:
    monkeypatch.setattr(
        "bakar.setup.actions.tools.detect_buildtools",
        lambda: _toolchain(present=True),
    )
    action = BuildtoolsInstallAction(install_buildtools="/ws/install-buildtools")
    assert action.is_satisfied(make_host_profile()) is True


def test_buildtools_install_unsatisfied_when_detector_absent(monkeypatch) -> None:
    monkeypatch.setattr(
        "bakar.setup.actions.tools.detect_buildtools",
        lambda: _toolchain(present=False),
    )
    action = BuildtoolsInstallAction(install_buildtools="/ws/install-buildtools")
    assert action.is_satisfied(make_host_profile()) is False


# ---------------------------------------------------------------------------
# BuildtoolsConfigPersistAction
# ---------------------------------------------------------------------------


def test_buildtools_persist_is_an_action_remediating_host_preflight() -> None:
    action = BuildtoolsConfigPersistAction()
    assert isinstance(action, Action)
    assert action.check_name == "host-preflight"
    assert action.needs_root is False


def test_buildtools_persist_has_no_shell_operations() -> None:
    """The persist happens in apply(), so operations() yields nothing."""
    assert BuildtoolsConfigPersistAction().operations() == []


def test_buildtools_persist_satisfied_tracks_detector(monkeypatch) -> None:
    monkeypatch.setattr(
        "bakar.setup.actions.tools.detect_buildtools",
        lambda: _toolchain(present=True),
    )
    assert BuildtoolsConfigPersistAction().is_satisfied(make_host_profile()) is True
    monkeypatch.setattr(
        "bakar.setup.actions.tools.detect_buildtools",
        lambda: _toolchain(present=False),
    )
    assert BuildtoolsConfigPersistAction().is_satisfied(make_host_profile()) is False


def test_buildtools_persist_writes_build_key_to_global_config(tmp_path, monkeypatch) -> None:
    """apply() routes set_setting at the global config, the [build] key."""
    install_dir = tmp_path / "bt"
    install_dir.mkdir()
    (install_dir / "environment-setup-x86_64-pokysdk-linux").write_text("# env\n")
    calls: list[tuple[str, str, Path | None]] = []
    monkeypatch.setattr(
        "bakar.setup.actions.tools.set_setting",
        lambda key, value, path=None: calls.append((key, value, path)),
    )
    BuildtoolsConfigPersistAction(install_dir=install_dir).apply()
    assert calls == [("build.buildtools_dir", str(install_dir), None)]


def test_buildtools_persist_targets_explicit_global_path_not_workspace(tmp_path, monkeypatch) -> None:
    """apply(path) writes the given global config file; never a workspace .bakar.toml."""
    captured: dict[str, object] = {}

    def _fake_set_setting(key, value, path=None):
        captured["key"] = key
        captured["value"] = value
        captured["path"] = path

    monkeypatch.setattr("bakar.setup.actions.tools.set_setting", _fake_set_setting)
    install_dir = tmp_path / "bt"
    install_dir.mkdir()
    (install_dir / "environment-setup-x86_64-pokysdk-linux").write_text("# env\n")
    global_cfg = tmp_path / "config.toml"
    BuildtoolsConfigPersistAction(install_dir=install_dir).apply(global_cfg)
    assert captured["key"] == "build.buildtools_dir"
    assert captured["value"] == str(install_dir)
    assert captured["path"] == global_cfg
    assert Path(captured["path"]).name != ".bakar.toml"
