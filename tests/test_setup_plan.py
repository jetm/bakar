"""Tests for the ``SetupPlan`` builder of ``bakar setup``.

The load-bearing properties per the task and its falsifier:

- only host-environment checks map to actions; workspace/runtime checks
  (manifest, hashserv, bitbake-locks, ...) never do;
- advisory conditions (memory, disk-free, ...) are reported, never an action;
- an action whose ``is_satisfied`` is True is dropped, so a fully prepared host
  (every host check PASS) yields an empty plan;
- the FAIL-status gate is primary: an action whose ``is_satisfied`` is
  unconditionally False is still dropped when its check PASSes;
- the container-image action receives ``cfg.container_image`` from the builder;
- ``ConfigWriteAction`` is appended only when a value-applying action (sysctl /
  docker-ulimits) is in the plan.

``diagnostics.run_all`` is monkeypatched to return synthetic ``CheckResult``s so
the plan logic is tested without touching the live host.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING

from bakar.diagnostics import CheckResult, Severity, Status
from bakar.setup import plan as plan_mod
from bakar.setup.actions.cache import CacheDirsAction
from bakar.setup.actions.config_write import ConfigWriteAction
from bakar.setup.actions.docker import (
    DockerDaemonAction,
    DockerGroupAction,
    DockerStorageDriverAction,
    DockerUlimitsAction,
)
from bakar.setup.actions.git import GitConfigAction
from bakar.setup.actions.sysctl import SysctlAction
from bakar.setup.actions.tools import (
    BuildtoolsConfigPersistAction,
    BuildtoolsInstallAction,
    DockerPullAction,
    KasInstallAction,
)
from bakar.setup.profile import HostProfile

if TYPE_CHECKING:
    from collections.abc import Iterable

    import pytest

# A prepared-host baseline: every knob meets its recommended target, docker is
# installed and the user is in its group. Individual tests override fields via
# ``_profile(**overrides)`` to simulate a specific gap.
_BASE_PROFILE: dict[str, object] = {
    "cpu_count": 8,
    "mem_available_gb": 32.0,
    "disk_free_gb": 400.0,
    "distro_id": "arch",
    "pkg_manager": "pacman",
    "in_docker_group": True,
    "docker_installed": True,
    "inotify_instances": 8192,
    "inotify_watches": 1048576,
    "swappiness": 10,
    "docker_nofile_soft": 65536,
}


def _profile(**overrides: object) -> HostProfile:
    """A prepared-host profile by default; override fields to simulate gaps."""
    return HostProfile(**{**_BASE_PROFILE, **overrides})


def _fail(name: str) -> CheckResult:
    return CheckResult(name=name, severity=Severity.BLOCK, status=Status.FAIL, message=f"{name} failed")


def _ok(name: str) -> CheckResult:
    return CheckResult(name=name, severity=Severity.BLOCK, status=Status.PASS, message=f"{name} ok")


def _patch_results(monkeypatch: pytest.MonkeyPatch, results: Iterable[CheckResult]) -> None:
    """Make the plan builder see exactly ``results`` from ``run_all``."""
    monkeypatch.setattr(plan_mod, "run_all", lambda _cfg, _bsp: list(results))


_CFG = SimpleNamespace(kas_container_image="jetm/kas-build-env:latest")


def _types(actions: list) -> set[type]:
    return {type(a) for a in actions}


def test_prepared_host_yields_empty_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every host check PASS -> no actions, even for unconditionally-False actions."""
    results = [
        _ok("host-tools"),
        _ok("docker-daemon"),
        _ok("container-image"),
        _ok("docker-ulimits"),
        _ok("docker-storage-driver"),
        _ok("sysctl"),
        _ok("git-global-config"),
        _ok("cache-dirs"),
    ]
    _patch_results(monkeypatch, results)
    result = plan_mod.build(_profile(), cfg=_CFG)
    assert result.actions == []


def test_failing_checks_map_to_their_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each FAILing host check contributes its remediation action(s)."""
    results = [
        _fail("host-tools"),
        _fail("docker-daemon"),
        _fail("container-image"),
        _fail("docker-ulimits"),
        _fail("docker-storage-driver"),
        _fail("sysctl"),
        _fail("git-global-config"),
        _fail("cache-dirs"),
    ]
    _patch_results(monkeypatch, results)
    # Force every is_satisfied False so nothing is dropped at stage two: a host
    # with no docker group, low knobs, and missing cache dirs.
    profile = _profile(
        in_docker_group=False,
        inotify_instances=1024,
        inotify_watches=8192,
        swappiness=60,
        docker_nofile_soft=1024,
    )
    monkeypatch.setattr(KasInstallAction, "is_satisfied", lambda _self, _p: False)
    monkeypatch.setattr(CacheDirsAction, "is_satisfied", lambda _self, _p: False)
    monkeypatch.setattr(ConfigWriteAction, "is_satisfied", lambda _self, _p: False)
    result = plan_mod.build(profile, cfg=_CFG, git_email="me@example.com", git_name="Me")

    present = _types(result.actions)
    assert KasInstallAction in present
    assert DockerDaemonAction in present
    assert DockerGroupAction in present
    assert DockerPullAction in present
    assert DockerUlimitsAction in present
    assert DockerStorageDriverAction in present
    assert SysctlAction in present
    assert GitConfigAction in present
    assert CacheDirsAction in present
    # A value-applying action ran, so the persist action is appended.
    assert ConfigWriteAction in present


def test_workspace_runtime_checks_never_become_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A FAILing workspace/runtime check produces no action."""
    results = [
        _fail("manifest"),
        _fail("forks-linux-imx"),
        _fail("ti-config"),
        _fail("ti-layertool"),
        _fail("bbsetup-init"),
        _fail("kas-yaml-syntax"),
        _fail("hashserv"),
        _fail("bitbake-locks"),
        _fail("bitbake-override"),
        _fail("sstate-hash-leak"),
    ]
    _patch_results(monkeypatch, results)
    result = plan_mod.build(_profile(), cfg=_CFG)
    assert result.actions == []


def test_advisory_checks_are_reported_never_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """memory/disk-free/... FAILs become advisory text, not actions."""
    results = [
        _fail("memory"),
        _fail("disk-free"),
        _fail("workspace-filesystem"),
        _fail("docker-version"),
    ]
    _patch_results(monkeypatch, results)
    result = plan_mod.build(_profile(), cfg=_CFG)
    assert result.actions == []
    joined = " ".join(result.advisories)
    for advisory in ("memory", "disk-free", "workspace-filesystem", "docker-version"):
        assert advisory in joined


def test_satisfied_action_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A FAILing check whose action is already satisfied is dropped (stage two)."""
    _patch_results(monkeypatch, [_fail("sysctl")])
    # The profile already meets the sysctl recommended targets, so the action is
    # satisfied and dropped despite the FAIL status.
    result = plan_mod.build(_profile(), cfg=_CFG)
    assert result.actions == []


def test_sysctl_failure_emits_sysctl_and_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuinely low sysctl host gets the action plus the config persist."""
    _patch_results(monkeypatch, [_fail("sysctl")])
    profile = _profile(inotify_instances=1024, inotify_watches=8192, swappiness=60)
    monkeypatch.setattr(ConfigWriteAction, "is_satisfied", lambda _self, _p: False)
    result = plan_mod.build(profile, cfg=_CFG)
    present = _types(result.actions)
    assert SysctlAction in present
    assert ConfigWriteAction in present


def test_no_persist_without_a_value_applying_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """cache-dirs alone is not value-applying, so no ConfigWriteAction."""
    _patch_results(monkeypatch, [_fail("cache-dirs")])
    monkeypatch.setattr(CacheDirsAction, "is_satisfied", lambda _self, _p: False)
    result = plan_mod.build(_profile(), cfg=_CFG)
    present = _types(result.actions)
    assert CacheDirsAction in present
    assert ConfigWriteAction not in present


def test_container_image_action_gets_cfg_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """The docker-pull action is constructed with cfg.container_image."""
    _patch_results(monkeypatch, [_fail("container-image")])
    cfg = SimpleNamespace(kas_container_image="example/image:9.9")
    result = plan_mod.build(_profile(), cfg=cfg)
    pulls = [a for a in result.actions if isinstance(a, DockerPullAction)]
    assert len(pulls) == 1
    assert pulls[0].image == "example/image:9.9"


def test_docker_absent_suppresses_docker_actions_and_adds_advice(monkeypatch: pytest.MonkeyPatch) -> None:
    """No docker engine -> no docker-dependent action; advisory install text instead."""
    results = [
        _fail("host-tools"),
        _fail("docker-daemon"),
        _fail("container-image"),
        _fail("docker-ulimits"),
        _fail("docker-storage-driver"),
    ]
    _patch_results(monkeypatch, results)
    monkeypatch.setattr(KasInstallAction, "is_satisfied", lambda _self, _p: False)
    profile = _profile(docker_installed=False, in_docker_group=False, docker_nofile_soft=None)
    result = plan_mod.build(profile, cfg=_CFG)
    present = _types(result.actions)
    # kas is bakar-owned and still installs; no docker action survives.
    assert KasInstallAction in present
    assert DockerDaemonAction not in present
    assert DockerGroupAction not in present
    assert DockerPullAction not in present
    assert DockerUlimitsAction not in present
    assert DockerStorageDriverAction not in present
    # The advisory docker-engine install hint is present.
    assert any("docker" in a.lower() for a in result.advisories)


def test_git_action_omitted_without_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """git-global-config FAIL with no email/name supplied yields no git action."""
    _patch_results(monkeypatch, [_fail("git-global-config")])
    result = plan_mod.build(_profile(), cfg=_CFG)
    assert all(not isinstance(a, GitConfigAction) for a in result.actions)


def test_git_identity_absent_produces_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    """git-global-config FAIL without email/name emits an advisory, not silence."""
    _patch_results(monkeypatch, [_fail("git-global-config")])
    result = plan_mod.build(_profile(), cfg=_CFG)
    assert any("git-global-config" in a for a in result.advisories)


def test_git_action_uses_supplied_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """A supplied email/name produces a GitConfigAction carrying them."""
    _patch_results(monkeypatch, [_fail("git-global-config")])
    monkeypatch.setattr(GitConfigAction, "is_satisfied", lambda _self, _p: False)
    result = plan_mod.build(_profile(), cfg=_CFG, git_email="a@b.co", git_name="A B")
    git_actions = [a for a in result.actions if isinstance(a, GitConfigAction)]
    assert len(git_actions) == 1
    assert git_actions[0].email == "a@b.co"
    assert git_actions[0].name == "A B"


@dataclass
class _ResolvedCfg:
    """A dataclass stand-in for the resolved ``BuildConfig``.

    Must be a real dataclass (not ``SimpleNamespace``) so build()'s
    ``replace(cfg, host_mode=False)`` works as it does on the frozen
    ``BuildConfig`` resolve() actually returns.
    """

    kas_container_image: str
    host_mode: bool = True


def test_build_resolves_cfg_when_not_supplied(monkeypatch: pytest.MonkeyPatch) -> None:
    """When cfg is omitted, build() resolves it via config.resolve like doctor."""
    captured: dict[str, object] = {}

    def _fake_resolve(*, workspace, user_config, **_kw):
        captured["workspace"] = workspace
        captured["user_config"] = user_config
        return _ResolvedCfg(kas_container_image="resolved/image:1.0")

    monkeypatch.setattr(plan_mod.config, "resolve", _fake_resolve)
    _patch_results(monkeypatch, [_fail("container-image")])
    result = plan_mod.build(_profile())

    assert "workspace" in captured
    pulls = [a for a in result.actions if isinstance(a, DockerPullAction)]
    assert pulls and pulls[0].image == "resolved/image:1.0"


def test_build_forces_host_mode_off_so_docker_checks_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A self-resolved cfg has host_mode forced False; otherwise run_all filters
    out every docker check and no docker remediation could ever be produced."""
    seen: dict[str, object] = {}

    monkeypatch.setattr(
        plan_mod.config,
        "resolve",
        lambda *, workspace, user_config, **_kw: _ResolvedCfg(kas_container_image="img:1", host_mode=True),
    )

    def _capturing_run_all(cfg: object, _bsp: object) -> list[CheckResult]:
        seen["host_mode"] = cfg.host_mode
        return []

    monkeypatch.setattr(plan_mod, "run_all", _capturing_run_all)
    plan_mod.build(_profile())

    assert seen["host_mode"] is False


# ---------------------------------------------------------------------------
# host-preflight -> buildtools provisioning
#
# The remediation is gated on (1) host mode being the effective default, (2) the
# host-preflight check FAILing, and (3) the active workspace exposing
# openembedded-core/scripts/install-buildtools. The already-present case is
# dropped by the existing is_satisfied filter (detect_buildtools().present).
# ---------------------------------------------------------------------------


def _workspace_with_installer(tmp_path) -> object:
    """A tmp workspace dir carrying the oe-core install-buildtools script."""
    script = tmp_path / "openembedded-core" / "scripts" / "install-buildtools"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")
    return tmp_path


def _host_cfg(workspace) -> SimpleNamespace:
    """A host-mode cfg pointing at ``workspace`` (host mode is the effective default)."""
    return SimpleNamespace(kas_container_image="img:1", host_mode=True, workspace=workspace)


def test_host_preflight_failing_in_host_mode_adds_buildtools_actions(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Host mode default + host-preflight FAIL + installer present -> install action."""
    _patch_results(monkeypatch, [_fail("host-preflight")])
    # The toolchain is absent, so the install action is not dropped at stage two.
    monkeypatch.setattr(BuildtoolsInstallAction, "is_satisfied", lambda _self, _p: False)
    monkeypatch.setattr(BuildtoolsConfigPersistAction, "is_satisfied", lambda _self, _p: False)
    cfg = _host_cfg(_workspace_with_installer(tmp_path))
    result = plan_mod.build(_profile(), cfg=cfg)
    present = _types(result.actions)
    assert BuildtoolsInstallAction in present
    assert BuildtoolsConfigPersistAction in present
    installs = [a for a in result.actions if isinstance(a, BuildtoolsInstallAction)]
    assert installs[0].install_buildtools.endswith("openembedded-core/scripts/install-buildtools")


def test_host_preflight_in_container_mode_adds_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Host mode NOT the effective default -> no buildtools action even with the installer present."""
    _patch_results(monkeypatch, [_fail("host-preflight")])
    monkeypatch.setattr(BuildtoolsInstallAction, "is_satisfied", lambda _self, _p: False)
    monkeypatch.setattr(BuildtoolsConfigPersistAction, "is_satisfied", lambda _self, _p: False)
    cfg = SimpleNamespace(kas_container_image="img:1", host_mode=False, workspace=_workspace_with_installer(tmp_path))
    result = plan_mod.build(_profile(), cfg=cfg)
    present = _types(result.actions)
    assert BuildtoolsInstallAction not in present
    assert BuildtoolsConfigPersistAction not in present


def test_host_preflight_toolchain_already_present_is_dropped(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """An already-present toolchain (is_satisfied True) drops both buildtools actions."""
    _patch_results(monkeypatch, [_fail("host-preflight")])
    # detect_buildtools().present is True -> the satisfied-action filter drops them.
    monkeypatch.setattr(BuildtoolsInstallAction, "is_satisfied", lambda _self, _p: True)
    monkeypatch.setattr(BuildtoolsConfigPersistAction, "is_satisfied", lambda _self, _p: True)
    cfg = _host_cfg(_workspace_with_installer(tmp_path))
    result = plan_mod.build(_profile(), cfg=cfg)
    present = _types(result.actions)
    assert BuildtoolsInstallAction not in present
    assert BuildtoolsConfigPersistAction not in present


def test_host_preflight_without_installer_adds_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Host mode default but no install-buildtools script in the workspace -> nothing added."""
    _patch_results(monkeypatch, [_fail("host-preflight")])
    monkeypatch.setattr(BuildtoolsInstallAction, "is_satisfied", lambda _self, _p: False)
    # tmp_path has no openembedded-core/scripts/install-buildtools.
    cfg = _host_cfg(tmp_path)
    result = plan_mod.build(_profile(), cfg=cfg)
    present = _types(result.actions)
    assert BuildtoolsInstallAction not in present
    assert BuildtoolsConfigPersistAction not in present
