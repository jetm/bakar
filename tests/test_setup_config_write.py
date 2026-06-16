"""Tests for the global-config persist action of ``bakar setup``.

Covers :class:`ConfigWriteAction`: it persists the four applied host knobs to
the GLOBAL config via ``set_setting`` (never a workspace ``.bakar.toml``), using
the section-relative dotted keys (``host.inotify_instances``, not the field-name
``host.host_inotify_instances``) and string-coerced values, and never persists
the never-applied ``host.mem_min_gb`` advisory.
"""

from __future__ import annotations

import tomllib

import pytest

from bakar.setup.actions.base import Action
from bakar.setup.actions.config_write import APPLIED_HOST_SETTINGS, ConfigWriteAction
from bakar.user_config import set_setting


def _profile():
    """A minimal stand-in profile; this action ignores every field."""
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


def test_config_write_is_an_action_with_synthetic_check_name() -> None:
    action = ConfigWriteAction()
    assert isinstance(action, Action)
    assert action.check_name == "host-config-persist"
    assert action.needs_root is False


def test_apply_writes_the_four_knobs_to_the_passed_global_config(tmp_path) -> None:
    """apply() persists each knob into the [host] table of the given config path."""
    config_path = tmp_path / "config.toml"
    ConfigWriteAction().apply(config_path)

    with config_path.open("rb") as f:
        data = tomllib.load(f)

    host = data["host"]
    # The leaf key in the file is the section-relative form (no host_ prefix),
    # because [host] is already the section - the same form as the dotted key.
    assert host["inotify_instances"] == 8192
    assert host["inotify_watches"] == 1048576
    assert host["swappiness_max"] == 10
    assert host["nofile_soft"] == 65536


def test_apply_does_not_persist_the_never_applied_mem_advisory(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    ConfigWriteAction().apply(config_path)

    with config_path.open("rb") as f:
        data = tomllib.load(f)

    assert "mem_min_gb" not in data.get("host", {})


def test_apply_calls_set_setting_with_stripped_dotted_keys_and_str_values(monkeypatch) -> None:
    """The dotted keys are the prefix-stripped form and values are strings."""
    calls: list[tuple[str, str]] = []

    def _spy(key, raw_value, path=None):
        assert isinstance(raw_value, str)
        calls.append((key, raw_value))

    monkeypatch.setattr("bakar.setup.actions.config_write.set_setting", _spy)
    ConfigWriteAction().apply()

    keys = [key for key, _ in calls]
    assert keys == [
        "host.inotify_instances",
        "host.inotify_watches",
        "host.swappiness_max",
        "host.nofile_soft",
    ]
    # The field-name spelling (doubled host_ prefix) is never used.
    for key in keys:
        assert not key.startswith("host.host_")
    assert all(isinstance(value, str) for _, value in calls)


def test_apply_passes_through_the_global_config_path(monkeypatch, tmp_path) -> None:
    """The path given to apply() is forwarded verbatim to set_setting."""
    seen_paths = []
    monkeypatch.setattr(
        "bakar.setup.actions.config_write.set_setting",
        lambda _key, _value, path=None: seen_paths.append(path),
    )
    config_path = tmp_path / "config.toml"
    ConfigWriteAction().apply(config_path)

    assert seen_paths == [config_path] * len(APPLIED_HOST_SETTINGS)


def test_apply_never_writes_a_workspace_bakar_toml(tmp_path, monkeypatch) -> None:
    """Persist targets only the passed config; no .bakar.toml is created."""
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.toml"
    ConfigWriteAction().apply(config_path)

    assert config_path.exists()
    assert not (tmp_path / ".bakar.toml").exists()
    assert list(tmp_path.rglob(".bakar.toml")) == []


def test_doubled_prefix_field_name_key_is_rejected_by_set_setting(tmp_path) -> None:
    """The field-name spelling host.host_inotify_instances raises ValueError.

    Confirms why the action uses the section-relative dotted keys: the doubled
    prefix is not a recognized setting.
    """
    with pytest.raises(ValueError):
        set_setting("host.host_inotify_instances", "8192", tmp_path / "config.toml")


def test_is_satisfied_true_when_all_knobs_already_persisted(tmp_path, monkeypatch) -> None:
    """is_satisfied checks the global config; all-set means a no-op persist."""
    config_path = tmp_path / "config.toml"
    ConfigWriteAction().apply(config_path)

    monkeypatch.setattr(
        "bakar.setup.actions.config_write.get_setting",
        lambda key, path=None: APPLIED_HOST_SETTINGS[key],
    )
    assert ConfigWriteAction().is_satisfied(_profile()) is True


def test_is_satisfied_false_when_a_knob_is_missing(monkeypatch) -> None:
    monkeypatch.setattr("bakar.setup.actions.config_write.get_setting", lambda _key, path=None: None)
    assert ConfigWriteAction().is_satisfied(_profile()) is False


def test_operations_is_empty_persist_happens_in_apply() -> None:
    """No shell primitives: the persist runs via apply(), not operations()."""
    assert ConfigWriteAction().operations() == []
