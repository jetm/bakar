"""Edge-level regression tests for ``.arch-rules.toml``.

The architecture fitness check only ever sees the imports that exist in the
tree today. That makes it a happy-path check: a rule set can lose a whole
direction of a boundary and still report "no violations", because no module
happens to exercise the edge yet. Two such defects shipped and were caught by
hand rather than by the check:

* A single eight-entry ``layers`` order ranked the four feature families
  against each other. A ``layers`` order is TOTAL, so that PERMITTED
  ``setup -> feed`` while the ``independent`` rule forbade it, and the checker
  rejected the config outright with six "conflicting rules for edge" errors.
* Splitting that order fixed the conflict but left ``family -> analysis``
  governed by nothing: no order held both (analysis sat in the main order, the
  families in their own), ``independent`` excluded analysis, and the four
  ``forbidden`` rules only named the ``analysis -> family`` direction.
  ``from bakar import triage`` added to ``src/bakar/setup/plan.py`` reported
  "no violations" - the exact edge the file's own prose forbids.

Neither is reachable through the normal check, so these tests assert on the
rule set directly: they re-derive the same directed edges the checker derives
and assert which pairs are forbidden. No import is needed, so a rule can be
proven to bind before any module exercises it.

The edge semantics below mirror ``_rule_edge_constraints`` in devspec's
``core/mechanical/arch.py``. They are reimplemented rather than imported
because devspec is not a bakar dependency; an ``importorskip`` would make
these tests skip on every machine, which is indistinguishable from passing.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

RULES_FILE = Path(__file__).resolve().parent.parent / ".arch-rules.toml"

#: Groups that are mutual peers - none may import another, in either direction.
#: Adding a feature family to ``.arch-rules.toml`` without adding it here (and
#: to the ``independent`` rule) is meant to fail ``test_peer_set_is_exactly``.
PEERS = frozenset({"analysis", "setup", "insights", "feed"})

#: Edges that MUST stay legal. Each is a real dependency direction the stack
#: relies on; a rule change that forbids one of these has over-tightened.
LEGAL_EDGES = [
    ("commands", "steps"),
    ("commands", "analysis"),
    ("commands", "feed"),
    ("commands", "foundation"),
    ("steps", "analysis"),
    ("steps", "feed"),
    ("steps", "foundation"),
    ("analysis", "foundation"),
    ("feed", "foundation"),
    ("setup", "foundation"),
    ("insights", "foundation"),
]


def _rules() -> dict:
    """Parse the committed rules file."""
    return tomllib.loads(RULES_FILE.read_text(encoding="utf-8"))


def _edge_constraints(doc: dict) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Return ``(forbidden, permitted)`` directed edges the whole rule set imposes.

    Mirrors devspec's ``_rule_edge_constraints``:

    - ``forbidden``: forbids ``from -> to``.
    - ``independent``: forbids every ordered pair of distinct members.
    - ``layers``: for positions ``i < j``, permits ``order[i] -> order[j]`` and
      forbids ``order[j] -> order[i]``.
    """
    forbidden: set[tuple[str, str]] = set()
    permitted: set[tuple[str, str]] = set()

    for rule in doc.get("rules", []):
        kind = rule.get("type")
        if kind == "forbidden":
            src, dst = rule.get("from"), rule.get("to")
            if src and dst:
                forbidden.add((src, dst))
        elif kind == "independent":
            members = rule.get("modules", [])
            for src in members:
                for dst in members:
                    if src != dst:
                        forbidden.add((src, dst))
        elif kind == "layers":
            order = rule.get("order", [])
            for i, outer in enumerate(order):
                for inner in order[i + 1 :]:
                    permitted.add((outer, inner))
                    forbidden.add((inner, outer))

    return forbidden, permitted


@pytest.mark.unit
def test_rules_file_parses() -> None:
    """A malformed rules file governs nothing, so parse failure is the first falsifier."""
    doc = _rules()

    assert doc.get("modules"), "no [[modules]] declared"
    assert doc.get("rules"), "no [[rules]] declared"


@pytest.mark.unit
def test_peer_set_is_exactly() -> None:
    """The ``independent`` rule must name exactly the peer groups.

    A family added to the file but not to this rule gets no peer isolation,
    which is the ungoverned-edge defect in a new costume.
    """
    doc = _rules()

    independent = [r for r in doc["rules"] if r.get("type") == "independent"]
    assert len(independent) == 1, f"expected exactly one independent rule, got {len(independent)}"
    assert frozenset(independent[0]["modules"]) == PEERS


@pytest.mark.unit
@pytest.mark.parametrize("src", sorted(PEERS))
@pytest.mark.parametrize("dst", sorted(PEERS))
def test_peer_edges_forbidden_in_both_directions(src: str, dst: str) -> None:
    """Every ordered pair of peers is forbidden.

    This is the regression: ``setup -> analysis`` and ``feed -> analysis`` were
    permitted while their reverses were forbidden, because the prohibition was
    written as four one-directional ``forbidden`` rules instead of a symmetric
    ``independent`` set.
    """
    if src == dst:
        pytest.skip("a group importing itself is an intra-group edge, not a boundary")

    forbidden, _ = _edge_constraints(_rules())

    assert (src, dst) in forbidden, f"{src} -> {dst} is ungoverned: no rule forbids it"


@pytest.mark.unit
@pytest.mark.parametrize(("src", "dst"), LEGAL_EDGES)
def test_legal_edges_are_not_forbidden(src: str, dst: str) -> None:
    """Real dependency directions stay legal - the falsifier for over-tightening."""
    forbidden, _ = _edge_constraints(_rules())

    assert (src, dst) not in forbidden, f"{src} -> {dst} must stay legal but is forbidden"


@pytest.mark.unit
def test_no_edge_is_both_forbidden_and_permitted() -> None:
    """No edge may be claimed by two rules in opposite directions.

    devspec's ``validate_arch_rules`` reports each such pair as a CRITICAL
    config error and the whole rule set stops being evaluated, so a conflict
    disables governance rather than tightening it.
    """
    forbidden, permitted = _edge_constraints(_rules())

    conflicts = sorted(forbidden & permitted)

    assert conflicts == [], f"conflicting rules for edges: {conflicts}"


@pytest.mark.unit
def test_every_rule_references_a_declared_module() -> None:
    """A rule naming an undeclared group silently governs nothing."""
    doc = _rules()
    declared = {m["name"] for m in doc["modules"]}

    referenced: set[str] = set()
    for rule in doc["rules"]:
        referenced.update(rule.get("order", []))
        referenced.update(rule.get("modules", []))
        for key in ("from", "to"):
            if rule.get(key):
                referenced.add(rule[key])

    assert referenced <= declared, f"rules reference undeclared groups: {sorted(referenced - declared)}"
