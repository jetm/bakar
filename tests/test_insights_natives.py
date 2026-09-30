"""Tests for bakar.insights_natives: pairing, helper invocation and bucket grouping."""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from bakar import insights_natives as mod
from bakar.native_ledger import MANIFEST_NAME, ledger_root

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

START, END = 1000.0, 2000.0
TASK = "do_populate_sysroot"


def _h(ch: str) -> str:
    return ch * 40


class Env:
    def __init__(self, tmp: Path) -> None:
        self.sstate = tmp / "sstate"
        self.run_dir = tmp / "run"
        self.run_dir.mkdir()
        self.lib = tmp / "bitbake" / "lib"
        (self.lib / "bb").mkdir(parents=True)
        (self.lib / "bb" / "siggen.py").write_text("")
        self.manifest: list[dict[str, str]] = []
        self.rows: list[dict[str, Any]] = []

    def sig(self, recipe: str, hash_: str, mtime: float) -> Path:
        p = ledger_root(self.sstate) / recipe / f"{TASK}.{hash_}.sigdata"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        os.utime(p, (mtime, mtime))
        return p

    def recipe(self, recipe: str, current: str | None = "c", prev: tuple[str, float] | None = None) -> None:
        self.rows.append({"recipe": recipe, "task": TASK, "outcome": "succeeded", "completed": END - 1})
        if current is not None:
            self.manifest.append({"recipe": recipe, "task": TASK, "hash": _h(current)})
            self.sig(recipe, _h(current), END - 5)
        if prev is not None:
            self.sig(recipe, _h(prev[0]), prev[1])

    def report(self, artifact_extra: dict[str, Any] | None = None, **kw: Any) -> mod.NativesReport:
        (self.run_dir / MANIFEST_NAME).write_text(json.dumps({"schema": 1, "tasks": self.manifest}))
        artifact: dict[str, Any] = {"build": {"started": START, "completed": END}, "tasks": self.rows}
        artifact.update(artifact_extra or {})
        return mod.natives_report(
            artifact,
            run_dir=self.run_dir,
            sstate_dir=self.sstate,
            sstate_namespaces=[],
            stamp_roots=[],
            bitbake_lib=self.lib,
            **kw,
        )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


class FakeRun:
    def __init__(self, lines_for: Any = None, errors: list[dict[str, str]] | None = None, exc: Exception | None = None):
        self.calls: list[dict[str, Any]] = []
        self.lines_for = lines_for or (lambda recipe: ["Variable FOO value changed:", "-a", "+b"])
        self.errors = errors or []
        self.exc = exc
        self.returncode = 0
        self.stderr = ""

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        assert kw.get("shell") is not True
        # The request must arrive on stdin as a file, never as ``input=``: the
        # project's opengrep ruleset flags ``input=`` with a dynamic value.
        assert "input" not in kw
        self.calls.append(
            {"argv": argv, "request": json.loads(kw["stdin"].read()), "env": kw["env"], "timeout": kw["timeout"]}
        )
        if self.exc:
            raise self.exc
        bad = {(e["recipe"], e["task"]) for e in self.errors}
        results = [
            {"recipe": c["recipe"], "task": c["task"], "lines": self.lines_for(c["recipe"])}
            for c in self.calls[-1]["request"]["comparisons"]
            if (c["recipe"], c["task"]) not in bad
        ]
        out = json.dumps({"results": results, "errors": self.errors})
        return subprocess.CompletedProcess(argv, self.returncode, out, self.stderr)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    f = FakeRun()
    monkeypatch.setattr(mod.subprocess, "run", f)
    return f


def test_one_recipe_per_bucket_reconciles(env: Env, fake: FakeRun) -> None:
    env.recipe("attr-native", "c", prev=("a", START - 50))
    env.recipe("nocur-native", current=None)
    env.recipe("nopre-native", "c")
    env.recipe("same-native", "c", prev=("c", START - 50))
    fake.errors = []
    report = env.report()
    assert (report.attributed, report.not_recoverable, report.no_previous, report.unchanged) == (1, 1, 1, 1)
    assert report.rebuilt_recipes == 4
    assert report.reconciled
    assert report.not_recoverable_groups[0].reason == mod.REASON_NO_CURRENT
    assert report.no_previous_groups[0].reason == mod.REASON_NO_PREVIOUS
    assert report.groups[0].kind == "value"


def test_signature_inside_run_window_never_previous(env: Env, fake: FakeRun) -> None:
    env.recipe("x-native", "c", prev=("a", START + 10))
    report = env.report()
    assert report.no_previous == 1
    assert report.attributed == 0
    assert fake.calls == []


def test_same_hash_previous_is_unchanged_and_not_sent(env: Env, fake: FakeRun) -> None:
    env.recipe("same-native", "c", prev=("c", START - 50))
    env.recipe("diff-native", "c", prev=("a", START - 50))
    report = env.report()
    assert report.unchanged == 1
    assert len(fake.calls) == 1
    sent = [c["recipe"] for c in fake.calls[0]["request"]["comparisons"]]
    assert sent == ["diff-native"]


def test_timeout_sets_error_and_no_groups(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.recipe("x-native", "c", prev=("a", START - 50))
    monkeypatch.setattr(mod.subprocess, "run", FakeRun(exc=subprocess.TimeoutExpired("h", 1)))
    report = env.report(timeout=1)
    assert "timed out" in report.error
    assert report.groups == ()
    assert report.attributed == 0


def test_nonzero_exit_sets_error(env: Env, fake: FakeRun) -> None:
    env.recipe("x-native", "c", prev=("a", START - 50))
    fake.returncode = 2
    fake.stderr = "sigdiff_helper: bad request\n"
    report = env.report()
    assert "exit 2" in report.error
    assert report.groups == ()


def test_none_window_sends_everything_to_no_previous(env: Env, fake: FakeRun) -> None:
    env.recipe("a-native", "c", prev=("a", START - 50))
    env.recipe("b-native", "c")
    env.rows = [dict(r, started=None, completed=None) for r in env.rows]
    report = env.report({"build": None})
    assert report.no_previous == 2
    assert report.no_previous_groups[0].reason == mod.REASON_NO_WINDOW
    assert fake.calls == []
    assert report.reconciled


def test_helper_invoked_once_for_whole_report(env: Env, fake: FakeRun) -> None:
    for i in range(5):
        env.recipe(f"r{i}-native", "c", prev=("a", START - 50))
    env.report()
    assert len(fake.calls) == 1
    assert len(fake.calls[0]["request"]["comparisons"]) == 5
    assert fake.calls[0]["env"]["PYTHONPATH"] == str(env.lib)
    assert fake.calls[0]["argv"][1].endswith("sigdiff_helper.py")


def test_missing_bitbake_lib_names_path(env: Env, fake: FakeRun) -> None:
    env.recipe("x-native", "c", prev=("a", START - 50))
    (env.lib / "bb" / "siggen.py").unlink()
    report = env.report()
    assert str(env.lib / "bb" / "siggen.py") in report.error
    assert fake.calls == []


def test_forty_recipes_one_cause_one_group(env: Env, fake: FakeRun) -> None:
    for i in range(40):
        env.recipe(f"r{i:02d}-native", "c", prev=("a", START - 50))
    report = env.report()
    assert report.attributed == 40
    assert len(report.groups) == 1
    assert report.groups[0].recipes == 40
    assert len(report.groups[0].examples) == 5
    assert report.reconciled


def test_multi_cause_recipe_counts_once(env: Env, fake: FakeRun) -> None:
    env.recipe("m-native", "c", prev=("a", START - 50))
    fake.lines_for = lambda recipe: [
        "Variable FOO value changed:",
        "-a",
        "+b",
        "Variable BAR value changed:",
        "-a",
        "+b",
    ]
    report = env.report()
    assert report.attributed == 1
    assert len(report.groups) == 2
    assert report.reconciled


def test_nested_dependency_cause_names_the_task_where_it_appeared(env: Env, fake: FakeRun) -> None:
    env.recipe("m-native", "c", prev=("a", START - 50))
    fake.lines_for = lambda recipe: [
        "Hash for task dependency dep-native:do_configure changed from aaa to bbb",
        "    Variable FOO value changed:",
        "    -a",
        "    +b",
    ]
    report = env.report()
    assert report.groups[0].examples == (("m-native", "do_configure"),)


def test_helper_error_and_unrecoverable_are_not_recoverable(env: Env, fake: FakeRun) -> None:
    env.recipe("bad-native", "c", prev=("a", START - 50))
    env.recipe("gone-native", "c", prev=("a", START - 50))
    env.recipe("ok-native", "c", prev=("a", START - 50))
    fake.errors = [{"recipe": "bad-native", "task": TASK, "reason": "path outside request roots"}]
    fake.lines_for = lambda recipe: (
        ["Unable to find matching sigdata for foo-native.do_x"]
        if recipe == "gone-native"
        else ["Variable FOO value changed:", "-a", "+b"]
    )
    report = env.report()
    assert (report.attributed, report.not_recoverable) == (1, 2)
    reasons = {g.reason for g in report.not_recoverable_groups}
    assert "path outside request roots" in reasons
    assert report.reconciled


def test_missing_manifest_sets_error_with_counts(env: Env, fake: FakeRun) -> None:
    env.recipe("x-native", "c")
    artifact = {"tasks": env.rows}
    report = mod.natives_report(
        artifact,
        run_dir=env.run_dir,
        sstate_dir=env.sstate,
        sstate_namespaces=[],
        stamp_roots=[],
        bitbake_lib=env.lib,
    )
    assert report.error == mod.ERR_NO_MANIFEST
    assert report.executed_tasks == 1
    assert report.groups == ()
    assert fake.calls == []


def test_nothing_executed_gives_message(env: Env, fake: FakeRun) -> None:
    report = mod.natives_report(
        {"tasks": []},
        run_dir=env.run_dir,
        sstate_dir=env.sstate,
        sstate_namespaces=[],
        stamp_roots=[],
        bitbake_lib=env.lib,
    )
    assert report.message == mod.NO_DATA_MESSAGE
    assert report.error == ""


def test_extra_allowed_roots_reach_the_request(env: Env, fake: FakeRun, tmp_path: Path) -> None:
    env.recipe("x-native", "c", prev=("a", START - 50))
    seeds = [tmp_path / "seed" / "scarthgap"]
    env.report(extra_allowed_roots=seeds)
    assert fake.calls[-1]["request"]["roots"]["also_allowed"] == [str(seeds[0])]


def test_extra_allowed_roots_default_is_empty(env: Env, fake: FakeRun) -> None:
    env.recipe("x-native", "c", prev=("a", START - 50))
    env.report()
    assert fake.calls[-1]["request"]["roots"]["also_allowed"] == []


def test_rows_named_by_pf_pair_with_the_pn_keyed_manifest(env: Env, fake: FakeRun) -> None:
    # Real event rows say "quilt-native-0.69-r0"; the manifest and ledger say "quilt-native".
    env.recipe("quilt-native", "c", prev=("a", START - 50))
    env.rows = [dict(r, recipe="quilt-native-0.69-r0") for r in env.rows]
    report = env.report()
    assert (report.attributed, report.not_recoverable, report.no_previous) == (1, 0, 0)
    assert report.reconciled
    assert fake.calls[0]["request"]["comparisons"][0]["recipe"] == "quilt-native"


def test_changed_early_task_is_attributed_when_sysroot_signature_is_unchanged(env: Env, fake: FakeRun) -> None:
    # Hash equivalence keeps do_populate_sysroot stable while do_configure changed.
    def ledger(task: str, hash_: str, mtime: float) -> None:
        p = ledger_root(env.sstate) / "q-native" / f"{task}.{_h(hash_)}.sigdata"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        os.utime(p, (mtime, mtime))

    for task, started in (("do_configure", END - 50), ("do_populate_sysroot", END - 10)):
        env.rows.append(
            {"recipe": "q-native", "task": task, "outcome": "succeeded", "started": started, "completed": started + 1}
        )
    ledger("do_configure", "a", START - 50)
    ledger("do_configure", "b", END - 40)
    ledger("do_populate_sysroot", "e", START - 50)  # same hash before and after
    env.manifest += [
        {"recipe": "q-native", "task": "do_configure", "hash": _h("b")},
        {"recipe": "q-native", "task": "do_populate_sysroot", "hash": _h("e")},
    ]
    report = env.report()
    assert (report.attributed, report.unchanged) == (1, 0)
    assert report.reconciled
    sent = fake.calls[0]["request"]["comparisons"]
    assert [(c["recipe"], c["task"]) for c in sent] == [("q-native", "do_configure")]
    assert report.groups[0].examples == (("q-native", "do_configure"),)
