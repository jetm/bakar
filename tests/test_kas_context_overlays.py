"""Every command that opens a kas context must hand it the build's overlay set.

A meta-avocado kas step re-dumps the configuration into ``avocado-bakar.yml``,
and that file is the next run's entry. A context built with the base overlay
only therefore rewrites the file without the tuning sections (mold, hashequiv,
uninative, arch probes) the last build ran with. The dump-family commands avoid
that by passing ``extra_overlays``; this pins the rest of them to the same rule.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

COMMANDS_DIR = Path(__file__).resolve().parent.parent / "src" / "bakar" / "commands"

# ``_make_kas_ctx`` builds the build's own context; run_build receives the
# overlays as an argument instead and hands them on itself.
EXEMPT_FUNCTIONS = {"_make_kas_ctx"}


def _context_calls_without_overlays() -> list[str]:
    offenders: list[str] = []
    for path in sorted(COMMANDS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "KasBuildContext"):
                continue
            func = node
            while func in parents and not isinstance(func, ast.FunctionDef):
                func = parents[func]
            if isinstance(func, ast.FunctionDef) and func.name in EXEMPT_FUNCTIONS:
                continue
            if "extra_overlays" not in {kw.arg for kw in node.keywords}:
                offenders.append(f"{path.name}:{node.lineno}")
    return offenders


@pytest.mark.unit
def test_every_command_kas_context_carries_the_overlay_set() -> None:
    assert _context_calls_without_overlays() == []
