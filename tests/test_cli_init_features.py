"""Tests for the non-interactive ``bakar init`` feature step.

Everything that would touch the host is patched: the doctor runner, the probe
table, the host profile and plan builder, and the setup runner's sudo and
subprocess helpers. No test starts ssh, systemctl, sudo or psql.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
from typer.testing import CliRunner

from bakar import diagnostics, user_config
from bakar.commands import _init_features, _init_probes, _init_requirements
from bakar.commands import init as init_module  # noqa: F401 - registers the command
from bakar.commands._app import app
from bakar.setup import plan as setup_plan
from bakar.setup import runner as setup_runner
from bakar.setup.actions.base import RunCommand
from bakar.setup.plan import SetupPlan
from bakar.setup.profile import HostProfile

pytestmark = pytest.mark.unit

cli = CliRunner()


class _HostAction:
    """A stand-in host remediation carrying one privileged operation."""

    check_name = "sysctl"
    needs_root = True

    def describe(self) -> str:
        return "raise fs.inotify.max_user_watches"

    def is_satisfied(self, _profile: object) -> bool:
        return False

    def operations(self) -> list[RunCommand]:
        return [RunCommand(argv=["sysctl", "-w", "fs.inotify.max_user_watches=1"], needs_root=True)]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    state = SimpleNamespace(
        overrides={},
        probes={},
        host_actions=[],
        sudo_ok=True,
        privileged=[],
        unprivileged=[],
        apply_calls=[],
        units_enabled=False,
        ws=tmp_path / "ws",
    )

    def fake_run_all(cfg, bsp=None, *, only=None, deadline=None, **_kw):
        return [
            state.overrides.get(
                name,
                diagnostics.CheckResult(name, diagnostics.Severity.WARN, diagnostics.Status.PASS, f"{name} ok"),
            )
            for name in sorted(only or ())
        ]

    def fake_probe(_cfg, _values, _inputs):
        return True, "ok"

    monkeypatch.setattr(diagnostics, "run_all", fake_run_all)
    monkeypatch.setattr(
        _init_probes,
        "PROBES",
        MappingProxyType({name: state.probes.get(name, fake_probe) for name in _init_probes.PROBES}),
    )
    monkeypatch.setattr(_init_requirements.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(_init_features, "central_node_units_enabled", lambda: state.units_enabled)
    monkeypatch.setattr(HostProfile, "detect", classmethod(lambda cls: object()))
    monkeypatch.setattr(
        setup_plan, "build", lambda *_a, **_k: SetupPlan(actions=list(state.host_actions), advisories=[])
    )
    monkeypatch.setattr(setup_runner, "_has_passwordless_sudo", lambda: state.sudo_ok)
    monkeypatch.setattr(setup_runner, "_run_privileged_script", lambda ops: state.privileged.append(list(ops)))
    monkeypatch.setattr(setup_runner, "_run_unprivileged", state.unprivileged.append)
    real_apply = setup_runner.apply_plan

    def spy_apply(*args, **kwargs):
        state.apply_calls.append(kwargs)
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(setup_runner, "apply_plan", spy_apply)
    return state


def _user_config_path() -> Path:
    return Path.home() / ".config" / "bakar" / "config.toml"


def _init_ws(ws: Path) -> None:
    result = cli.invoke(app, ["init", "--family", "generic", "-w", str(ws)])
    assert result.exit_code == 0, result.output


def _run(*args: str):
    return cli.invoke(app, ["init", *args])


def _fail_check(env: SimpleNamespace, name: str, message: str = "broken") -> None:
    env.overrides[name] = diagnostics.CheckResult(
        name, diagnostics.Severity.WARN, diagnostics.Status.FAIL, message, "docs/features/x.md"
    )


def test_unreachable_mirror_exits_1_and_writes_no_config(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    _fail_check(env, "sstate-mirror-reachable", "http://127.0.0.1:1 is unreachable")
    result = _run(
        "-w", str(env.ws), "--feature", "sstate-mirror", "--set", "build.sstate_mirror_url=http://127.0.0.1:1"
    )
    assert result.exit_code == 1
    assert "unreachable" in result.output
    assert not _user_config_path().exists()
    assert env.apply_calls == []


def test_unknown_feature_lists_ids(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    result = _run("-w", str(env.ws), "--feature", "nonsense")
    assert result.exit_code == 1
    assert "nonsense" in result.output
    assert "ccache" in result.output
    assert "central-node" in result.output


def test_stray_set_key_exits_1(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    result = _run("-w", str(env.ws), "--feature", "ccache", "--set", "build.sstate_mirror_url=x")
    assert result.exit_code == 1
    assert "belongs to no selected feature" in result.output
    assert not _user_config_path().exists()


def test_disable_ccache_removes_ccache_keys(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    user_config.set_setting("build.ccache", "true")
    user_config.set_setting("build.ccache_dir", "/var/cache/cc")
    result = _run("-w", str(env.ws), "--disable", "ccache")
    assert result.exit_code == 0, result.output
    assert user_config.get_setting("build.ccache") is None
    assert user_config.get_setting("build.ccache_dir") is None


def test_feature_on_fresh_directory_without_family_exits_1(env: SimpleNamespace) -> None:
    result = _run("-w", str(env.ws), "--feature", "ccache")
    assert result.exit_code == 1
    assert "--family" in result.output
    assert not env.ws.exists()


def test_family_alone_on_initialized_workspace_keeps_old_error(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    result = _run("-w", str(env.ws), "--family", "generic")
    assert result.exit_code == 1
    assert "already initialized" in result.output


def test_family_alone_fresh_creates_no_user_config_and_never_applies(env: SimpleNamespace) -> None:
    result = _run("-w", str(env.ws), "--family", "generic")
    assert result.exit_code == 0, result.output
    assert (env.ws / ".bakar.toml").is_file()
    assert not _user_config_path().exists()
    assert env.apply_calls == []


def test_family_and_feature_scaffold_then_write_ccache(env: SimpleNamespace) -> None:
    result = _run("-w", str(env.ws), "--family", "generic", "--feature", "ccache")
    assert result.exit_code == 0, result.output
    assert (env.ws / ".bakar.toml").is_file()
    assert user_config.get_setting("build.ccache") is True


def test_privileged_plan_without_yes_exits_1_before_the_script_runner(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    env.host_actions.append(_HostAction())
    result = _run("-w", str(env.ws), "--feature", "ccache")
    assert result.exit_code == 1
    assert "--yes" in result.output
    assert "sysctl" in result.output
    assert env.privileged == []
    assert env.apply_calls == []
    assert not _user_config_path().exists()


def test_yes_with_failing_passwordless_sudo_exits_1(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    env.host_actions.append(_HostAction())
    env.sudo_ok = False
    result = _run("-w", str(env.ws), "--feature", "ccache", "--yes")
    assert result.exit_code == 1
    assert "sudo" in result.output
    assert env.privileged == []
    assert not _user_config_path().exists()


def test_yes_is_forwarded_only_when_given(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    assert _run("-w", str(env.ws), "--feature", "ccache").exit_code == 0
    assert env.apply_calls[-1]["assume_yes"] is False
    assert _run("-w", str(env.ws), "--feature", "hashserv", "--yes").exit_code == 0
    assert env.apply_calls[-1]["assume_yes"] is True


def test_cache_push_inactive_service_warns_with_guide_and_applies(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    ssh = Path.home() / ".ssh"
    ssh.mkdir(parents=True, exist_ok=True)
    (ssh / "bakar-cache-push").write_text("key")
    user_config.set_setting("build.dl_dir", "/srv/downloads")
    _fail_check(env, "cache-push-service", "avocado cache push unit is inactive")
    result = _run("-w", str(env.ws), "--feature", "cache-push", "--set", "build.cache_push_target=host:/srv/cache")
    assert result.exit_code == 0, result.output
    assert "inactive" in result.output
    assert "docs/features/cache-push.md" in result.output
    assert user_config.get_setting("build.cache_push_target") == "host:/srv/cache"


def test_unreachable_mirror_only_warns_when_sstate_mirror_is_already_enabled(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    user_config.set_setting("build.sstate_mirror_url", "http://127.0.0.1:1")
    _fail_check(env, "sstate-mirror-reachable", "http://127.0.0.1:1 is unreachable")
    result = _run("-w", str(env.ws), "--feature", "ccache")
    assert result.exit_code == 0, result.output
    assert "warning" in result.output
    assert "unreachable" in result.output
    assert user_config.get_setting("build.ccache") is True


def test_remote_dispatch_without_host_names_the_input(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    result = _run(
        "-w",
        str(env.ws),
        "--feature",
        "remote-dispatch",
        "--feature",
        "release-workspaces",
        "--set",
        "workspaces.template=/srv/{release}",
    )
    assert result.exit_code == 1
    assert "remote-dispatch.host" in result.output
    assert not _user_config_path().exists()


def test_central_node_plans_database_bootstrap_unit_enable_and_cluster_keys(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    result = _run(
        "-w",
        str(env.ws),
        "--feature",
        "central-node",
        "--set",
        "central-node.bind_host=10.0.0.1",
        "--yes",
    )
    assert result.exit_code == 0, result.output
    # The plan printed the feature-config action carrying all three keys.
    for key in ("build.bb_hashserve", "build.prserv_host", "build.cluster"):
        assert key in result.output
    assert len(env.unprivileged) == 2
    assert all("createdb" in " ".join(op.argv) for op in env.unprivileged)
    assert len(env.privileged) == 1
    assert env.privileged[0][0].argv[-1] == "avocado-prserv.service"
    assert user_config.get_setting("build.bb_hashserve") == "10.0.0.1:8686"
    assert user_config.get_setting("build.prserv_host") == "10.0.0.1:8585"
    assert user_config.get_setting("build.cluster") is True


def test_central_node_without_bind_host_exits_1(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    result = _run("-w", str(env.ws), "--feature", "central-node")
    assert result.exit_code == 1
    assert "bind_host" in result.output


def test_disable_reports_workspace_key_still_in_effect_and_leaves_file_alone(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    marker = env.ws / ".bakar.toml"
    marker.write_text(marker.read_text() + "\n[build]\nccache = true\n")
    user_config.set_setting("build.ccache", "true")
    before = hashlib.sha256(marker.read_bytes()).hexdigest()
    result = _run("-w", str(env.ws), "--disable", "ccache")
    assert result.exit_code == 0, result.output
    assert "still in effect" in result.output
    assert "build.ccache" in result.output
    assert user_config.get_setting("build.ccache") is None
    assert hashlib.sha256(marker.read_bytes()).hexdigest() == before


def test_mask_url_userinfo() -> None:
    assert _init_features.mask_url_userinfo("http://u:p@h/x") == "http://***@h/x"
    assert _init_features.mask_url_userinfo("no url here") == "no url here"
    assert _init_features.mask_url_userinfo("a postgres://r:r@db:5432/x b") == "a postgres://***@db:5432/x b"


def test_url_credentials_never_appear_in_output(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    result = _run("-w", str(env.ws), "--feature", "sstate-mirror", "--set", "build.sstate_mirror_url=http://u:p@h")
    assert result.exit_code == 0, result.output
    assert "u:p" not in result.output
    assert "http://***@h" in result.output
    assert user_config.get_setting("build.sstate_mirror_url") == "http://u:p@h"


@pytest.mark.unit
def test_set_without_a_feature_is_refused_not_dropped(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    result = _run("-w", str(env.ws), "--set", "build.ccache=false")
    assert result.exit_code == 1
    assert "--feature or --disable" in result.output
    assert not _user_config_path().exists()


@pytest.mark.unit
def test_set_on_an_already_enabled_feature_is_gated_like_an_added_one(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    user_config.set_setting("build.sstate_mirror_url", "http://good.example/sstate")
    _fail_check(env, "sstate-mirror-reachable", "http://127.0.0.1:1 is unreachable")
    result = _run(
        "-w", str(env.ws), "--feature", "sstate-mirror", "--set", "build.sstate_mirror_url=http://127.0.0.1:1"
    )
    assert result.exit_code == 1
    assert user_config.get_setting("build.sstate_mirror_url") == "http://good.example/sstate"
    assert env.apply_calls == []


@pytest.mark.unit
def test_plan_lines_print_literally_with_brackets(monkeypatch: pytest.MonkeyPatch) -> None:
    from bakar.commands._app import console
    from bakar.commands.setup import _print_plan

    class _Action:
        check_name = "x"
        needs_root = False

        def describe(self) -> str:
            return "bind [fd00::1]:8686 and [/] literally"

        def is_satisfied(self, _profile: object) -> bool:
            return False

        def operations(self) -> list[object]:
            return []

    with console.capture() as captured:
        _print_plan(SetupPlan(actions=[_Action()], advisories=[]))
    assert "[fd00::1]:8686" in captured.get()
    assert "[/] literally" in captured.get()
