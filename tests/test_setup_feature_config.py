"""Tests for :class:`FeatureConfigAction`, the atomic feature-config persist step."""

from __future__ import annotations

import subprocess
import tomllib

import pytest

from bakar.setup import runner
from bakar.setup.actions import feature_config
from bakar.setup.actions.base import Action, RunCommand
from bakar.setup.actions.feature_config import FeatureConfigAction
from bakar.setup.plan import SetupPlan
from tests.conftest import make_host_profile

pytestmark = pytest.mark.unit

SETS = {"host.swappiness_max": "10", "host.inotify_instances": "8192"}


def test_conforms_to_action_protocol() -> None:
    action = FeatureConfigAction(SETS, [])
    assert isinstance(action, Action)
    assert action.check_name == "feature-config"
    assert action.needs_root is False
    assert action.operations() == []


def test_is_satisfied_only_when_empty() -> None:
    profile = make_host_profile()
    assert FeatureConfigAction({}, []).is_satisfied(profile) is True
    assert FeatureConfigAction(SETS, []).is_satisfied(profile) is False
    assert FeatureConfigAction({}, ["host.nofile_soft"]).is_satisfied(profile) is False


def test_describe_names_every_key_in_sorted_order() -> None:
    text = FeatureConfigAction(SETS, ["host.nofile_soft", "host.inotify_watches"]).describe()
    assert "\n" not in text
    for key in (*SETS, "host.nofile_soft", "host.inotify_watches"):
        assert key in text
    assert "set host.inotify_instances = 8192" in text
    assert text.index("host.inotify_instances") < text.index("host.swappiness_max")
    assert text.index("unset host.inotify_watches") < text.index("unset host.nofile_soft")


def test_apply_makes_one_apply_settings_call(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    calls: list[tuple] = []
    real = feature_config.apply_settings

    def _spy(sets, unsets, path):
        calls.append((dict(sets), list(unsets), path))
        real(sets, unsets, path)

    monkeypatch.setattr(feature_config, "apply_settings", _spy)
    FeatureConfigAction(SETS, [], config_path).apply()

    assert calls == [(SETS, [], config_path)]
    with config_path.open("rb") as f:
        assert tomllib.load(f)["host"]["swappiness_max"] == 10


class _PrivilegedAction:
    check_name = "fake-privileged"
    needs_root = True

    def describe(self) -> str:
        return "fake"

    def is_satisfied(self, _profile) -> bool:
        return False

    def operations(self):
        return [RunCommand(argv=["true"], needs_root=True)]


def test_settings_not_written_when_privileged_script_fails(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"

    def _boom(_ops) -> None:
        raise subprocess.CalledProcessError(1, ["sudo", "bash", "-s"])

    monkeypatch.setattr(runner, "_run_privileged_script", _boom)
    plan = SetupPlan(actions=[_PrivilegedAction(), FeatureConfigAction(SETS, [], config_path)])

    with pytest.raises(subprocess.CalledProcessError):
        runner.apply_plan(plan, confirmed=True)

    assert not config_path.exists()
