"""Post-mortem triage for a failed build run.

Reads a run directory produced by :mod:`bspctl.observability`, locates
the first ``step_fail`` event, and surfaces the relevant portion of
``kas.log`` plus (for bitbake failures) the specific recipe log that
triggered the stop.

Fixture suggestions are keyed off the failure pattern so the user does not
have to re-read bitbake's output to figure out what to try next.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from bspctl.fork_race_signatures import (
    FORK_RACE_SIGNATURES,
    FORK_RACE_SUGGESTION,
)


@dataclass(frozen=True)
class RecipeError:
    recipe: str
    task: str
    excerpt: str


@dataclass(frozen=True)
class TriageReport:
    run_dir: Path
    failing_step: str | None
    fail_reason: str | None
    kas_log_tail: list[str]
    recipe_log: Path | None
    recipe_log_tail: list[str]
    suggestions: list[str]
    recipe_errors: list[RecipeError]


def _last_event_matching(events_path: Path, event_name: str) -> dict | None:
    last: dict | None = None
    if not events_path.is_file():
        return None
    for line in events_path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") == event_name:
            last = rec
    return last


def _bitbake_override_summary(events_path: Path) -> str | None:
    """Return a one-line note describing override state during the run.

    Scans ``events.jsonl`` for the last ``bitbake_override`` step event
    (ok or skip) and renders it for the triage suggestions block.
    Returns ``None`` when no event is present (older bspctl runs, or the
    step never executed in this pipeline).
    """
    if not events_path.is_file():
        return None
    last: dict | None = None
    for line in events_path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("step") == "bitbake_override" and rec.get("event") in ("step_ok", "step_skip"):
            last = rec
    if last is None:
        return None
    if last.get("event") == "step_ok":
        branch = last.get("branch") or "?"
        sha = last.get("sha") or "?"
        upstream = last.get("upstream_version") or "?"
        return f"bitbake-override active during this run: branch={branch} sha={sha} upstream={upstream}"
    return f"bitbake-override skipped during this run: {last.get('reason', 'unknown')}"


def _tail(path: Path, n: int = 80) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(errors="replace").splitlines()
    return lines[-n:]


_RECIPE_LOG_RE = re.compile(r"Logfile of failure stored in: (?P<path>/[^\s]+)")

# Matches bitbake-reported recipe-level failures in kas.log.  Shape is
# `ERROR: <recipe> do_<task>: <message>` with any leading log-formatter
# prefix (timestamp, level tag, colour codes).  Used when events.jsonl
# doesn't carry a step_fail - or alongside it - to surface which recipes
# actually broke.  Example match:
#   ERROR: firmware-nxp-wifi-1.0-r0 do_fetch: Fetcher failure: ...
_RECIPE_ERROR_RE = re.compile(
    r"ERROR: (?P<recipe>[\w\-\.\+]+) "
    r"do_(?P<task>fetch|compile|configure|install|populate_sysroot|rootfs|unpack|patch): "
    r"(?P<msg>.+)$"
)


def _scan_recipe_errors(kas_log: Path, cap: int = 10) -> list[RecipeError]:
    """Walk kas.log for bitbake recipe-level ERROR lines.

    Returns up to ``cap`` distinct ``(recipe, task)`` pairs with a short
    one-line excerpt (truncated to ~120 chars).  Order is first-seen.
    """
    if not kas_log.is_file():
        return []
    seen: set[tuple[str, str]] = set()
    out: list[RecipeError] = []
    for line in kas_log.read_text(errors="replace").splitlines():
        m = _RECIPE_ERROR_RE.search(line)
        if not m:
            continue
        key = (m.group("recipe"), m.group("task"))
        if key in seen:
            continue
        seen.add(key)
        msg = m.group("msg").strip()
        if len(msg) > 120:
            msg = msg[:117] + "..."
        out.append(RecipeError(recipe=key[0], task=key[1], excerpt=msg))
        if len(out) >= cap:
            break
    return out


def _find_recipe_log(kas_log: Path, workspace: Path) -> Path | None:
    """Scan kas.log for bitbake's Logfile hint and rewrite the container
    path (/work/...) to a host path under ``workspace``."""
    if not kas_log.is_file():
        return None
    for line in kas_log.read_text(errors="replace").splitlines():
        m = _RECIPE_LOG_RE.search(line)
        if not m:
            continue
        container_path = m.group("path")
        host_path = Path(container_path.replace("/work/", str(workspace) + "/", 1))
        if host_path.is_file():
            return host_path
    return None


_SUGGESTIONS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"do_fetch:.*Fetcher failure"),
        "Fetch failure: retry, or add a PREMIRROR for the recipe's upstream URL.",
    ),
    (
        # Manifestations of the fork-in-multi-threaded-program race in
        # bitbake's parser. Patterns live in
        # bspctl.fork_race_signatures so the empirical stress-test
        # harness in steps/stress_parse.py shares the same set; new
        # variants only need adding once.
        re.compile("|".join(p.pattern for p in FORK_RACE_SIGNATURES)),
        FORK_RACE_SUGGESTION,
    ),
    (
        re.compile(r"/bin/sh: \d+: ccache [^:]+: not found"),
        "cmake ccache launcher quoted wrong (meta-oe renderdoc-style bug). "
        'Override CMAKE_CXX_COMPILER_LAUNCHER:pn-<recipe> = "" and '
        'CMAKE_C_COMPILER_LAUNCHER:pn-<recipe> = "" in the kas YAML\'s '
        "local_conf_header.",
    ),
    (
        re.compile(r"out of memory|Cannot allocate memory|OOM"),
        "Out of memory. Lower BB_NUMBER_THREADS or close RAM-heavy apps.",
    ),
    (re.compile(r"No space left on device"), "Disk full. Check df on /, and your SSTATE_DIR / DL_DIR mounts."),
    (
        re.compile(r"Failed to fetch URL git://github.com"),
        "github.com clone flaked. Check network; ensure forks/linux-imx is populated "
        "so the local PREMIRROR handles linux-imx without going external.",
    ),
    (
        re.compile(r"ACCEPT_FSL_EULA"),
        'EULA not accepted. Ensure ACCEPT_FSL_EULA = "1" is in the kas YAML\'s '
        "local_conf_header (the overlay file injects it at build time for NXP).",
    ),
    (re.compile(r"ERROR: When reparsing"), "Stale bitbake cache. Remove build/cache and retry."),
    (
        re.compile(r"Config file validation Error"),
        "kas YAML schema violation. Verify `kas --version` matches the kas-container "
        "version (mismatch causes obscure schema errors).",
    ),
]


def _match_suggestions(text: str) -> list[str]:
    hits: list[str] = []
    for pattern, suggestion in _SUGGESTIONS:
        if pattern.search(text):
            hits.append(suggestion)
    return hits


def analyse(run_dir: Path, workspace: Path) -> TriageReport:
    events_path = run_dir / "events.jsonl"
    kas_log = run_dir / "kas.log"

    fail = _last_event_matching(events_path, "step_fail")
    failing_step = fail.get("step") if fail else None
    fail_reason = fail.get("reason") if fail else None

    kas_log_tail = _tail(kas_log, 60)
    recipe_log = _find_recipe_log(kas_log, workspace)
    recipe_log_tail = _tail(recipe_log, 60) if recipe_log else []
    recipe_errors = _scan_recipe_errors(kas_log)

    suggestions_text = "\n".join(kas_log_tail + recipe_log_tail)
    suggestions = _match_suggestions(suggestions_text)

    # Prepend the recipe-level failures so they land in the same rendered
    # section as suggestions (cli.py iterates `suggestions` under the
    # "suggestions:" heading).  Rendering as `<recipe> do_<task>: <excerpt>`
    # mirrors the format the task spec calls out.
    if recipe_errors:
        header = "recipe-level failures (from kas.log, unique recipes):"
        lines = [f"{e.recipe} do_{e.task}: {e.excerpt}" for e in recipe_errors]
        suggestions = [header, *lines, *suggestions]

    override_line = _bitbake_override_summary(events_path)
    if override_line:
        suggestions = [override_line, *suggestions]

    return TriageReport(
        run_dir=run_dir,
        failing_step=failing_step,
        fail_reason=fail_reason,
        kas_log_tail=kas_log_tail,
        recipe_log=recipe_log,
        recipe_log_tail=recipe_log_tail,
        suggestions=suggestions,
        recipe_errors=recipe_errors,
    )


def find_runs(workspace: Path) -> list[Path]:
    """Return run directories from both BSPs, most-recent first.

    Walks ``<workspace>/nxp/build/runs/`` and ``<workspace>/ti/build/runs/``
    so callers do not need to pre-dispatch a BSP family. Used by
    ``bspctl triage`` when no run id is supplied.
    """
    out: list[Path] = []
    for family in ("nxp", "ti"):
        runs_dir = workspace / family / "build" / "runs"
        if runs_dir.is_dir():
            out.extend(p for p in runs_dir.iterdir() if p.is_dir())
    return sorted(out, key=lambda p: p.name, reverse=True)
