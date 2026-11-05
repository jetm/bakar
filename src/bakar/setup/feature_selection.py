"""Pure feature-selection logic for ``bakar init``.

Derives which catalog features a raw config already enables, validates a
selection, and plans the config writes. No I/O: callers pass raw config values
and apply the returned plan themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bakar.setup.features import CATALOG, FEATURE_IDS, Feature

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

_CENTRAL_NODE = "central-node"
_CENTRAL_CLIENT = "central-client"


@dataclass(frozen=True, slots=True)
class FeatureChanges:
    """The config writes and feature bookkeeping for one init selection."""

    sets: dict[str, str]
    unsets: tuple[str, ...]
    added: tuple[str, ...]
    removed: tuple[str, ...]
    configure: tuple[str, ...]


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        return text not in ("", "false", "0")
    return False


def _markers_on(feature: Feature, raw: Mapping[str, object]) -> bool:
    if not feature.markers:
        return False
    present = [raw[key] for key in feature.markers if raw.get(key) is not None]
    if feature.default_on and not present:
        return True
    flags = [_truthy(raw.get(key)) for key in feature.markers]
    return any(flags) if feature.marker_mode == "any" else all(flags)


def derive_enabled(raw: Mapping[str, object], *, central_node_units_enabled: bool) -> frozenset[str]:
    """Return the ids of persistent features the raw config already enables."""
    enabled: set[str] = set()
    for feature in CATALOG:
        if not feature.persistent or not _markers_on(feature, raw):
            continue
        if feature.id == _CENTRAL_NODE and not central_node_units_enabled:
            continue
        if feature.id == _CENTRAL_CLIENT and central_node_units_enabled:
            continue
        enabled.add(feature.id)
    return frozenset(enabled)


def validate_selection(selected: Collection[str], enabled: Collection[str]) -> tuple[list[str], list[str]]:
    """Return ``(errors, warnings)`` for a selection against the enabled set."""
    errors: list[str] = []
    warnings: list[str] = []
    chosen = set(selected)
    valid = ", ".join(FEATURE_IDS)
    errors.extend(
        f"unknown feature {feature_id!r}; valid features: {valid}" for feature_id in sorted(chosen - set(FEATURE_IDS))
    )

    seen_pairs: set[frozenset[str]] = set()
    for feature in CATALOG:
        if feature.id not in chosen:
            continue
        errors.extend(
            f"{feature.id} requires {needed}; select {needed} or enable it first"
            for needed in feature.requires
            if needed not in chosen
        )
        for other in feature.excludes:
            pair = frozenset((feature.id, other))
            if other in chosen and pair not in seen_pairs:
                seen_pairs.add(pair)
                first, second = sorted(pair)
                errors.append(f"{first} and {second} are mutually exclusive; choose one")
        for other, message in feature.warns_with:
            if other in chosen:
                warnings.append(f"{feature.id} with {other}: {message}")
    return errors, warnings


def _written_keys(features: list[Feature]) -> set[str]:
    keys: set[str] = set()
    for feature in features:
        keys.update(setting.key for setting in feature.settings)
        keys.update(feature.fixed)
    return keys


def plan_changes(
    selected: Collection[str],
    enabled: Collection[str],
    values: Mapping[str, str],
) -> FeatureChanges:
    """Plan the config writes that move ``enabled`` to ``selected``."""
    chosen = set(selected)
    on = set(enabled)
    picked = [f for f in CATALOG if f.id in chosen]
    dropped = [f for f in CATALOG if f.id in on and f.id not in chosen]

    sets: dict[str, str] = {}
    for feature in picked:
        for setting in feature.settings:
            value = values.get(setting.key)
            if value:
                sets[setting.key] = value
        sets.update(feature.fixed)

    protected = _written_keys(picked)
    unsets: set[str] = set()
    for feature in dropped:
        for key, reset in feature.off.items():
            if key in protected:
                continue
            if reset is None:
                unsets.add(key)
            else:
                sets[key] = reset

    added = tuple(f.id for f in picked if f.persistent and f.id not in on)
    configure = tuple(f.id for f in picked if f.id in added or not f.persistent)
    return FeatureChanges(
        sets=sets,
        unsets=tuple(sorted(unsets)),
        added=added,
        removed=tuple(f.id for f in dropped),
        configure=configure,
    )
