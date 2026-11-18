"""Behaviour tests for the bounded mount-probe runner ``mounts._run_bounded``.

The runner polls its children with a doubling backoff (1 ms up to 50 ms) that
never sleeps past the shared absolute deadline. These tests pin the guarantees
that matter to callers: a child that never exits is killed and reported as
``(None, "", "")`` by the deadline, a child that exits within milliseconds is
seen within milliseconds, several children share one deadline, and the runner
never waits on the pipes of a child it killed.

Every check is a function of the runner, so the same checks run against
stand-ins with one guarantee removed at a time (no deadline exit, no clamp, no
kill, pipes read after the kill); each stand-in must fail at least one check.
"""

from __future__ import annotations

import inspect
import shlex
import textwrap
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from bakar import mounts
from tests.test_pin_probe import _gone as _pid_is_gone

Runner = Callable[[dict[Path, list[str]], float], dict[Path, tuple[int | None, str, str]]]

DEADLINE_S = 0.2
KILL_MARGIN_S = 0.5
BOUND_S = DEADLINE_S + KILL_MARGIN_S
# Longer than BOUND_S, so a child that is not killed is still alive when the runner returns.
STUCK_S = 1.5
STUCK_COUNT = 6
# Guards the shared-deadline check's own sensitivity: a runner giving each stuck child its own deadline needs
# STUCK_COUNT * DEADLINE_S = 1.2 s, which must stay well above BOUND_S or that check stops telling the two apart.
assert STUCK_COUNT * DEADLINE_S - 0.2 > BOUND_S


def check_clamp_never_sleeps_past_the_deadline(runner: Runner, tmp_path: Path) -> None:
    """Deterministic: a virtual clock replaces ``time`` inside the runner's module."""

    class VirtualTime:
        def __init__(self) -> None:
            self.now = 1000.0
            self.sleeps: list[float] = []

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            assert self.now < deadline, f"slept at {self.now} with the deadline {deadline} already passed"
            assert self.now + seconds <= deadline + 1e-9, (
                f"sleep({seconds}) at {self.now} runs past the deadline {deadline}"
            )
            self.sleeps.append(seconds)
            self.now += seconds

    clock = VirtualTime()
    deadline = clock.now + DEADLINE_S
    path = Path("/stuck")
    namespace = runner.__globals__
    real_time = namespace["time"]
    namespace["time"] = clock
    try:
        result = runner({path: ["sleep", str(STUCK_S)]}, deadline)
    finally:
        namespace["time"] = real_time

    assert result == {path: (None, "", "")}
    assert clock.sleeps, "the runner never slept between polls"
    # The backoff starts at a millisecond and never exceeds the old fixed 50 ms poll, so a child that exits
    # within milliseconds is seen within milliseconds. A runner whose first poll is 20 to 35 ms late fails here
    # without any wall-clock bound.
    assert clock.sleeps[0] <= 0.005, clock.sleeps
    assert max(clock.sleeps) <= 0.05 + 1e-9, clock.sleeps
    assert clock.now == pytest.approx(deadline)


def check_never_exiting_child_is_killed_at_deadline(runner: Runner, tmp_path: Path) -> None:
    workdir = tmp_path / "never-exits"
    workdir.mkdir()
    pidfile = workdir / "pid"
    script = f"echo $$ > {shlex.quote(str(pidfile))}; exec sleep {STUCK_S}"
    path = Path("/never-exits")
    started = time.monotonic()
    result = runner({path: ["sh", "-c", script]}, started + DEADLINE_S)
    elapsed = time.monotonic() - started

    assert result == {path: (None, "", "")}
    assert elapsed <= BOUND_S
    assert _pid_is_gone(int(pidfile.read_text()))


def check_grandchild_holding_the_pipes_does_not_block_the_runner(runner: Runner, tmp_path: Path) -> None:
    workdir = tmp_path / "grandchild"
    workdir.mkdir()
    pidfile = workdir / "pid"
    # The backgrounded sleep inherits both pipes and outlives the bound, so reading a killed sh's pipes blocks.
    script = f"echo $$ > {shlex.quote(str(pidfile))}; sleep 1 & wait"
    path = Path("/grandchild")
    started = time.monotonic()
    result = runner({path: ["sh", "-c", script]}, started + DEADLINE_S)
    elapsed = time.monotonic() - started

    assert result == {path: (None, "", "")}
    assert elapsed <= BOUND_S, elapsed
    assert _pid_is_gone(int(pidfile.read_text()))


def check_quick_child_is_seen_promptly(runner: Runner, tmp_path: Path) -> None:
    path = Path("/quick")
    durations: list[float] = []
    for _ in range(5):
        started = time.monotonic()
        result = runner({path: ["true"]}, started + 5.0)
        durations.append(time.monotonic() - started)
        assert result == {path: (0, "", "")}

    # The old fixed 50 ms poll could never see a child before 50 ms. A scheduling stall can slow any single run,
    # so the fastest of five is the stable signal.
    assert min(durations) < 0.04, durations


def check_children_share_one_deadline(runner: Runner, tmp_path: Path) -> None:
    quick = Path("/quick")
    stuck = [Path(f"/stuck-{index}") for index in range(STUCK_COUNT)]
    specs = {quick: ["sh", "-c", "echo out; echo err >&2"]}
    specs.update({path: ["sleep", str(STUCK_S)] for path in stuck})
    started = time.monotonic()
    result = runner(specs, started + DEADLINE_S)
    elapsed = time.monotonic() - started

    assert result[quick] == (0, "out\n", "err\n")
    for path in stuck:
        assert result[path] == (None, "", "")
    # One shared budget: six stuck children cost one deadline, not six (see the module-level guard on the constants).
    assert elapsed <= BOUND_S, elapsed


# The deterministic clamp check goes first so a stand-in that never leaves its poll loop fails before it can run
# for a stuck child's whole life.
CHECKS = [
    check_clamp_never_sleeps_past_the_deadline,
    check_never_exiting_child_is_killed_at_deadline,
    check_grandchild_holding_the_pipes_does_not_block_the_runner,
    check_quick_child_is_seen_promptly,
    check_children_share_one_deadline,
]


def _mutated_runner(name: str, *edits: tuple[str, str]) -> Runner:
    """A copy of the real helper with each ``(old, new)`` source edit applied exactly once."""
    mutated = textwrap.dedent(inspect.getsource(mounts._run_bounded))
    for old, new in edits:
        assert mutated.count(old) == 1, f"mutation anchor not unique for {name}: {old!r} x{mutated.count(old)}"
        mutated = mutated.replace(old, new)
    namespace: dict[str, Any] = dict(vars(mounts))
    # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
    exec(compile(mutated, f"<run_bounded {name}>", "exec"), namespace)
    return namespace["_run_bounded"]


MUTANTS: dict[str, tuple[tuple[str, str], ...]] = {
    "no deadline exit": (
        ("if not pending or remaining <= 0:", "if not pending:"),
        ("time.sleep(min(delay, remaining))", "time.sleep(delay)"),
    ),
    "no clamp": (("time.sleep(min(delay, remaining))", "time.sleep(delay)"),),
    "slow first poll": (("delay = _POLL_DELAY_MIN_S", "delay = 0.03"),),
    "no kill": (("proc.kill()", "pass"),),
    "pipes read after the kill": (
        ('results[path] = (None, "", "")', 'proc.stdout.read(); results[path] = (None, "", "")'),
    ),
}


@pytest.mark.parametrize("check", CHECKS, ids=lambda fn: fn.__name__)
def test_run_bounded_guarantees(check: Callable[[Runner, Path], None], tmp_path: Path) -> None:
    check(mounts._run_bounded, tmp_path)


def _first_failing_check(name: str, tmp_path: Path) -> str | None:
    runner = _mutated_runner(name, *MUTANTS[name])
    workdir = tmp_path / name.replace(" ", "-")
    workdir.mkdir()
    for check in CHECKS:
        try:
            check(runner, workdir)
        except AssertionError:
            return check.__name__
    return None


def test_checks_fail_when_a_guarantee_is_removed(tmp_path: Path) -> None:
    # The stand-ins run side by side: the slowest one (pipes read after the kill) waits out a grandchild's
    # second, and running them one after another would triple that.
    with ThreadPoolExecutor(max_workers=len(MUTANTS)) as pool:
        futures = {name: pool.submit(_first_failing_check, name, tmp_path) for name in MUTANTS}
        caught = {name: future.result() for name, future in futures.items()}
    missed = [name for name, check in caught.items() if check is None]
    assert not missed, f"no check noticed: {missed}; caught by {caught}"
