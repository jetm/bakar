"""Requirement evaluation for the ``bakar init`` feature step.

The feature catalog (:mod:`bakar.setup.features`) names its requirements as
data: a registered doctor check, a binary, a path, a setting, or a probe id.
This module resolves those references against a proposed configuration -
built through the normal user-config load and :func:`bakar.config.resolve`
path from a temporary copy of the user config, so the real file is never
written - and reports per-feature results plus how many scheduled checks
actually produced a result.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from bakar import config, diagnostics, user_config
from bakar.setup.features import Feature, Requirement, feature_by_id

if TYPE_CHECKING:
    from bakar.config import BuildConfig

Phase = Literal["pre", "values"]
ProbeCallable = Callable[["BuildConfig", Mapping[str, str], Mapping[str, str]], tuple[bool, str]]
CheckRunner = Callable[..., list[diagnostics.CheckResult]]

_DID_NOT_RUN = "not verified: the check did not run"
_UNKNOWN_PROBE = "not verified: unknown probe"

# Stands in for "the effective configuration builds on the host" so a
# mode == "host" feature that is unavailable in container mode shows up in
# hard_unmet() like any other refused requirement.
_HOST_MODE_REQUIREMENT = Requirement(
    "setting",
    "build.container",
    hard=True,
    fix="build in host mode (the default): set build.container to false and unset BAKAR_CONTAINER",
)


def _setting_fields() -> dict[str, str]:
    """Map each dotted settings key to the config field its table maps it to."""
    out: dict[str, str] = {}
    for section, mapping, _extra in user_config.USER_CONFIG_TABLES:
        for key, field_name in mapping.items():
            out[".".join((*section, key))] = field_name
    return out


_SETTING_FIELDS = _setting_fields()
_BUILD_CONFIG_FIELDS = frozenset(f.name for f in dataclasses.fields(config.BuildConfig))


def build_config_field(key: str) -> str | None:
    """The ``BuildConfig`` attribute a settings key resolves into, or None."""
    field_name = _SETTING_FIELDS.get(key)
    if field_name is None or field_name not in _BUILD_CONFIG_FIELDS:
        return None
    return field_name


def _default_user_config_path() -> Path:
    return Path.home() / ".config" / "bakar" / "config.toml"


def proposed_config(
    workspace: Path,
    sets: Mapping[str, str],
    unsets: Iterable[str],
    *,
    user_config_path: Path | None = None,
) -> tuple[BuildConfig, list[str]]:
    """Resolve the config the next build would see after ``sets``/``unsets``.

    The changes are applied to a temporary copy of the user config; the real
    file is only ever read. Returns the resolved config and one warning per
    set key whose resolved value differs from the proposed one (an
    environment variable or the workspace ``.bakar.toml`` overrides it).
    """
    real = user_config_path if user_config_path is not None else _default_user_config_path()
    with tempfile.TemporaryDirectory(prefix="bakar-init-") as tmp:
        copy = Path(tmp) / "config.toml"
        if real.is_file():
            shutil.copyfile(real, copy)
        user_config.apply_settings(sets, list(unsets), path=copy)
        loaded = user_config.load_user_config(copy)
    cfg = config.resolve(config.ResolveRequest(workspace=workspace, user_config=loaded))

    warnings: list[str] = []
    for key in sets:
        field_name = build_config_field(key)
        if field_name is None:
            continue
        proposed = getattr(loaded, field_name, None)
        resolved = getattr(cfg, field_name, None)
        if proposed == resolved:
            continue
        env_name = "BAKAR_" + key.rsplit(".", 1)[-1].upper()
        if env_name in os.environ:
            warnings.append(f"{key} is overridden by {env_name} (resolved to {resolved!r}, proposed {proposed!r})")
        else:
            warnings.append(
                f"{key} is overridden by an environment variable or the workspace .bakar.toml "
                f"(resolved to {resolved!r}, proposed {proposed!r})"
            )
    return cfg, warnings


@dataclass(frozen=True, slots=True)
class RequirementResult:
    """One requirement's outcome. ``met`` is None when it was not checked."""

    requirement: Requirement
    met: bool | None
    reason: str
    applicable: bool = True


@dataclass(slots=True)
class Evaluation:
    results: dict[str, list[RequirementResult]] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    scheduled: int = 0
    ran: int = 0

    def results_for(self, feature_id: str) -> list[RequirementResult]:
        return list(self.results.get(feature_id, ()))

    def hard_unmet(self, feature_id: str) -> list[RequirementResult]:
        return [r for r in self.results.get(feature_id, ()) if r.applicable and r.met is False and r.requirement.hard]

    def soft_unmet(self, feature_id: str) -> list[RequirementResult]:
        return [
            r for r in self.results.get(feature_id, ()) if r.applicable and r.met is False and not r.requirement.hard
        ]


def checks_line(ev: Evaluation) -> str:
    """``checks: N/M run``: N scheduled check names that returned a result, of M."""
    return f"checks: {ev.ran}/{ev.scheduled} run"


def _has_value(value: object) -> bool:
    return value is not None and value != ""


def _missing_required(
    feature: Feature, cfg: BuildConfig, values: Mapping[str, str], inputs: Mapping[str, str]
) -> list[str]:
    missing: list[str] = []
    for setting in feature.settings:
        if not setting.required or _has_value(values.get(setting.key)):
            continue
        field_name = build_config_field(setting.key)
        if field_name is not None and _has_value(getattr(cfg, field_name, None)):
            continue
        missing.append(setting.key)
    for spec in feature.inputs:
        if not spec.required:
            continue
        if _has_value(inputs.get(spec.key)) or _has_value(inputs.get(f"{feature.id}.{spec.key}")):
            continue
        missing.append(f"{feature.id}.{spec.key}")
    return missing


def _check_reason(result: diagnostics.CheckResult) -> str:
    if result.fix_hint:
        return f"{result.message}; {result.fix_hint}"
    return result.message


def _evaluate_check(req: Requirement, results: Mapping[str, diagnostics.CheckResult]) -> RequirementResult:
    result = results.get(req.target)
    if result is None:
        return RequirementResult(req, met=False, reason=_DID_NOT_RUN)
    if result.status is diagnostics.Status.PASS:
        return RequirementResult(req, met=True, reason=result.message)
    if result.status is diagnostics.Status.SKIP and req.skip_ok and not result.message.startswith("not run:"):
        # "not run:" is run_all's cache-mounts gate refusing to start the check,
        # which is the opposite of the check having found nothing to do.
        return RequirementResult(req, met=True, reason=result.message)
    return RequirementResult(req, met=False, reason=_check_reason(result))


def _evaluate_local(
    req: Requirement,
    cfg: BuildConfig,
    values: Mapping[str, str],
    inputs: Mapping[str, str],
    probes: Mapping[str, ProbeCallable],
) -> RequirementResult:
    if req.kind == "binary":
        found = shutil.which(req.target)
        if found:
            return RequirementResult(req, met=True, reason=f"{req.target} found at {found}")
        return RequirementResult(req, met=False, reason=f"{req.target} not found on PATH")
    if req.kind == "path":
        if Path(req.target).expanduser().exists():
            return RequirementResult(req, met=True, reason=f"{req.target} exists")
        return RequirementResult(req, met=False, reason=f"{req.target} does not exist")
    if req.kind == "setting":
        field_name = build_config_field(req.target)
        resolved = getattr(cfg, field_name, None) if field_name is not None else None
        if values.get(req.target) or resolved:
            return RequirementResult(req, met=True, reason=f"{req.target} is set")
        return RequirementResult(req, met=False, reason=f"{req.target} is not set")
    probe = probes.get(req.target)
    if probe is None:
        return RequirementResult(req, met=False, reason=f"{_UNKNOWN_PROBE} {req.target!r}")
    try:
        met, reason = probe(cfg, values, inputs)
    except Exception as exc:  # noqa: BLE001 - one probe's bug must not abort the feature step
        return RequirementResult(req, met=False, reason=f"probe {req.target} crashed: {exc!r}")
    return RequirementResult(req, met=bool(met), reason=reason)


def _resolve_features(features: Iterable[Feature | str]) -> list[Feature]:
    return [feature_by_id(f) if isinstance(f, str) else f for f in features]


def evaluate(
    features: Iterable[Feature | str],
    cfg: BuildConfig,
    *,
    values: Mapping[str, str],
    inputs: Mapping[str, str],
    phase: Phase,
    probes: Mapping[str, ProbeCallable],
    deadline: float = 30.0,
    run_checks: CheckRunner | None = None,
    explain: bool = False,
) -> Evaluation:
    """Evaluate the ``phase`` requirements of ``features`` against ``cfg``.

    "pre" covers requirements with ``needs_values`` False, "values" those with
    True. Every check-kind target of the phase goes to ``run_checks`` in a
    single call; ``run_checks`` defaults to :func:`bakar.diagnostics.run_all`,
    looked up at call time so a patched runner is honoured.
    """
    runner: CheckRunner = run_checks if run_checks is not None else diagnostics.run_all
    want_values = phase == "values"
    ev = Evaluation()

    # Check-kind rows are staged as None and filled after the single run.
    staged: dict[str, list[RequirementResult | None]] = {}
    pending: list[tuple[str, int, Requirement]] = []
    check_names: list[str] = []

    for feature in _resolve_features(features):
        rows: list[RequirementResult | None] = []
        if feature.mode == "host" and not cfg.host_mode:
            reason = f"{feature.id} needs host mode; the resolved configuration builds in a container"
            ev.unavailable[feature.id] = reason
            if phase == "pre":
                rows.append(RequirementResult(_HOST_MODE_REQUIREMENT, met=False, reason=reason))
        missing = _missing_required(feature, cfg, values, inputs) if explain and want_values else []
        for req in feature.requirements:
            if req.needs_values != want_values:
                continue
            if req.host_only and not cfg.host_mode:
                rows.append(
                    RequirementResult(req, met=True, reason="not applicable in container mode", applicable=False)
                )
                continue
            if missing:
                rows.append(RequirementResult(req, met=None, reason="not checked: set " + ", ".join(missing)))
                continue
            if req.kind == "check":
                pending.append((feature.id, len(rows), req))
                rows.append(None)
                if req.target not in check_names:
                    check_names.append(req.target)
                continue
            rows.append(_evaluate_local(req, cfg, values, inputs, probes))
        staged[feature.id] = rows

    by_name: dict[str, diagnostics.CheckResult] = {}
    if check_names:
        for result in runner(cfg, None, only=frozenset(check_names), deadline=deadline):
            by_name.setdefault(result.name, result)
    ev.scheduled = len(check_names)
    ev.ran = sum(1 for name in check_names if name in by_name)

    for feature_id, index, req in pending:
        staged[feature_id][index] = _evaluate_check(req, by_name)
    ev.results = {fid: [r for r in rows if r is not None] for fid, rows in staged.items()}
    return ev
