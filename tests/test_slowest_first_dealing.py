"""Tests for the chunk-aware slowest-first dealing in ``tests/conftest.py``.

The pure placement function is exercised without xdist; some tests feed its order to
the real ``WorkStealingScheduling`` to check where the listed tests land in each
worker's initial queue.
"""

from __future__ import annotations

import random
import warnings
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pluggy
import pytest
from xdist.scheduler.worksteal import WorkStealingScheduling

import tests.conftest as harness
from tests.conftest import (
    SlowestFirstDealing,
    _chunk_sizes,
    selection_is_narrowed,
    slowest_first_order,
    stale_slowest_ids,
)
from tests.regen_slowest_tests import rank_slowest, render_list

if TYPE_CHECKING:
    from pathlib import Path

TOTAL = 8363
LISTED = 64


def _synthetic(total: int = TOTAL, listed: int = LISTED) -> tuple[list[str], list[str]]:
    """A shuffled order of ``total`` ids, ``listed`` of them in the slow list (rank order)."""
    ids = [f"tests/test_x.py::t{i}" for i in range(total)]
    slowest = [f"tests/test_x.py::t{i}" for i in range(listed)]
    random.Random(1).shuffle(ids)
    return ids, slowest


def _initial_queues(order: list[str], workers: int) -> list[list[str]]:
    """The tests each node is sent first by the real worksteal scheduler."""

    class Node:
        def __init__(self, i: int) -> None:
            self.shutting_down = False
            self.gateway = SimpleNamespace(id=f"gw{i}")
            self.got: list[int] = []

        def send_runtest_some(self, idx: list[int]) -> None:
            self.got.extend(idx)

    cfg = SimpleNamespace(getvalue=lambda name: [f"{workers}*popen"])
    sched = WorkStealingScheduling(cfg, log=None)
    nodes = [Node(i) for i in range(workers)]
    for node in nodes:
        sched.add_node(node)
    for node in nodes:
        sched.add_node_collection(node, order)
    sched.schedule()
    return [[order[i] for i in node.got] for node in nodes]


def _chunk_starts(total: int, workers: int) -> list[int]:
    starts = []
    pos = 0
    for size in _chunk_sizes(total, workers):
        starts.append(pos)
        pos += size
    return starts


@pytest.mark.parametrize("workers", [1, 4, 16])
def test_order_is_a_permutation(workers: int) -> None:
    ids, slowest = _synthetic()
    order = slowest_first_order(ids, slowest, workers)
    assert sorted(order) == list(range(len(ids)))


@pytest.mark.parametrize("workers", [1, 4, 16])
def test_order_is_deterministic(workers: int) -> None:
    ids, slowest = _synthetic()
    assert slowest_first_order(ids, slowest, workers) == slowest_first_order(list(ids), list(slowest), workers)


@pytest.mark.parametrize("workers", [1, 4, 16])
def test_unlisted_tests_keep_their_relative_order(workers: int) -> None:
    ids, slowest = _synthetic()
    listed = set(slowest)
    order = slowest_first_order(ids, slowest, workers)
    assert [ids[i] for i in order if ids[i] not in listed] == [i for i in ids if i not in listed]


@pytest.mark.parametrize("workers", [1, 4, 16])
def test_each_chunk_gets_an_equal_round_robin_share(workers: int) -> None:
    ids, slowest = _synthetic()
    order = [ids[i] for i in slowest_first_order(ids, slowest, workers)]
    start = 0
    for chunk, size in enumerate(_chunk_sizes(len(order), workers)):
        head = order[start : start + size][: LISTED // workers]
        # rank k lands at chunk k % workers, offset k // workers
        assert head == slowest[chunk::workers]
        start += size


@pytest.mark.parametrize("workers", [4, 16])
def test_real_worksteal_deals_every_worker_the_same_number_of_listed_tests(workers: int) -> None:
    ids, slowest = _synthetic()
    order = [ids[i] for i in slowest_first_order(ids, slowest, workers)]
    listed = set(slowest)
    counts = [sum(nodeid in listed for nodeid in queue) for queue in _initial_queues(order, workers)]
    assert counts == [LISTED // workers] * workers


def test_chunk_sizes_match_the_worksteal_deal() -> None:
    ids, _ = _synthetic()
    for workers in (4, 16):
        assert _chunk_sizes(len(ids), workers) == [len(q) for q in _initial_queues(ids, workers)]


def test_listed_ids_missing_from_the_order_are_skipped() -> None:
    ids, slowest = _synthetic(total=500, listed=8)
    ids = [i for i in ids if i not in {slowest[0], slowest[3]}]
    order = slowest_first_order(ids, slowest, 4)
    assert sorted(order) == list(range(len(ids)))
    present = [i for i in slowest if i in set(ids)]
    heads = [ids[order[start]] for start in _chunk_starts(len(ids), 4)]
    # ranks among the present tests shift up, so the round-robin stays level
    assert heads == present[:4]


def test_empty_list_leaves_the_order_unchanged() -> None:
    ids, _ = _synthetic(total=100)
    assert slowest_first_order(ids, [], 16) == list(range(100))


def test_no_collected_listed_ids_leaves_the_order_unchanged() -> None:
    ids, _ = _synthetic(total=100)
    assert slowest_first_order(ids, ["tests/gone.py::t"], 4) == list(range(100))


def test_tiny_run_still_returns_a_permutation() -> None:
    ids, slowest = _synthetic(total=5, listed=5)
    assert sorted(slowest_first_order(ids, slowest, 16)) == list(range(5))


def test_stale_ids_report_removed_tests_and_modules(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("")
    (tmp_path / "tests" / "test_b.py").write_text("")
    collected = ["tests/test_a.py::t1"]
    slowest = [
        "tests/test_a.py::t1",  # collected
        "tests/test_a.py::renamed",  # module collected, id gone
        "tests/test_b.py::t1",  # module on disk but not collected: a partial run
        "tests/test_deleted.py::t1",  # module gone from disk
    ]
    assert stale_slowest_ids(collected, slowest, tmp_path) == [
        "tests/test_a.py::renamed",
        "tests/test_deleted.py::t1",
    ]


def _items(ids: list[str]) -> list[SimpleNamespace]:
    return [SimpleNamespace(nodeid=i) for i in ids]


def _config(root: Path, *, keyword: str = "", markexpr: str = "", deselect: list[str] | None = None, args=()):
    """The few attributes of ``pytest.Config`` the dealing hook reads."""
    option = SimpleNamespace(keyword=keyword, markexpr=markexpr, deselect=deselect)
    return SimpleNamespace(rootpath=root, option=option, args=list(args))


def _listed_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[SimpleNamespace]:
    listed = tmp_path / "slowest_tests.txt"
    listed.write_text("# header\ntests/a.py::t0\ntests/a.py::gone\n")
    monkeypatch.setattr(harness, "_SLOWEST_TESTS_FILE", listed)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "2")
    return _items(["tests/a.py::t1", "tests/a.py::t0", "tests/a.py::t2"])


def test_hook_warns_about_stale_ids_without_failing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    items = _listed_run(monkeypatch, tmp_path)
    with pytest.warns(pytest.PytestWarning, match="tests/a.py::gone"):
        SlowestFirstDealing().pytest_collection_modifyitems(_config(tmp_path), items)  # type: ignore[arg-type]
    assert items[0].nodeid == "tests/a.py::t0"
    assert sorted(it.nodeid for it in items) == ["tests/a.py::t0", "tests/a.py::t1", "tests/a.py::t2"]


@pytest.mark.parametrize(
    "narrowing",
    [{"keyword": "clamp"}, {"markexpr": "unit"}, {"deselect": ["tests/a.py::gone"]}, {"args": ["tests/a.py::t1"]}],
    ids=["-k", "-m", "--deselect", "file::test"],
)
def test_hook_stays_silent_when_the_selection_was_narrowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, narrowing: dict
) -> None:
    items = _listed_run(monkeypatch, tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        SlowestFirstDealing().pytest_collection_modifyitems(_config(tmp_path, **narrowing), items)  # type: ignore[arg-type]
    assert items[0].nodeid == "tests/a.py::t0"


@pytest.mark.parametrize(
    ("keyword", "markexpr", "deselect", "args", "expected"),
    [
        ("", "", None, [], False),
        ("", "", [], ["tests", "tests/test_a.py"], False),
        ("clamp", "", None, [], True),
        ("", "not slow", None, [], True),
        ("", "", ["tests/a.py::t0"], [], True),
        ("", "", None, ["tests/a.py::t0"], True),
    ],
)
def test_selection_is_narrowed(
    keyword: str, markexpr: str, deselect: list[str] | None, args: list[str], expected: bool
):
    assert selection_is_narrowed(keyword, markexpr, deselect, args) is expected


def test_hook_is_a_no_op_outside_xdist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    items = _items(["b", "a"])
    SlowestFirstDealing().pytest_collection_modifyitems(SimpleNamespace(rootpath="."), items)  # type: ignore[arg-type]
    assert [it.nodeid for it in items] == ["b", "a"]


def test_dealing_plugin_is_registered_by_name_and_blocked_by_no_flag() -> None:
    manager = pluggy.PluginManager("pytest")
    harness.pytest_configure(SimpleNamespace(pluginmanager=manager))  # type: ignore[arg-type]
    assert isinstance(manager.get_plugin("bakar_slowest_first"), SlowestFirstDealing)
    assert isinstance(manager.get_plugin("bakar_harness_gc"), harness.HarnessGcPause)

    # ``-p no:NAME`` blocks the name before registration, which is what makes it a switch.
    blocked = pluggy.PluginManager("pytest")
    blocked.set_blocked("bakar_slowest_first")
    harness.pytest_configure(SimpleNamespace(pluginmanager=blocked))  # type: ignore[arg-type]
    assert blocked.get_plugin("bakar_slowest_first") is None
    assert isinstance(blocked.get_plugin("bakar_harness_gc"), harness.HarnessGcPause)


def test_committed_list_has_a_header_naming_the_command_and_source() -> None:
    lines = harness._SLOWEST_TESTS_FILE.read_text().splitlines()
    header = [ln for ln in lines if ln.startswith("#")]
    assert any(ln.startswith("# Command: ") for ln in header)
    assert any(ln.startswith("# Source run: ") for ln in header)
    assert len([ln for ln in lines if ln and not ln.startswith("#")]) == LISTED


def test_generator_sums_phases_ranks_longest_first_and_renders_a_header() -> None:
    log = (
        "0.50s call     tests/a.py::t1\n"
        "0.30s setup    tests/a.py::t1\n"
        "0.60s call     tests/a.py::t2\n"
        "0.10s call     tests/a.py::t3\n"
        "(5 durations < 0.1s hidden.)\n"
    )
    ranked = rank_slowest(log, top=2)
    assert [nodeid for nodeid, _ in ranked] == ["tests/a.py::t1", "tests/a.py::t2"]
    spaced = rank_slowest("0.90s call     tests/a.py::t4[a b]\n0.10s call     tests/a.py::t5\n", top=1)
    assert spaced == [("tests/a.py::t4[a b]", pytest.approx(0.9))]
    text = render_list(ranked, command="cmd", source="src")
    assert "# Command: cmd" in text
    assert "# Source run: src" in text
    assert text.splitlines()[-2:] == ["tests/a.py::t1", "tests/a.py::t2"]
