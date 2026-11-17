"""Behaviour tests for the bounded mount-probe runner ``mounts._run_bounded``.

The runner polls its children with a doubling backoff (1 ms up to 50 ms) that
never sleeps past the shared absolute deadline. These tests pin the guarantees
that matter to callers: a child that never exits is killed and reported as
``(None, "", "")`` by the deadline, a child that exits within milliseconds is
seen within milliseconds, and several children share one deadline.

Every check is a function of the runner, so the same checks run against a
stand-in with the deadline exit removed; at least one must fail there.
"""

from __future__ import annotations

import inspect
import statistics
import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bakar import mounts

Runner = Callable[[dict[Path, list[str]], float], dict[Path, tuple[int | None, str, str]]]

DEADLINE_S = 0.2
KILL_MARGIN_S = 0.5


def _pid_is_gone(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError, ProcessLookupError:
        return True
    # state is the field after the parenthesised command name; Z = zombie.
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


def check_never_exiting_child_is_killed_at_deadline(runner: Runner, tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    script = f"echo $$ > {pidfile}; exec sleep 0.45"
    path = Path("/never-exits")
    started = time.monotonic()
    result = runner({path: ["sh", "-c", script]}, started + DEADLINE_S)
    elapsed = time.monotonic() - started

    assert result == {path: (None, "", "")}
    assert elapsed <= DEADLINE_S + KILL_MARGIN_S
    assert _pid_is_gone(int(pidfile.read_text()))


def check_quick_child_is_seen_promptly(runner: Runner, tmp_path: Path) -> None:
    path = Path("/quick")
    durations: list[float] = []
    for _ in range(5):
        started = time.monotonic()
        result = runner({path: ["true"]}, started + 5.0)
        durations.append(time.monotonic() - started)
        assert result == {path: (0, "", "")}

    assert statistics.median(durations) < 0.025, durations
    assert max(durations) < 0.045, durations


def check_children_share_one_deadline(runner: Runner, tmp_path: Path) -> None:
    quick = Path("/quick")
    slow_a = Path("/slow-a")
    slow_b = Path("/slow-b")
    specs = {
        quick: ["sh", "-c", "echo out; echo err >&2"],
        slow_a: ["sleep", "0.45"],
        slow_b: ["sleep", "0.45"],
    }
    started = time.monotonic()
    result = runner(specs, started + DEADLINE_S)
    elapsed = time.monotonic() - started

    assert result[quick] == (0, "out\n", "err\n")
    assert result[slow_a] == (None, "", "")
    assert result[slow_b] == (None, "", "")
    # One shared budget: two stuck children do not cost two deadlines.
    assert elapsed <= DEADLINE_S + KILL_MARGIN_S


CHECKS = [
    check_never_exiting_child_is_killed_at_deadline,
    check_quick_child_is_seen_promptly,
    check_children_share_one_deadline,
]


def _runner_without_deadline_exit() -> Runner:
    """A copy of the real helper that never leaves its poll loop on the deadline."""
    source = textwrap.dedent(inspect.getsource(mounts._run_bounded))
    mutated = source.replace("if not pending or remaining <= 0:", "if not pending:").replace(
        "time.sleep(min(delay, remaining))", "time.sleep(delay)"
    )
    assert mutated.count("if not pending:") == 1 and "time.sleep(delay)" in mutated, "mutation did not apply"
    namespace: dict[str, Any] = dict(vars(mounts))
    # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
    exec(compile(mutated, "<run_bounded without deadline exit>", "exec"), namespace)
    return namespace["_run_bounded"]


@pytest.mark.parametrize("check", CHECKS, ids=lambda fn: fn.__name__)
def test_run_bounded_guarantees(check: Callable[[Runner, Path], None], tmp_path: Path) -> None:
    check(mounts._run_bounded, tmp_path)


def test_checks_fail_when_the_deadline_exit_is_removed(tmp_path: Path) -> None:
    runner = _runner_without_deadline_exit()
    failed: list[str] = []
    for index, check in enumerate(CHECKS):
        try:
            check(runner, tmp_path / str(index))
        except AssertionError:
            failed.append(check.__name__)
    assert failed, "no check noticed the missing deadline exit"


@pytest.fixture(autouse=True)
def _per_check_dirs(tmp_path: Path) -> None:
    for index in range(len(CHECKS)):
        (tmp_path / str(index)).mkdir()
