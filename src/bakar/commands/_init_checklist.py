"""Interactive feature step of ``bakar init``.

:func:`run_feature_step` shows the feature checklist, collects values for the
features being configured, verifies their requirements, prints the plan and
asks ONE confirmation before applying it. Nothing is written to the user
config before that confirmation: the settings land through one
:class:`~bakar.setup.actions.feature_config.FeatureConfigAction` that the runner
applies after every host operation succeeded.

An already-enabled feature that is shown disabled (its requirement now fails)
stays selected and is never planned for removal here: a disabled checkbox
choice cannot be toggled, and removing a feature needs ``--disable``.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

import questionary
import typer

from bakar.commands import _init_probes, _init_requirements
from bakar.commands._app import console
from bakar.commands._init_features import (
    _CENTRAL_NODE,
    _bare_inputs,
    _central_inputs,
    _MaskedAction,
    _note_still_in_effect,
    _read_raw_config,
    _say,
    central_node_units_enabled,
    derive_central_values,
    mask_url_userinfo,
)
from bakar.commands._prompts import _ask
from bakar.setup import plan as setup_plan
from bakar.setup import runner
from bakar.setup.actions.central_tier import CentralTierAction, CentralTierConfig
from bakar.setup.actions.feature_config import FeatureConfigAction
from bakar.setup.feature_selection import derive_enabled, plan_changes, validate_selection
from bakar.setup.features import CATALOG, Feature, FeatureSetting, feature_by_id
from bakar.setup.profile import HostProfile

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from bakar.setup.actions.base import Action

REENTER = "Re-enter values"
DROP = "Drop this feature"


def _truthy(value: object) -> bool:
    return str(value).strip().lower() not in ("", "false", "0", "none")


def _prompt_one(spec: FeatureSetting, preset: object) -> str:
    """Prompt one setting or input; ``preset`` (config or earlier entry) beats the default."""
    default = preset if preset is not None else spec.default
    if spec.kind == "bool":
        answer = _ask(questionary.confirm(spec.prompt, default=_truthy(default) if default is not None else False))
        return "true" if answer else "false"
    while True:
        text = str(_ask(questionary.text(spec.prompt, default="" if default is None else str(default)))).strip()
        if text or not spec.required:
            return text
        _say(f"{spec.key} is required", style="red")


def _prompt_feature(
    feature: Feature,
    values: dict[str, str],
    inputs: dict[str, str],
    raw: Mapping[str, object],
) -> None:
    """Prompt every setting and input of ``feature`` that has a prompt."""
    for setting in feature.settings:
        if not setting.prompt:
            continue
        preset = values.get(setting.key)
        if preset is None:
            current = raw.get(setting.key)
            preset = None if current is None else str(current)
        values[setting.key] = _prompt_one(setting, preset)
    for spec in feature.inputs:
        if not spec.prompt:
            continue
        key = f"{feature.id}.{spec.key}"
        inputs[key] = _prompt_one(spec, inputs.get(key))
    if feature.id == _CENTRAL_NODE:
        values.update(derive_central_values(inputs))


def _drop_values(feature: Feature, values: dict[str, str], inputs: dict[str, str]) -> None:
    for setting in feature.settings:
        values.pop(setting.key, None)
    for spec in feature.inputs:
        inputs.pop(f"{feature.id}.{spec.key}", None)


def _disabled_reasons(ev: _init_requirements.Evaluation) -> dict[str, list[_init_requirements.RequirementResult]]:
    out: dict[str, list[_init_requirements.RequirementResult]] = {}
    for feature in CATALOG:
        unmet = ev.hard_unmet(feature.id)
        if unmet:
            out[feature.id] = unmet
    return out


def _choices(
    enabled: frozenset[str], disabled: Mapping[str, list[_init_requirements.RequirementResult]]
) -> list[questionary.Choice | questionary.Separator]:
    choices: list[questionary.Choice | questionary.Separator] = []
    tier = 0
    for feature in CATALOG:
        if feature.tier != tier:
            tier = feature.tier
            choices.append(questionary.Separator(f"Tier {tier}"))
        reason = "; ".join(r.reason for r in disabled.get(feature.id, ())) or None
        choices.append(
            questionary.Choice(
                title=f"{feature.title} ({feature.id})",
                value=feature.id,
                checked=feature.id in enabled,
                disabled=reason,
            )
        )
    return choices


def _select_features(
    enabled: frozenset[str],
    disabled: Mapping[str, list[_init_requirements.RequirementResult]],
) -> set[str]:
    """Ask the checklist until the selection validates; return it with enabled-but-disabled features kept."""
    choices = _choices(enabled, disabled)
    pinned = set(enabled) & set(disabled)
    while True:
        answer = _ask(questionary.checkbox("Select features:", choices=choices))
        selected = set(answer) | pinned
        errors, warnings = validate_selection(selected, enabled)
        for message in warnings:
            _say(f"warning: {message}", style="yellow")
        if not errors:
            return selected
        for message in errors:
            _say(message, style="red")


def _print_blocked(feature: Feature, result: _init_requirements.RequirementResult) -> None:
    _say(f"{feature.id}: {result.reason}", style="red")
    _say(f"  fix: {result.requirement.fix}")
    _say(f"  guide: {feature.guide}")


def _replay_line(
    workspace: Path,
    configure: tuple[str, ...],
    removed: tuple[str, ...],
    values: Mapping[str, str],
    inputs: Mapping[str, str],
) -> str:
    parts = ["bakar", "init", "-w", str(workspace)]
    for feature_id in configure:
        parts += ["--feature", feature_id]
    for feature_id in removed:
        parts += ["--disable", feature_id]
    entered: list[str] = []
    for feature_id in configure:
        feature = feature_by_id(feature_id)
        entered.extend(
            f"{setting.key}={values[setting.key]}"
            for setting in feature.settings
            if setting.prompt and values.get(setting.key)
        )
        for spec in feature.inputs:
            key = f"{feature_id}.{spec.key}"
            if spec.prompt and inputs.get(key):
                entered.append(f"{key}={inputs[key]}")
    for item in entered:
        parts += ["--set", item]
    return " ".join(shlex.quote(mask_url_userinfo(part)) for part in parts)


def run_feature_step(workspace: Path) -> None:
    """Run the interactive feature step for an initialized ``workspace``."""
    from bakar.commands import _app as _state
    from bakar.commands.setup import _print_plan

    try:
        raw = _read_raw_config()
    except ValueError as exc:
        _say(f"cannot read the user config: {exc}", style="red")
        raise typer.Exit(1) from exc

    # 1. current state and the "pre" evaluation of every feature
    enabled = derive_enabled(raw, central_node_units_enabled=central_node_units_enabled())
    try:
        cfg0, _ = _init_requirements.proposed_config(workspace, {}, ())
    except ValueError as exc:
        _say(str(exc), style="red")
        raise typer.Exit(1) from exc
    pre = _init_requirements.evaluate(CATALOG, cfg0, values={}, inputs={}, phase="pre", probes=_init_probes.PROBES)
    _say(_init_requirements.checks_line(pre))
    disabled = _disabled_reasons(pre)
    for feature_id, reason in pre.unavailable.items():
        if feature_id not in disabled:
            _say(f"{feature_id}: {reason}", style="yellow")

    # 2-4. checklist
    selected = _select_features(enabled, disabled)

    # 3. disabled features' fix and guide, pre-phase soft warnings once
    for feature in CATALOG:
        for result in disabled.get(feature.id, ()):
            _print_blocked(feature, result)
        if feature.id in selected:
            for result in pre.soft_unmet(feature.id):
                _say(f"warning: {feature.id}: {result.reason} (guide: {feature.guide})", style="yellow")

    # 5. values for the features being configured
    changes = plan_changes(selected, enabled, {})
    configure = list(changes.configure)
    values: dict[str, str] = {}
    inputs: dict[str, str] = {}
    for feature_id in configure:
        _prompt_feature(feature_by_id(feature_id), values, inputs, raw)

    # 6. "values" evaluation, re-enter or drop until met
    while True:
        changes = plan_changes(selected, enabled, values)
        try:
            cfg, override_warnings = _init_requirements.proposed_config(workspace, changes.sets, changes.unsets)
        except ValueError as exc:
            _say(f"invalid value: {exc}", style="red")
            for feature_id in configure:
                _prompt_feature(feature_by_id(feature_id), values, inputs, raw)
            continue
        chosen = [f for f in CATALOG if f.id in selected]
        ev = _init_requirements.evaluate(
            chosen,
            cfg,
            values=values,
            inputs=_bare_inputs(inputs),
            phase="values",
            probes=_init_probes.PROBES,
        )
        _say(_init_requirements.checks_line(ev))
        blocked = [f for f in chosen if f.id in configure and ev.hard_unmet(f.id)]
        for feature in blocked:
            for result in ev.hard_unmet(feature.id):
                _print_blocked(feature, result)
        if not blocked:
            break
        for feature in blocked:
            choice = _ask(questionary.select(f"{feature.id}: requirement not met", choices=[REENTER, DROP]))
            if choice == DROP:
                selected.discard(feature.id)
                configure.remove(feature.id)
                _drop_values(feature, values, inputs)
            else:
                _prompt_feature(feature, values, inputs, raw)
        errors, warnings = validate_selection(selected, enabled)
        for message in warnings:
            _say(f"warning: {message}", style="yellow")
        if errors:
            for message in errors:
                _say(message, style="red")
            _say("Nothing was changed.")
            return

    for message in override_warnings:
        _say(f"warning: {message}", style="yellow")
    for feature in chosen:
        for result in ev.hard_unmet(feature.id):
            if feature.id not in configure:
                _say(
                    f"warning: {feature.id} is already enabled but a requirement is unmet: "
                    f"{result.reason} (guide: {feature.guide})",
                    style="yellow",
                )
        for result in ev.soft_unmet(feature.id):
            _say(f"warning: {feature.id}: {result.reason} (guide: {feature.guide})", style="yellow")

    # 7. plan and one confirmation
    extra: list[Action] = []
    if changes.sets or changes.unsets:
        extra.append(_MaskedAction(FeatureConfigAction(changes.sets, changes.unsets)))
    if _CENTRAL_NODE in changes.added:
        bind_host, pg_host, pg_port = _central_inputs(inputs)
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
    if not plan.actions:
        _say("Nothing to apply.")
        return
    if not _ask(questionary.confirm("Apply this plan?", default=False)):
        _say("Nothing was changed.")
        return
    runner.apply_plan(plan, confirmed=True)

    # 8. replay line
    _say("To repeat this without prompts:")
    replay = _replay_line(workspace, tuple(configure), changes.removed, values, inputs)
    console.print(replay, soft_wrap=True, markup=False, highlight=False)
    if "***@" in replay:
        _say("Replace *** with the real user:password before running that line; the printed copy hides it.")
