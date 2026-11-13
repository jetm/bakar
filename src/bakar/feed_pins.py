"""Compare the commits a kas file pins with the commits a host build used.

Pure comparison: the declared mapping and the host description are arguments;
nothing is read and no git runs. ``meta-avocado`` is the layer the feed
delivers, so it is reported as ``delivered`` and never refuses a refresh.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bakar.feed_describe import LayerCommit

DELIVERED_REPO = "meta-avocado"
_MIN_PREFIX = 7
_HEX = frozenset("0123456789abcdef")

Verdict = Literal["match", "differs", "missing"]


@dataclass(frozen=True)
class PinVerdict:
    repo: str
    declared: str
    observed: str | None
    verdict: Verdict


@dataclass(frozen=True)
class Refusal:
    repo: str
    declared: str
    observed: str | None
    verdict: Verdict


@dataclass(frozen=True)
class PinReport:
    verdicts: tuple[PinVerdict, ...]
    refusals: tuple[Refusal, ...]
    delivered: str | None
    observed: Mapping[str, str | None]


def _matches(declared: str, observed: str) -> bool:
    d, o = declared.lower(), observed.lower()
    if d == o:
        return True
    return _MIN_PREFIX <= len(d) < len(o) and set(d) <= _HEX and o.startswith(d)


def _basename(path: object) -> str | None:
    if not isinstance(path, str):
        return None
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return name or None


def compare_pins(declared: Mapping[str, object], observed: tuple[LayerCommit, ...]) -> PinReport:
    """Verdict per declared repository that carries a ``commit``."""
    seen = {layer.repo: layer.commit for layer in observed}
    verdicts: list[PinVerdict] = []
    for name in sorted(declared):
        entry = declared[name]
        if name == DELIVERED_REPO or not isinstance(entry, dict):
            continue
        pin = entry.get("commit")
        if not isinstance(pin, str) or not pin:
            continue
        key = name
        if key not in seen:
            base = _basename(entry.get("path"))
            if base is not None and base in seen:
                key = base
        got = seen.get(key)
        if got is None:
            verdict: Verdict = "missing"
        elif _matches(pin, got):
            verdict = "match"
        else:
            verdict = "differs"
        verdicts.append(PinVerdict(repo=name, declared=pin, observed=got, verdict=verdict))
    refusals = tuple(
        Refusal(repo=v.repo, declared=v.declared, observed=v.observed, verdict=v.verdict)
        for v in verdicts
        if v.verdict != "match"
    )
    return PinReport(
        verdicts=tuple(verdicts),
        refusals=refusals,
        delivered=seen.get(DELIVERED_REPO),
        observed=dict(seen),
    )


def describe_refusal(report: PinReport) -> str:
    """One line naming each refusing repository, its pin and what the host built."""
    parts = []
    for r in report.refusals:
        if r.observed is None:
            parts.append(f"{r.repo} declared {r.declared} but the host reported no evidence for it")
        else:
            parts.append(f"{r.repo} declared {r.declared} but the host built {r.observed}")
    return "; ".join(parts)
