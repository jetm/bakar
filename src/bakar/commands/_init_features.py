"""Feature step of ``bakar init``: the non-interactive run and shared helpers.

:func:`run_noninteractive` selects features from flags, verifies their
requirements, prints the plan and applies it. It writes no user-config setting
and runs no host change before the final apply step; the settings land through
one :class:`~bakar.setup.actions.feature_config.FeatureConfigAction` that the
runner applies after every host operation succeeded.

Helpers here (:func:`mask_url_userinfo`, :func:`derive_central_values`,
:func:`central_node_units_enabled`) are shared with the interactive step.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from typing import TYPE_CHECKING

import typer

from bakar import hashserv, prserv, user_config
from bakar.commands import _init_probes, _init_requirements
from bakar.commands._app import console
from bakar.setup import plan as setup_plan
from bakar.setup import runner
from bakar.setup.actions.base import RunCommand
from bakar.setup.actions.central_tier import CentralTierAction, CentralTierConfig
from bakar.setup.actions.feature_config import FeatureConfigAction
from bakar.setup.feature_selection import derive_enabled, plan_changes, validate_selection
from bakar.setup.features import CATALOG, FEATURE_IDS, Feature, feature_by_id
from bakar.setup.profile import HostProfile
from bakar.workspace_config import load_workspace_config

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from bakar.setup.actions.base import Action, WriteFile

_CENTRAL_NODE = "central-node"
_NODE_UNIT = "avocado-hashserv.service"
_UNIT_TIMEOUT = 10.0
_URL_USERINFO = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]*://)[^/\s]*@")


def mask_url_userinfo(value: str) -> str:
    """Replace the userinfo of every URL in ``value`` with ``***``.

    ``http://u:p@host/x`` becomes ``http://***@host/x``; a value with no URL
    is returned unchanged.
    """
    return _URL_USERINFO.sub(r"\1***@", value)


def _say(message: str, *, style: str | None = None) -> None:
    console.print(mask_url_userinfo(message), style=style, markup=False, highlight=False)


def _fail(message: str) -> typer.Exit:
    _say(message, style="red")
    return typer.Exit(1)


def derive_central_values(inputs: Mapping[str, str]) -> dict[str, str]:
    """The ``build.bb_hashserve`` and ``build.prserv_host`` values for a bind host.

    Uses the same helpers :class:`CentralTierAction` records, so the planned
    settings match what the action would write. Exits 1 naming ``bind_host``
    when it is absent. ``inputs`` is looked up by bare name or by
    ``central-node.bind_host``.
    """
    bind_host = inputs.get("bind_host") or inputs.get(f"{_CENTRAL_NODE}.bind_host")
    if not bind_host:
        raise _fail(f"{_CENTRAL_NODE}.bind_host is required: pass --set {_CENTRAL_NODE}.bind_host=<address>")
    return {
        "build.bb_hashserve": hashserv.central_bb_hashserve(bind_host, hashserv.CENTRAL_DEFAULT_PORT),
        "build.prserv_host": prserv.central_prserv_host(bind_host, prserv.CENTRAL_DEFAULT_PORT),
    }


def central_node_units_enabled() -> bool:
    """Whether ``systemctl is-enabled --quiet avocado-hashserv.service`` exits 0.

    A missing ``systemctl`` or a timeout reads as False. The child leads its own
    session so a timeout signals only its own process group.
    """
    try:
        proc = subprocess.Popen(
            ["systemctl", "is-enabled", "--quiet", _NODE_UNIT],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    try:
        return proc.wait(timeout=_UNIT_TIMEOUT) == 0
    except subprocess.TimeoutExpired:
        if os.getpgid(proc.pid) == proc.pid:
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return False


class _MaskedAction:
    """Wrap an action so its printed description never shows URL userinfo."""

    def __init__(self, inner: Action) -> None:
        self._inner = inner

    @property
    def check_name(self) -> str:
        return self._inner.check_name

    @property
    def needs_root(self) -> bool:
        return self._inner.needs_root

    def describe(self) -> str:
        return mask_url_userinfo(self._inner.describe())

    def is_satisfied(self, profile: HostProfile) -> bool:
        return self._inner.is_satisfied(profile)

    def operations(self) -> list[RunCommand | WriteFile]:
        return self._inner.operations()

    def apply(self) -> None:
        apply = getattr(self._inner, "apply", None)
        if callable(apply):
            apply()


def _all_catalog_keys() -> list[str]:
    keys: dict[str, None] = {}
    for feature in CATALOG:
        for key in (*feature.markers, *(s.key for s in feature.settings), *feature.fixed, *feature.off):
            keys[key] = None
    return list(keys)


def _read_raw_config() -> dict[str, object]:
    return {key: user_config.get_setting(key) for key in _all_catalog_keys()}


def _parse_sets(items: Sequence[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise _fail(f"invalid --set {item.split('=', 1)[0]!r}: expected KEY=VALUE")
        parsed[key] = value
    return parsed


def _split_sets(parsed: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Split ``--set`` pairs into (settings, inputs); inputs are ``<feature-id>.<name>``."""
    settings: dict[str, str] = {}
    inputs: dict[str, str] = {}
    for key, value in parsed.items():
        if key.split(".", 1)[0] in FEATURE_IDS:
            inputs[key] = value
        else:
            settings[key] = value
    return settings, inputs


def _check_stray_keys(selected: set[str], settings: Mapping[str, str], inputs: Mapping[str, str]) -> None:
    chosen = [f for f in CATALOG if f.id in selected]
    allowed_settings = {s.key for f in chosen for s in f.settings}
    allowed_inputs = {f"{f.id}.{i.key}" for f in chosen for i in f.inputs}
    for key in settings:
        if key not in allowed_settings:
            raise _fail(f"--set {key}: the key belongs to no selected feature")
    for key in inputs:
        if key not in allowed_inputs:
            raise _fail(f"--set {key}: the input belongs to no selected feature")


def _has_set_values(feature: Feature, settings: Mapping[str, str], inputs: Mapping[str, str]) -> bool:
    """Whether ``--set`` named a setting or input of ``feature``."""
    return any(s.key in settings for s in feature.settings) or any(
        f"{feature.id}.{i.key}" in inputs for i in feature.inputs
    )


def _collect_values(
    configure: Sequence[str], settings: Mapping[str, str], inputs: Mapping[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    """Fill defaults and require every required setting or input of ``configure`` features."""
    values = dict(settings)
    all_inputs = dict(inputs)
    for feature_id in configure:
        feature = feature_by_id(feature_id)
        for setting in feature.settings:
            if values.get(setting.key) or not setting.required or not setting.prompt:
                continue
            if setting.default is None:
                raise _fail(f"{feature.id}: {setting.key} is required: pass --set {setting.key}=<value>")
            values[setting.key] = setting.default
        for spec in feature.inputs:
            key = f"{feature.id}.{spec.key}"
            if all_inputs.get(key):
                continue
            if spec.default is not None:
                all_inputs[key] = spec.default
            elif spec.required:
                raise _fail(f"{feature.id}: input {key} is required: pass --set {key}=<value>")
    return values, all_inputs


def _bare_inputs(all_inputs: Mapping[str, str]) -> dict[str, str]:
    """Inputs under both ``<feature-id>.<name>`` and the bare name probes read."""
    merged = dict(all_inputs)
    for key, value in all_inputs.items():
        merged.setdefault(key.split(".", 1)[1], value)
    return merged


def _report_requirements(
    selected: Sequence[Feature],
    configure: set[str],
    evaluations: Sequence[_init_requirements.Evaluation],
) -> bool:
    """Print unmet requirements; return True when a feature in ``configure`` is blocked."""
    blocked = False
    for feature in selected:
        for ev in evaluations:
            for result in ev.hard_unmet(feature.id):
                if feature.id in configure:
                    blocked = True
                    _say(f"{feature.id}: {result.reason}", style="red")
                    _say(f"  fix: {result.requirement.fix}")
                    _say(f"  guide: {feature.guide}")
                else:
                    _say(
                        f"warning: {feature.id} is already enabled but a requirement is unmet: "
                        f"{result.reason} (guide: {feature.guide})",
                        style="yellow",
                    )
            for result in ev.soft_unmet(feature.id):
                _say(f"warning: {feature.id}: {result.reason} (guide: {feature.guide})", style="yellow")
    return blocked


def _note_still_in_effect(removed: Sequence[str], workspace: Path) -> None:
    try:
        ws_cfg = load_workspace_config(workspace)
    except ValueError:
        return
    for feature_id in removed:
        for key in feature_by_id(feature_id).off:
            field_name = _init_requirements.build_config_field(key)
            env_name = "BAKAR_" + key.rsplit(".", 1)[-1].upper()
            if field_name is not None and getattr(ws_cfg, field_name, None) is not None:
                _say(
                    f"note: {key} is still in effect: it is set in {workspace / '.bakar.toml'}, which init leaves alone"
                )
            elif env_name in os.environ:
                _say(f"note: {key} is still in effect: {env_name} is set in the environment")


def _central_inputs(all_inputs: Mapping[str, str]) -> tuple[str, str, int]:
    bind_host = all_inputs[f"{_CENTRAL_NODE}.bind_host"]
    pg_host = all_inputs.get(f"{_CENTRAL_NODE}.pg_host") or "localhost"
    raw_port = all_inputs.get(f"{_CENTRAL_NODE}.pg_port") or "5432"
    try:
        return bind_host, pg_host, int(raw_port)
    except ValueError:
        raise _fail(f"{_CENTRAL_NODE}.pg_port {raw_port!r} is not an integer") from None


def run_noninteractive(
    *,
    workspace: Path,
    features: Sequence[str],
    disables: Sequence[str],
    set_items: Sequence[str],
    yes: bool,
    scaffold: Callable[[], None] | None = None,
) -> None:
    """Select, verify, plan and apply features from flags (spec steps 1 and 3-8).

    ``scaffold`` creates the workspace when it does not exist yet; it runs after
    the selection and its values are valid and before any requirement check.
    """
    from bakar.commands import _app as _state
    from bakar.commands.setup import _print_plan

    # 1. parse --set
    settings, inputs = _split_sets(_parse_sets(set_items))

    # 3. current state
    try:
        raw = _read_raw_config()
    except ValueError as exc:
        raise _fail(f"cannot read the user config: {exc}") from exc
    enabled = derive_enabled(raw, central_node_units_enabled=central_node_units_enabled())

    # 4. selection
    unknown_disables = sorted(set(disables) - set(FEATURE_IDS))
    if unknown_disables:
        raise _fail(f"unknown feature {unknown_disables[0]!r}; valid features: {', '.join(FEATURE_IDS)}")
    selected = (set(enabled) | set(features)) - set(disables)
    errors, sel_warnings = validate_selection(selected, enabled)
    for message in sel_warnings:
        _say(f"warning: {message}", style="yellow")
    if errors:
        for message in errors:
            _say(message, style="red")
        raise typer.Exit(1)
    _check_stray_keys(selected, settings, inputs)

    # 5. values
    configure = plan_changes(selected, enabled, {}).configure
    values, all_inputs = _collect_values(configure, settings, inputs)
    if _CENTRAL_NODE in configure:
        values.update(derive_central_values(all_inputs))
    changes = plan_changes(selected, enabled, values)

    if scaffold is not None:
        scaffold()

    # 6. requirements
    try:
        cfg, override_warnings = _init_requirements.proposed_config(workspace, changes.sets, changes.unsets)
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    chosen = [f for f in CATALOG if f.id in selected]
    probe_inputs = _bare_inputs(all_inputs)
    evaluations = []
    for phase in ("pre", "values"):
        ev = _init_requirements.evaluate(
            chosen,
            cfg,
            values=values,
            inputs=probe_inputs,
            phase=phase,
            probes=_init_probes.PROBES,
        )
        evaluations.append(ev)
        _say(_init_requirements.checks_line(ev))
    for message in override_warnings:
        _say(f"warning: {message}", style="yellow")
    # A feature the user passed --set values for is being configured even when
    # it was already enabled: gate it like an added one, so an unreachable
    # mirror URL is refused rather than written.
    configured = set(configure) | {f.id for f in chosen if _has_set_values(f, settings, inputs)}
    if _report_requirements(chosen, configured, evaluations):
        raise typer.Exit(1)

    # 7. plan
    extra: list[Action] = []
    if changes.sets or changes.unsets:
        extra.append(_MaskedAction(FeatureConfigAction(changes.sets, changes.unsets)))
    if _CENTRAL_NODE in changes.added:
        bind_host, pg_host, pg_port = _central_inputs(all_inputs)
        extra.append(
            _MaskedAction(CentralTierAction(CentralTierConfig.from_inputs(bind_host, pg_host, pg_port), persist=False))
        )
    _say("checking host")
    plan = setup_plan.build_init_plan(
        HostProfile.detect(),
        workspace=workspace,
        extra_actions=extra,
        user_config=_state._USER_CONFIG,
    )
    script = _print_plan(plan)
    if script:
        _say("Privileged script")
        _say(script)
    _note_still_in_effect(changes.removed, workspace)

    # 8. apply
    privileged = [op for action in plan.actions for op in action.operations() if op.needs_root]
    if privileged and not yes:
        steps = "; ".join(_describe_op(op) for op in privileged)
        raise _fail(f"the plan needs root for: {steps}. Re-run with --yes (needs passwordless sudo) to apply it.")
    runner.apply_plan(plan, assume_yes=yes)


def _describe_op(op: RunCommand | WriteFile) -> str:
    if isinstance(op, RunCommand):
        return " ".join(op.argv)
    return f"write {op.path}"
