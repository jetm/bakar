"""Tests for the interactive ``bakar init`` feature checklist.

questionary is faked (as in tests/test_cli_presets.py) and everything that
would touch the host is patched: the doctor runner, the probe table, the host
profile and plan builder, and the setup runner's sudo and subprocess helpers.
No test starts ssh, systemctl, sudo or psql.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
import questionary
import typer
from typer.testing import CliRunner

from bakar import diagnostics, user_config
from bakar.commands import _init_checklist, _init_features, _init_probes, _init_requirements
from bakar.commands import init as init_module
from bakar.commands._app import app
from bakar.setup import plan as setup_plan
from bakar.setup import runner as setup_runner
from bakar.setup.plan import SetupPlan
from bakar.setup.profile import HostProfile

pytestmark = pytest.mark.unit

cli = CliRunner()

APPLY = "Apply this plan?"


class _Q:
    def __init__(self, answer: object) -> None:
        self._answer = answer

    def ask(self) -> object:
        return self._answer


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    state = SimpleNamespace(
        overrides={},
        extra_checked=[],
        checkbox_answer=None,
        texts={},
        confirms={APPLY: True},
        selects=[],
        prompts=[],
        choices=[],
        which_missing=set(),
        units_enabled=False,
        privileged=[],
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

    def fake_checkbox(message, choices):
        state.prompts.append(message)
        state.choices = [c for c in choices if isinstance(c, questionary.Choice)]
        if state.checkbox_answer is not None:
            return list(state.checkbox_answer)
        picked = [c.value for c in state.choices if c.checked and not c.disabled]
        return picked + list(state.extra_checked)

    def fake_text(message, default=""):
        state.prompts.append(message)
        return _Q(state.texts.get(message, default))

    def fake_confirm(message, default=True):
        state.prompts.append(message)
        return _Q(state.confirms.get(message, default))

    def fake_select(message, choices):
        state.prompts.append(message)
        if message == "BSP family:":
            return _Q("generic")
        return _Q(state.selects.pop(0))

    def fake_path(message, default=""):
        state.prompts.append(message)
        return _Q(default)

    monkeypatch.setattr(questionary, "checkbox", lambda m, choices: _Q(fake_checkbox(m, choices)))
    monkeypatch.setattr(questionary, "text", fake_text)
    monkeypatch.setattr(questionary, "confirm", fake_confirm)
    monkeypatch.setattr(questionary, "select", fake_select)
    monkeypatch.setattr(questionary, "path", fake_path)
    monkeypatch.setattr(init_module, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True)))

    monkeypatch.setattr(diagnostics, "run_all", fake_run_all)
    monkeypatch.setattr(
        _init_probes,
        "PROBES",
        MappingProxyType({name: (lambda _c, _v, _i: (True, "ok")) for name in _init_probes.PROBES}),
    )
    monkeypatch.setattr(
        _init_requirements.shutil,
        "which",
        lambda name: None if name in state.which_missing else f"/usr/bin/{name}",
    )
    monkeypatch.setattr(_init_checklist, "central_node_units_enabled", lambda: state.units_enabled)
    monkeypatch.setattr(_init_features, "central_node_units_enabled", lambda: state.units_enabled)
    monkeypatch.setattr(HostProfile, "detect", classmethod(lambda cls: object()))
    monkeypatch.setattr(setup_plan, "build", lambda *_a, **_k: SetupPlan(actions=[], advisories=[]))
    monkeypatch.setattr(setup_runner, "_has_passwordless_sudo", lambda: True)
    monkeypatch.setattr(setup_runner, "_run_privileged_script", lambda ops: state.privileged.append(list(ops)))
    monkeypatch.setattr(setup_runner, "_run_unprivileged", lambda _op: None)
    state.extra_checked = []
    return state


def _user_config_path() -> Path:
    return Path.home() / ".config" / "bakar" / "config.toml"


def _init_ws(ws: Path) -> None:
    result = cli.invoke(app, ["init", "--family", "generic", "-w", str(ws)])
    assert result.exit_code == 0, result.output


def _run(ws: Path):
    return cli.invoke(app, ["init", "-w", str(ws)])


def _fail_check(env: SimpleNamespace, name: str, message: str) -> None:
    env.overrides[name] = diagnostics.CheckResult(
        name, diagnostics.Severity.WARN, diagnostics.Status.FAIL, message, "docs/features/x.md"
    )


def _choice(env: SimpleNamespace, feature_id: str) -> questionary.Choice:
    return next(c for c in env.choices if c.value == feature_id)


def test_initialized_workspace_shows_checklist_without_scaffold(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    result = _run(env.ws)
    assert result.exit_code == 0, result.output
    assert "Select features:" in env.prompts
    assert "BSP family:" not in env.prompts
    assert "already initialized:" not in result.output
    assert "checks:" in result.output


def test_fresh_workspace_scaffolds_then_shows_checklist(env: SimpleNamespace) -> None:
    result = _run(env.ws)
    assert result.exit_code == 0, result.output
    assert (env.ws / ".bakar.toml").is_file()
    assert env.prompts.index("BSP family:") < env.prompts.index("Select features:")


def test_hard_unmet_feature_is_a_disabled_choice(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    env.which_missing.add("ccache")
    result = _run(env.ws)
    assert result.exit_code == 0, result.output
    ccache = _choice(env, "ccache")
    assert ccache.disabled
    assert "ccache not found on PATH" in ccache.disabled
    assert "fix:" in result.output
    assert "guide:" in result.output


def test_enabled_feature_with_failing_requirement_is_kept(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    user_config.set_setting("build.ccache", "true")
    env.which_missing.add("ccache")
    result = _run(env.ws)
    assert result.exit_code == 0, result.output
    assert _choice(env, "ccache").disabled
    assert "unset build.ccache" not in result.output
    assert "--disable" not in result.output
    assert user_config.get_setting("build.ccache") is True


def test_ccache_from_config_is_checked(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    user_config.set_setting("build.ccache", "true")
    result = _run(env.ws)
    assert result.exit_code == 0, result.output
    assert _choice(env, "ccache").checked


def test_cache_push_soft_warning_before_plan(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    key = Path.home() / ".ssh" / "bakar-cache-push"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text("k")
    user_config.set_setting("build.dl_dir", str(Path.home() / "dl"))
    _fail_check(env, "cache-push-service", "the cache-push unit is inactive")
    env.extra_checked = ["cache-push"]
    env.texts["Cache push target (user@host:/path)"] = "u@h:/srv"
    result = _run(env.ws)
    assert result.exit_code == 0, result.output
    warning = result.output.index("the cache-push unit is inactive")
    assert warning < result.output.index("Planned actions")


def test_remote_dispatch_prompts_host_and_skips_blank_optional(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    env.extra_checked = ["remote-dispatch", "release-workspaces"]
    env.texts["Remote host"] = "builder"
    env.texts["Release workspace path template"] = "/ws/{release}"
    result = _run(env.ws)
    assert result.exit_code == 0, result.output
    assert "Remote host" in env.prompts
    assert "dispatch.snapshot_untracked_cap_mb" not in result.output
    assert user_config.get_setting("dispatch.snapshot_untracked_cap_mb") is None
    assert user_config.get_setting("workspaces.template") == "/ws/{release}"
    assert "--feature remote-dispatch" in result.output
    assert "--set remote-dispatch.host=builder" in result.output


def test_central_node_plans_all_three_settings(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    env.extra_checked = ["central-node"]
    env.texts["Address the central services bind to"] = "10.0.0.1"
    env.confirms[APPLY] = False
    result = _run(env.ws)
    assert result.exit_code == 0, result.output
    for key in ("build.bb_hashserve", "build.prserv_host", "build.cluster"):
        assert key in result.output
    assert "10.0.0.1" in result.output


def test_declining_writes_nothing(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    env.extra_checked = ["ccache"]
    env.confirms[APPLY] = False
    result = _run(env.ws)
    assert result.exit_code == 0, result.output
    assert "Nothing was changed" in result.output
    assert not _user_config_path().exists()


def test_single_confirmation_only(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("typer.confirm called")

    monkeypatch.setattr(typer, "confirm", boom)
    _init_ws(env.ws)
    env.extra_checked = ["ccache"]
    result = _run(env.ws)
    assert result.exit_code == 0, result.output
    assert env.prompts.count(APPLY) == 1
    assert user_config.get_setting("build.ccache") is True


def test_unreachable_mirror_offers_reenter_or_drop(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    _fail_check(env, "sstate-mirror-reachable", "http://127.0.0.1:1 is unreachable")
    env.extra_checked = ["sstate-mirror"]
    env.texts["sstate mirror URL"] = "http://127.0.0.1:1"
    env.selects = [_init_checklist.REENTER, _init_checklist.DROP]
    result = _run(env.ws)
    assert result.exit_code == 0, result.output
    assert result.output.count("http://127.0.0.1:1 is unreachable") >= 2
    assert env.prompts.count("sstate mirror URL") == 2
    assert env.selects == []
    assert user_config.get_setting("build.sstate_mirror_url") is None


def test_replay_line_lists_features_and_masked_set_values(env: SimpleNamespace) -> None:
    _init_ws(env.ws)
    env.extra_checked = ["ccache", "sstate-mirror"]
    env.texts["sstate mirror URL"] = "http://user:secret@mirror.example/sstate"
    result = _run(env.ws)
    assert result.exit_code == 0, result.output
    replay = next(line for line in result.output.splitlines() if line.startswith("bakar init -w"))
    assert "--feature ccache" in replay
    assert "--feature sstate-mirror" in replay
    assert "--set build.ccache=true" in replay
    assert "build.sstate_mirror_url=" in replay
    assert "***@mirror.example" in replay
    assert "secret" not in result.output


def test_abort_at_checklist_leaves_scaffold_only(env: SimpleNamespace) -> None:
    env.checkbox_answer = None
    result = _run_aborting(env)
    assert result.exit_code == 1
    assert "Aborted." in result.output
    assert (env.ws / ".bakar.toml").is_file()
    assert not _user_config_path().exists()


def _run_aborting(env: SimpleNamespace):
    original = questionary.checkbox
    questionary.checkbox = lambda _m, choices: _Q(None)
    try:
        return _run(env.ws)
    finally:
        questionary.checkbox = original
