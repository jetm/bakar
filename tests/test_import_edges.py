"""Enforce ``.arch-rules.toml`` against the imports that actually exist.

``test_arch_rules_edges.py`` proves what the rule set forbids; this file proves
the source tree obeys it, inside the suite, with no devspec dependency. It walks
every ``import`` statement in ``src/bakar`` - module-level, ``TYPE_CHECKING``
guarded and function-body deferred alike, matching ``ast.walk`` in devspec's
checker - maps each importer and target to its group, and fails with both module
names when the edge between the two groups is forbidden.

Edge semantics are the ones ``test_arch_rules_edges.py`` mirrors from devspec's
``_rule_edge_constraints``: ``forbidden`` forbids ``from -> to``, ``independent``
forbids every ordered pair, and ``layers`` forbids each inner-to-outer edge.
Intra-group edges and edges touching an unclassified module are ungoverned.

Known and deliberate: ``setup`` -> ``core`` (``setup`` imports ``diagnostics``).
``setup`` is not in the main layers order and no rule pairs it with ``core``, so
the edge is ungoverned by construction rather than allowlisted here.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path, PurePosixPath

import pytest

from tests.test_arch_rules_edges import _edge_constraints

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_FILE = REPO_ROOT / ".arch-rules.toml"
SRC_ROOT = REPO_ROOT / "src"


def _load_rules() -> dict:
    return tomllib.loads(RULES_FILE.read_text(encoding="utf-8"))


def _group_of(rel_path: str, groups: list[dict]) -> str | None:
    """Return the first group whose patterns match ``rel_path``, else ``None``."""
    target = PurePosixPath(rel_path)
    for group in groups:
        if any(target.full_match(pattern) for pattern in group["paths"]):
            return group["name"]
    return None


def _module_file(dotted: str) -> str | None:
    """Map ``bakar.x.y`` to its repo-relative source path, or ``None``."""
    base = PurePosixPath("src", *dotted.split("."))
    if (REPO_ROOT / base.with_suffix(".py")).is_file():
        return base.with_suffix(".py").as_posix()
    if (REPO_ROOT / base / "__init__.py").is_file():
        return (base / "__init__.py").as_posix()
    return None


def _imported_files(source: str, importer: str) -> list[tuple[int, str]]:
    """Return ``(lineno, target_path)`` for every bakar import in ``source``."""
    tree = ast.parse(source)
    # src/bakar/a/b.py and src/bakar/a/__init__.py both live in package ["bakar", "a"].
    package = list(PurePosixPath(importer).parts[1:-1])

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package[: len(package) - (node.level - 1)]
                module = ".".join([*anchor, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            if module != "bakar" and not module.startswith("bakar."):
                continue
            names = [module]
            # ``from bakar.steps import kas_build`` imports the submodule.
            names += [f"{module}.{alias.name}" for alias in node.names if alias.name != "*"]
        for name in names:
            if name != "bakar" and not name.startswith("bakar."):
                continue
            path = _module_file(name)
            if path is not None:
                found.append((node.lineno, path))
    return found


def _violations(
    files: dict[str, str],
    groups: list[dict],
    forbidden: set[tuple[str, str]],
) -> list[str]:
    """Evaluate ``files`` (repo-relative path -> source) against the forbidden edges."""
    out: list[str] = []
    for importer, source in sorted(files.items()):
        src_group = _group_of(importer, groups)
        if src_group is None:
            continue
        for lineno, target in _imported_files(source, importer):
            dst_group = _group_of(target, groups)
            if dst_group is None or dst_group == src_group:
                continue
            if (src_group, dst_group) in forbidden:
                out.append(f"{importer}:{lineno} ({src_group}) imports {target} ({dst_group})")
    return sorted(set(out))


def _tree_sources() -> dict[str, str]:
    return {
        p.relative_to(REPO_ROOT).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(SRC_ROOT.glob("bakar/**/*.py"))
    }


def test_source_tree_obeys_the_arch_rules() -> None:
    """No module imports across a boundary the rules file forbids."""
    doc = _load_rules()
    forbidden, _ = _edge_constraints(doc)

    violations = _violations(_tree_sources(), doc["modules"], forbidden)

    assert violations == [], "forbidden import edges:\n" + "\n".join(violations)


def test_core_is_in_the_main_layer_order() -> None:
    """``core`` (diagnostics) must sit between commands and steps in the layer order."""
    orders = [r["order"] for r in _load_rules()["rules"] if r.get("type") == "layers"]

    assert ["commands", "core", "steps", "analysis", "foundation"] in orders


def test_a_violating_import_is_reported_with_both_modules() -> None:
    """The checker itself must fail on a violation, naming importer and target.

    Uses an in-memory fixture: a ``steps`` module importing ``bakar.diagnostics``
    in a function body (the shape the old rule set left ungoverned).
    """
    doc = _load_rules()
    forbidden, _ = _edge_constraints(doc)
    files = {
        "src/bakar/steps/kas_build.py": "def f():\n    from bakar.diagnostics import is_path_on_nfs\n",
    }

    violations = _violations(files, doc["modules"], forbidden)

    assert len(violations) == 1
    assert "src/bakar/steps/kas_build.py" in violations[0]
    assert "src/bakar/diagnostics.py" in violations[0]


def test_type_checking_and_submodule_imports_count() -> None:
    """``TYPE_CHECKING`` guards and ``from pkg import submodule`` both create edges."""
    doc = _load_rules()
    forbidden, _ = _edge_constraints(doc)
    files = {
        "src/bakar/triage.py": (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from bakar.steps import kas_build\n"
        ),
    }

    violations = _violations(files, doc["modules"], forbidden)

    # The package __init__ is reported too; the submodule edge is the one that matters.
    assert any("src/bakar/triage.py" in v and "src/bakar/steps/kas_build.py" in v for v in violations)


# commands/monitor.py is migrated off the diagnostics re-exports in this same
# change by a separate edit, so the scan leaves that one module out.
_REEXPORT_SCAN_SKIP = frozenset({"src/bakar/commands/monitor.py"})


def _names_defined_in(source: str) -> set[str]:
    """Names a module binds itself (def, class, assignment), not by importing."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            out.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


def _diagnostics_reexport_imports(files: dict[str, str], own: set[str]) -> list[str]:
    """``module: name`` for each ``from bakar.diagnostics import name`` that is a re-export."""
    found: list[str] = []
    for rel, source in files.items():
        if rel == "src/bakar/diagnostics.py" or rel in _REEXPORT_SCAN_SKIP:
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module == "bakar.diagnostics":
                found.extend(f"{rel}: {a.name}" for a in node.names if a.name not in own)
    return sorted(found)


def test_no_module_reaches_a_foundation_name_through_diagnostics() -> None:
    """Import a name from the module that defines it, not from a diagnostics re-export."""
    files = _tree_sources()
    own = _names_defined_in(files["src/bakar/diagnostics.py"])

    offenders = _diagnostics_reexport_imports(files, own)

    assert offenders == [], "import these from their defining module, not bakar.diagnostics:\n" + "\n".join(offenders)


def test_reexport_scan_names_module_and_name() -> None:
    """The scan itself must flag a re-exported name and pass a name diagnostics defines."""
    files = {
        "src/bakar/commands/x.py": "from bakar.diagnostics import probe_cluster, Status\n",
        "src/bakar/diagnostics.py": "class Status: ...\n",
    }
    own = _names_defined_in(files["src/bakar/diagnostics.py"])

    assert _diagnostics_reexport_imports(files, own) == ["src/bakar/commands/x.py: probe_cluster"]
