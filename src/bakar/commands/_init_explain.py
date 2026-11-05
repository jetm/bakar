"""``bakar init --explain <feature>``: a read-only report of one feature.

Prints the feature's title and summary, each requirement as met, unmet or not
checked, the fix for every unmet one, and the guide page. Value-dependent
requirements are evaluated against the current configuration (no proposed
changes). Nothing is written, no plan is applied and no workspace is scaffolded;
the only processes started are the read-only probes the requirements use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bakar.commands import _init_probes, _init_requirements
from bakar.commands._init_features import _fail, _say
from bakar.setup.features import FEATURE_IDS, feature_by_id

if TYPE_CHECKING:
    from pathlib import Path


def _status(result: _init_requirements.RequirementResult) -> str:
    if not result.applicable:
        return "not applicable"
    if result.met is None:
        return "not checked"
    return "met" if result.met else "unmet"


def run_explain(feature_id: str, workspace: Path) -> None:
    """Print the explanation for ``feature_id``; exit 1 for an unknown id."""
    if feature_id not in FEATURE_IDS:
        raise _fail(f"unknown feature {feature_id!r}; valid ids: {', '.join(FEATURE_IDS)}")
    feature = feature_by_id(feature_id)

    try:
        cfg, _ = _init_requirements.proposed_config(workspace, {}, ())
    except ValueError as exc:
        raise _fail(str(exc)) from exc

    results: list[_init_requirements.RequirementResult] = []
    unavailable: dict[str, str] = {}
    scheduled = ran = 0
    for phase in ("pre", "values"):
        ev = _init_requirements.evaluate(
            [feature],
            cfg,
            values={},
            inputs={},
            phase=phase,
            probes=_init_probes.PROBES,
            explain=True,
        )
        results.extend(ev.results_for(feature_id))
        unavailable.update(ev.unavailable)
        scheduled += ev.scheduled
        ran += ev.ran

    _say(f"{feature.title} ({feature.id})")
    _say(feature.summary)
    if feature_id in unavailable:
        _say(unavailable[feature_id], style="yellow")
    _say("Requirements:")
    if not results:
        _say("  none")
    for result in results:
        kind = "required" if result.requirement.hard else "recommended"
        _say(f"  {_status(result)}: {result.requirement.target} ({kind}) - {result.reason}")
        if result.met is False and result.applicable:
            _say(f"    fix: {result.requirement.fix}")
    _say(f"checks: {ran}/{scheduled} run")
    _say(f"Guide: {feature.guide}")
