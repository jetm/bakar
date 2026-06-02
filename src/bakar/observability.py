"""Structured logging and run-state tracking.

Each `bakar build` invocation creates a run directory under build/runs/<ts>/
containing:

    events.jsonl    one JSON object per step start/end/error (machine-readable)
    console.log     the same content in human-readable lines
    env.txt         snapshot of BAKAR_*, KAS_*, NPROC, DL_DIR, SSTATE_DIR at start
    kas.log         stdout+stderr from kas-container build
    time.log        /usr/bin/time -v output (when available)
    du.tsv          periodic `du -sb build/tmp` samples

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
    def time_log_path(self) -> Path:
        return self.run_dir / "time.log"

    @property
    def du_samples_path(self) -> Path:
        return self.run_dir / "du.tsv"

    @property
    def env_snapshot_path(self) -> Path:
        return self.run_dir / "env.txt"

    @property
    def console(self) -> Console:
        """The Rich console the log handler writes to.

        A ``Live`` display should be created on this same console so its
        in-place renders coordinate with log output (clear, print above,
        re-render) instead of colliding on the same line.
        """
        return console

    def __enter__(self) -> RunLogger:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._events_fh = self.events_path.open("w")
        self._logger = logging.getLogger(f"bakar.run.{self.run_id}")
        self._logger.setLevel(logging.DEBUG)
        self._logger.handlers.clear()
        rich_h = RichHandler(console=console, show_time=False, show_path=False, markup=True)
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

    def step_start(self, step: str, **fields: Any) -> None:
        self._logger.info(f"[cyan]→[/] {step}")
        self._emit("step_start", step=step, **fields)

    def step_ok(self, step: str, **fields: Any) -> None:
        self._logger.info(f"[green]✓[/] {step}")
        self._emit("step_ok", step=step, **fields)

    def step_skip(self, step: str, reason: str, **fields: Any) -> None:
        self._logger.info(f"[yellow]↷[/] {step} ({reason})")
        self._emit("step_skip", step=step, reason=reason, **fields)

    def step_fail(self, step: str, reason: str, **fields: Any) -> None:
        self._logger.error(f"[red]✗[/] {step}: {reason}")
        self._emit("step_fail", step=step, reason=reason, **fields)
