"""Post-mortem triage for a failed build run.

Reads a run directory produced by :mod:`bakar.observability`, locates
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

from bakar.fork_race_signatures import (
    FORK_RACE_SIGNATURES,
    FORK_RACE_SUGGESTION,
)

_ERROR_REPORT_FILENAME = "error-report.json"


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
    Returns ``None`` when no event is present (older bakar runs, or the
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


def _translate_container_path(container_path: str, workspace: Path) -> str:
    """Rewrite a leading ``/work/`` container prefix to a host path under
    ``workspace``.

    Only the first occurrence of the prefix is replaced. A path that does
    not start with ``/work/`` is returned unchanged - the ``/work`` bind
    mount is the only container-to-host mapping bakar controls, so a path
    outside it is already a host path and must pass through verbatim.
    """
    if not container_path.startswith("/work/"):
        return container_path
    return container_path.replace("/work/", str(workspace) + "/", 1)


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
        host_path = Path(_translate_container_path(container_path, workspace))
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
        # bakar.fork_race_signatures so the empirical stress-test
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
    (
        re.compile(
            r"Killed signal terminated program cc1plus"
            r"|cc1plus: out of memory"
            r"|c\+\+: fatal error: Killed signal"
        ),
        "Compiler OOM-kill. Lower BB_NUMBER_THREADS and/or PARALLEL_MAKE in the kas YAML's local_conf_header "
        '(e.g. BB_NUMBER_THREADS = "4", PARALLEL_MAKE = "-j 4") to reduce peak memory pressure.',
    ),
    (
        re.compile(r"HTTP Error 429|API rate limit exceeded"),
        "GitHub rate-limit hit. Wait a few minutes and retry, or authenticate by setting "
        "BB_GIT_SHALLOW_FETCH_EXTRA_OPTIONS with a personal access token to reduce unauthenticated API calls.",
    ),
    (
        re.compile(r"Name or service not known|Temporary failure in name resolution|Connection timed out"),
        "Network failure. 'Name or service not known' / 'Temporary failure in name resolution' indicates "
        "a DNS problem - check that the container has a working resolver. 'Connection timed out' indicates "
        "a routing or firewall issue. In both cases verify your network access and consider setting a "
        "PREMIRROR to serve sources locally.",
    ),
    (
        re.compile(r"Connection refused"),
        "PREMIRROR connection refused. The configured PREMIRROR is unreachable - check that the mirror "
        "server is running and the URL in PREMIRROR (local_conf_header) is correct.",
    ),
]


def _match_suggestions(text: str) -> list[str]:
    hits: list[str] = []
    for pattern, suggestion in _SUGGESTIONS:
        if pattern.search(text):
            hits.append(suggestion)
    return hits


def write_error_report(run_dir: Path, cfg, exit_code: int) -> None:
    """Write a structured failure artifact to ``run_dir/error-report.json``.

    Captures the kas.log tail, recipe-level errors, and matched suggestions
    at build-failure time so :func:`analyse` can reconstruct a triage report
    without re-parsing logs on every invocation.

    The write is best-effort: an :exc:`OSError` is silently swallowed so
    the original build failure exit code is never masked.
    """
    kas_log = run_dir / "kas.log"
    tail_lines = _tail(kas_log, 60)
    tail_text = "\n".join(tail_lines)
    recipe_errors = _scan_recipe_errors(kas_log)
    suggestions = _match_suggestions(tail_text)

    report: dict = {
        "step": "kas_build",
        "machine": cfg.machine,
        "distro": cfg.distro,
        "bsp_family": cfg.bsp_family,
        "exit_code": exit_code,
        "kas_log_tail": tail_lines,
        "recipe_errors": [{"recipe": e.recipe, "task": e.task, "excerpt": e.excerpt} for e in recipe_errors],
        "suggestions": suggestions,
    }

    try:
        (run_dir / _ERROR_REPORT_FILENAME).write_text(json.dumps(report, indent=2))
    except OSError:
        return


def _prepend_recipe_headers(suggestions: list[str], recipe_errors: list[RecipeError]) -> list[str]:
    if not recipe_errors:
        return suggestions
    header = "recipe-level failures (unique recipes):"
    lines = [f"{e.recipe} do_{e.task}: {e.excerpt}" for e in recipe_errors]
    return [header, *lines, *suggestions]


def analyse(run_dir: Path, workspace: Path) -> TriageReport:
    events_path = run_dir / "events.jsonl"

    # Fast path: read the pre-structured failure artifact written by run_build.
    # Avoids re-scanning kas.log on every triage invocation. Falls through to
    # the live-parse path on any parse error or missing file so old run dirs
    # without an error-report.json continue to work unchanged.
    error_report_path = run_dir / _ERROR_REPORT_FILENAME
    if error_report_path.exists():
        try:
            data = json.loads(error_report_path.read_text())
            failing_step: str | None = data["step"]
            kas_log_tail: list[str] = list(data["kas_log_tail"])
            recipe_errors: list[RecipeError] = [
                RecipeError(recipe=e["recipe"], task=e["task"], excerpt=e["excerpt"]) for e in data["recipe_errors"]
            ]
            suggestions: list[str] = list(data["suggestions"])
        except json.JSONDecodeError, KeyError, TypeError:
            pass
        else:
            # Supplement: recipe log data and fail_reason are not stored in
            # error-report.json; derive them the same way the live-parse path
            # does so the two paths produce equivalent output.
            kas_log = run_dir / "kas.log"
            fail = _last_event_matching(events_path, "step_fail")
            fail_reason: str | None = fail.get("reason") if fail else None
            recipe_log = _find_recipe_log(kas_log, workspace)
            recipe_log_tail = _tail(recipe_log, 60) if recipe_log else []
            # Recompute suggestions over combined text (table order) so the fast
            # path produces the same ordering as the live-parse path.
            suggestions = _match_suggestions("\n".join(kas_log_tail + recipe_log_tail))
            suggestions = _prepend_recipe_headers(suggestions, recipe_errors)
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

    # Live-parse path: used when error-report.json is absent or unreadable
    # (backward compatible with run dirs produced before this feature).
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
    suggestions = _prepend_recipe_headers(suggestions, recipe_errors)

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
    """Return run directories across all BSP families, most-recent first.

    Discovers run directories from three locations:

    1. ``<workspace>/nxp/build/runs/`` and ``<workspace>/ti/build/runs/``
       (legacy named-family paths, checked explicitly so they always participate
       even if the glob below would already pick them up).
    2. ``<workspace>/build/runs/`` - workspace-root builds (BYO/generic and
       bbsetup families whose build tree sits directly in the workspace).
    3. ``<workspace>/*/build/runs/`` - one-level-deep subdirectories other than
       the named families above (bounded: at most 2 glob depth levels, never
       rglob).

    Results are deduplicated by resolved path so a directory matched by both
    an explicit check and the glob does not appear twice.  The final list is
    sorted by run directory name (``YYYYMMDD-HHMMSS``) in descending order so
    the most recent run is first.
    """
    seen: set[Path] = set()
    out: list[Path] = []

    def _collect(runs_dir: Path) -> None:
        if not runs_dir.is_dir():
            return
        for p in runs_dir.iterdir():
            if not p.is_dir():
                continue
            key = p.resolve()
            if key in seen:
                continue
            seen.add(key)
            out.append(p)

    # Named-family paths (always checked first).
    for family in ("nxp", "ti"):
        _collect(workspace / family / "build" / "runs")

    # Workspace-root build tree (BYO/generic, bbsetup).
    _collect(workspace / "build" / "runs")

    # One-level-deep subdirs not already covered above.
    for runs_dir in workspace.glob("*/build/runs"):
        _collect(runs_dir)

    return sorted(out, key=lambda p: p.name, reverse=True)
