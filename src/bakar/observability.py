"""Structured logging and run-state tracking.

Each `bakar build` invocation creates a run directory under build/runs/<ts>/
containing:

    events.jsonl    one JSON object per step start/end/error (machine-readable)
    console.log     the same content in human-readable lines
    env.txt         snapshot of BAKAR_*, KAS_*, NPROC, DL_DIR, SSTATE_DIR at start
    kas.log         stdout+stderr from kas-container build

This layout lets `bakar triage` post-mortem a failure without re-running
the build: it grep's events.jsonl for the failing step and surfaces the
matching kas.log excerpt plus the bitbake recipe log that triggered it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.logging import RichHandler

from bakar import eventlog

if TYPE_CHECKING:
    from pathlib import Path

console = Console(stderr=True)


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RunLogger:
    """Writes both structured JSONL and a human log for one `bakar` run.

    Use as a context manager:

        with RunLogger(runs_dir) as log:
            log.step_start("repo_sync", machine=cfg.machine)
            ...
            log.step_ok("repo_sync", repos_count=24)
    """

    runs_dir: Path
    run_id: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d-%H%M%S"))
    # Monotonic stamp at construction (start of the `bakar` run, before doctor),
    # so the build UI's global timer can count from the command invocation.
    start_monotonic: float = field(default_factory=time.monotonic)
    _events_fh: Any = None
    # Optional per-instance render console. When None, ``console`` falls back to the
    # shared module-level Console. Plain mode supplies a no-color one so all run output
    # (status lines, out-of-Live summary/hint lines, layer tables) is ANSI-free.
    render_console: Console | None = None
    _logger: logging.Logger = field(init=False, repr=False)

    @property
    def run_dir(self) -> Path:
        return self.runs_dir / self.run_id

    @property
    def events_path(self) -> Path:
        return self.run_dir / "events.jsonl"

    @property
    def console_path(self) -> Path:
        return self.run_dir / "console.log"

    @property
    def kas_log_path(self) -> Path:
        return self.run_dir / "kas.log"

    @property
    def env_snapshot_path(self) -> Path:
        return self.run_dir / "env.txt"

    @property
    def error_report_path(self) -> Path:
        return self.run_dir / "error-report.json"

    @property
    def eventlog_path(self) -> Path:
        return self.run_dir / "bitbake_eventlog.json"

    @property
    def bitbake_events_path(self) -> Path:
        return self.run_dir / "bitbake-events.json"

    @property
    def sccache_stats_path(self) -> Path:
        return self.run_dir / "sccache-stats.json"

    @property
    def console(self) -> Console:
        """The Rich console the log handler writes to.

        A ``Live`` display should be created on this same console so its
        in-place renders coordinate with log output (clear, print above,
        re-render) instead of colliding on the same line.

        Returns the per-instance ``render_console`` when one was supplied (plain mode
        passes a no-color console), else the shared module-level console.
        """
        return self.render_console if self.render_console is not None else console

    def __enter__(self) -> RunLogger:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._events_fh = self.events_path.open("w")
        self._logger = logging.getLogger(f"bakar.run.{self.run_id}")
        self._logger.setLevel(logging.DEBUG)
        self._logger.handlers.clear()
        rich_h = RichHandler(console=self.console, show_time=False, show_path=False, markup=True)
        rich_h.setLevel(logging.INFO)
        file_h = logging.FileHandler(self.console_path)
        file_h.setLevel(logging.DEBUG)
        file_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self._logger.addHandler(rich_h)
        self._logger.addHandler(file_h)
        self._snapshot_env()
        self._emit("run_start", run_id=self.run_id)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            self._emit("run_error", error=str(exc), error_type=exc_type.__name__)
        else:
            self._emit("run_end")
        if self._events_fh is not None:
            self._events_fh.close()
        for h in list(self._logger.handlers):
            h.close()
            self._logger.removeHandler(h)

    def _emit(self, event: str, **fields: Any) -> None:
        rec = {"ts": _utc_now_iso(), "event": event, **fields}
        self._events_fh.write(json.dumps(rec, default=str) + "\n")
        self._events_fh.flush()

    def _snapshot_env(self) -> None:
        keep_prefixes = ("BAKAR_", "KAS_", "BB_", "DL_", "SSTATE_", "NPROC", "MACHINE", "DISTRO")
        lines = [f"{k}={v}" for k, v in sorted(os.environ.items()) if k.startswith(keep_prefixes)]
        self.env_snapshot_path.write_text("\n".join(lines) + "\n")

    # Public API -----------------------------------------------------------

    def info(self, msg: str, **fields: Any) -> None:
        self._logger.info(msg)
        self._emit("info", message=msg, **fields)

    def warn(self, msg: str, **fields: Any) -> None:
        self._logger.warning(msg)
        self._emit("warn", message=msg, **fields)

    def error(self, msg: str, **fields: Any) -> None:
        self._logger.error(msg)
        self._emit("error", message=msg, **fields)

    def _console_header(self, step: str) -> None:
        """Append a timestamped phase-boundary header to console.log only.

        Writes directly to the file to avoid routing through the logging
        handlers, which would also emit to the Rich/stderr console.
        """
        ts = _utc_now_iso()
        line = f"── [{ts}] {step} ──\n"
        with self.console_path.open("a") as fh:
            fh.write(line)

    def step_start(self, step: str, **fields: Any) -> None:
        self._console_header(step)
        self._logger.info(f"[cyan]→[/] {step}")
        self._emit("step_start", step=step, **fields)

    def step_ok(self, step: str, **fields: Any) -> None:
        self._console_header(step)
        self._logger.info(f"[green]✓[/] {step}")
        self._emit("step_ok", step=step, **fields)

    def step_skip(self, step: str, reason: str, **fields: Any) -> None:
        self._logger.info(f"[yellow]↷[/] {step} ({reason})")
        self._emit("step_skip", step=step, reason=reason, **fields)

    def step_fail(self, step: str, reason: str, **fields: Any) -> None:
        self._console_header(step)
        self._logger.error(f"[red]✗[/] {step}: {reason}")
        self._emit("step_fail", step=step, reason=reason, **fields)

    def persist_bitbake_events(self) -> None:
        """Normalize the raw bitbake event log into ``bitbake-events.json``.

        Best-effort: when the raw log is absent or empty, nothing is written
        and no event is emitted. Never raises.
        """
        raw = self.eventlog_path
        # Best-effort: a missing/rotated raw log, a decode/parse error in a
        # corrupt log, or a write failure must not crash an otherwise-completed
        # build at the persistence step. The is_file()/stat() preflight lives
        # inside the try so a TOCTOU race (the log removed or rotated between
        # the check and the stat) is a no-op rather than a crash.
        try:
            if not raw.is_file() or raw.stat().st_size == 0:
                return
            artifact = eventlog.normalize(raw)
            self.bitbake_events_path.write_text(json.dumps(artifact, default=str))
        except (OSError, ValueError) as exc:
            self.warn(f"failed to persist bitbake-events.json: {exc}")
            return
        self.step_ok("bitbake_events", path=str(self.bitbake_events_path))

    def persist_sccache_stats(self, doc: dict[str, Any] | None) -> None:
        """Persist the build-end sccache daemon stats as ``sccache-stats.json``.

        Serializes the ``daemon_doc`` dict (carrying the per-language
        ``hits_by_lang``/``misses_by_lang`` breakdown and per-node
        distribution) so ``bakar report`` can present it post-build without a
        live daemon. Best-effort: a ``None`` doc (no running daemon) or a
        write failure is a no-op. Never raises.
        """
        if not doc:
            return
        try:
            self.sccache_stats_path.write_text(json.dumps(doc, default=str))
        except (OSError, ValueError) as exc:
            self.warn(f"failed to persist sccache-stats.json: {exc}")
            return
        self.step_ok("sccache_stats", path=str(self.sccache_stats_path))

    def persist_task_timings(self, timings_path: Path | None = None) -> None:
        """Accumulate this run's task durations into the global baseline store.

        Best-effort: reads the normalized ``bitbake-events.json`` artifact
        written by :meth:`persist_bitbake_events` and folds its per-task
        durations into the shared timings file. The ``task_timings`` import is
        function-local to keep the dependency off the module import path and
        avoid any cycle. Never raises - a failure here must not break an
        otherwise-completed build.
        """
        from bakar import task_timings

        try:
            task_timings.update_from_events(
                self.bitbake_events_path,
                timings_path or task_timings.DEFAULT_TIMINGS_PATH,
            )
        except (OSError, ValueError) as exc:  # best-effort; must never break a build
            self.warn(f"failed to persist task timings: {exc}")
