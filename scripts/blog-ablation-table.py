#!/usr/bin/env python3
"""Aggregate VARIS-18 stress-parse summaries into a markdown ablation table.

Walks ``<bsp>/build/runs/*/stress-parse/summary.json`` under both NXP and TI
trees, filters to summaries that carry a ``label`` field whose value matches
``--label-prefix`` (default ``blog-``), groups by label, and emits a markdown
table with fire rate per label. Multiple runs that share a label are summed:
``passed`` and ``runs`` add together so n=20 across two n=10 invocations
shows up as a single n=20 row.

The label format the blog post uses is ``blog-py<ver>-<config>`` where
``<ver>`` is ``3.12``, ``3.14``, ``3.15``, ``3.15a8``, etc. and
``<config>`` is one of ``A`` through ``E``. Order in the output table
follows that schema; unknown labels sort lexicographically at the end.

Usage::

    python bspctl/scripts/blog-ablation-table.py
    python bspctl/scripts/blog-ablation-table.py --label-prefix blog-
    python bspctl/scripts/blog-ablation-table.py --workspace ~/repos/personal/bspctl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

CONFIG_LABELS = {
    "A": "Stock baseline",
    "B": "gc.freeze only",
    "C": "PYTHONMALLOC only",
    "D": "Minimal (gc.freeze + PYTHONMALLOC)",
    "E": "Full stack",
}

LABEL_RE = re.compile(r"^blog-py(?P<ver>[\w.]+)-(?P<config>[A-E])(?:-.*)?$")


def find_summaries(workspace: Path) -> list[Path]:
    """Return every summary.json under <workspace>/{nxp,ti}/build/runs/*/stress-parse/."""
    summaries: list[Path] = []
    for bsp in ("nxp", "ti"):
        runs_dir = workspace / bsp / "build" / "runs"
        if not runs_dir.is_dir():
            continue
        summaries.extend(runs_dir.glob("*/stress-parse/summary.json"))
    return summaries


def load_summary(path: Path) -> dict | None:
    """Parse a summary.json; skip files with no ``label`` field or invalid JSON."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if "label" not in data:
        return None
    return data


def aggregate(summaries: list[dict], label_prefix: str) -> dict[str, dict]:
    """Group summaries by label, summing runs/passed/failed across each group."""
    groups: dict[str, dict] = defaultdict(lambda: {"runs": 0, "passed": 0, "failed": 0, "elapsed_seconds": []})
    for s in summaries:
        label = s.get("label", "")
        if not label.startswith(label_prefix):
            continue
        g = groups[label]
        g["runs"] += s.get("runs", 0)
        g["passed"] += s.get("passed", 0)
        g["failed"] += s.get("failed", 0)
        g["elapsed_seconds"].extend(s.get("elapsed_seconds", []))
    return groups


def sort_key(label: str) -> tuple:
    """Sort by Python version (numeric where possible) then config letter."""
    m = LABEL_RE.match(label)
    if not m:
        return (1, label)
    ver = m.group("ver")
    config = m.group("config")
    # Numeric major.minor first, alpha/beta suffix second
    try:
        major_minor = tuple(int(x) for x in ver.split(".")[:2] if x.isdigit())
    except ValueError:
        major_minor = (0,)
    suffix = "".join(c for c in ver if not c.isdigit() and c != ".")
    return (0, major_minor, suffix, config)


def render_markdown(groups: dict[str, dict]) -> str:
    """Emit a markdown table from the grouped fire-rate data."""
    if not groups:
        return "No labelled summaries found."
    rows = sorted(groups.items(), key=lambda kv: sort_key(kv[0]))
    lines = [
        "| Label | Config | Python | n | passed | failed | rate |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for label, g in rows:
        m = LABEL_RE.match(label)
        config = m.group("config") if m else "?"
        ver = m.group("ver") if m else "?"
        config_desc = CONFIG_LABELS.get(config, config)
        n = g["runs"]
        rate = (g["failed"] / n * 100.0) if n else 0.0
        lines.append(
            f"| `{label}` | {config}. {config_desc} | {ver} | {n} | {g['passed']} | {g['failed']} | {rate:.1f}% |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Variscite workspace root (default: auto-detect from script path)",
    )
    parser.add_argument(
        "--label-prefix",
        default="blog-",
        help="Only consider summaries whose label starts with this prefix",
    )
    parser.add_argument(
        "--show-incomplete",
        action="store_true",
        help="Also list label groups whose run count is not a multiple of 20",
    )
    args = parser.parse_args()

    paths = find_summaries(args.workspace)
    summaries = [s for p in paths if (s := load_summary(p)) is not None]
    groups = aggregate(summaries, args.label_prefix)

    if args.show_incomplete:
        incomplete = {k: v for k, v in groups.items() if v["runs"] % 20 != 0}
        if incomplete:
            print("# Incomplete groups (n != 20*k):", file=sys.stderr)
            for k, v in sorted(incomplete.items()):
                print(f"  {k}: n={v['runs']}", file=sys.stderr)

    print(render_markdown(groups))
    return 0


if __name__ == "__main__":
    sys.exit(main())
