"""Tests for bakar.sigdiff_parse.leaf_causes."""

from __future__ import annotations

import pytest

from bakar.sigdiff_parse import Cause, leaf_causes

pytestmark = pytest.mark.unit

AR = (
    "List of dependencies for variable AR changed from 'frozenset({'BUILD_AR'})' "
    "to 'frozenset({'BUILD_AR', 'AR[export]'})'"
)
AR_ITEMS = "changed items: frozenset({'AR[export]'})"
CC = (
    "List of dependencies for variable BUILD_CC changed from 'frozenset({'BUILD_CC_ARCH', 'BUILD_PREFIX'})' "
    "to 'frozenset({'BUILD_CC_ARCH', 'BUILD_CC[export]', 'BUILD_PREFIX'})'"
)
CC_ITEMS = "changed items: frozenset({'BUILD_CC[export]'})"


def test_same_flag_on_many_variables_is_one_cause() -> None:
    lines = [AR, AR_ITEMS, CC, CC_ITEMS]
    causes = leaf_causes(lines, "cmake-native:do_install")
    assert {(c.kind, c.subject) for c in causes} == {("vardeps", "*[export]")}
    assert len(causes) == 1


def test_three_level_chain_attributes_leaf_to_deepest_key() -> None:
    lines = [
        "Hash for task dependency a:do_x changed from aaa to bbb",
        "    Hash for task dependency b:do_y changed from ccc to ddd",
        "        Hash for task dependency c:do_z changed from eee to fff",
        "            Variable FOO value changed:",
        "            - old",
        "            + new",
    ]
    assert leaf_causes(lines, "top:do_t") == [Cause("value", "FOO", "c:do_z")]


def test_unrecoverable() -> None:
    line = "Unable to find matching sigdata for m4-native:do_install with hash " + "a" * 64
    assert leaf_causes([line], "x:do_y") == [Cause("unrecoverable", "m4-native:do_install", "x:do_y")]


def test_chain_line_is_never_a_leaf() -> None:
    assert leaf_causes(["Hash for task dependency a:do_x changed from aaa to bbb"], "top:do_t") == []


def test_file_taskdep_taint_and_entry_task() -> None:
    lines = [
        "Checksum for file /a/b/foo.patch changed from 1 to 2",
        "Dependency on task do_bar was added",
        "Taint (by forced/invalidated task) changed from x to y",
    ]
    kinds = [(c.kind, c.subject, c.task) for c in leaf_causes(lines, "t:do_e")]
    assert kinds == [
        ("file", "foo.patch", "t:do_e"),
        ("taskdep-added", "do_bar", "t:do_e"),
        ("taint", "", "t:do_e"),
    ]


def test_basehash_only_and_unclassified() -> None:
    assert leaf_causes(["basehash changed from a to b"], "t:d") == [Cause("basehash", "", "t:d")]
    assert leaf_causes(["basehash changed from a to b", "Variable X value changed:"], "t:d") == [
        Cause("value", "X", "t:d")
    ]
    assert leaf_causes(["something odd"], "t:d") == [Cause("unclassified", "something odd", "t:d")]


def test_taskvardeps() -> None:
    line = "Task dependencies changed from: ['a', 'X[f]'] to: ['a', 'Y[f]', 'Z']"
    assert leaf_causes([line], "t:d") == [Cause("taskvardeps", "+*[f],+Z,-*[f]", "t:d")]


def test_bounded_lookahead_classifies_like_the_full_tail() -> None:
    lines = [
        AR,
        "",
        "",
        "",
        "",
        AR_ITEMS,
        "Task dependencies changed from:",
        "['a']",
        "to: ['a', 'b']",
        "Variable FOO value changed:",
        "-x",
        "+y",
    ]
    assert leaf_causes(lines, "r:do_x") == [
        Cause("vardeps", "*[export]", "r:do_x"),
        Cause("taskvardeps", "+b", "r:do_x"),
        Cause("value", "FOO", "r:do_x"),
    ]


def test_vardeps_pair_at_end_of_input_and_without_items() -> None:
    assert leaf_causes([AR, AR_ITEMS], "r:do_x") == [Cause("vardeps", "*[export]", "r:do_x")]
    assert leaf_causes([AR], "r:do_x") == [Cause("vardeps", "", "r:do_x")]
    assert leaf_causes([AR, "", ""], "r:do_x") == [Cause("vardeps", "", "r:do_x")]


def test_leaf_causes_memory_is_linear_in_input_size() -> None:
    import tracemalloc

    lines = ["Hash for task dependency a:do_x changed from 1 to 2"]
    for i in range(8000):
        lines.append(f"    List of dependencies for variable V{i} changed from '{{}}' to '{{}}'")
        lines.append(f"    changed items: frozenset({{'X{i}[flag]'}})")
    tracemalloc.start()
    try:
        causes = leaf_causes(lines, "r:do_x")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert causes == [Cause("vardeps", "*[flag]", "a:do_x")]
    assert peak < 20 * 1024 * 1024
