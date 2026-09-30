"""Tests for bakar.native_ledger."""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING

import pytest

from bakar import native_ledger as nl

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

H1 = "a" * 40
H2 = "b" * 40


def _sig(  # noqa: PLR0913
    root: Path, recipe: str, task: str, h: str, mtime: float, arch: str = "x86_64-linux"
) -> Path:
    d = root / arch / recipe
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"1.0-r0.{task}.sigdata.{h}"
    p.write_text("sig")
    os.utime(p, (mtime, mtime))
    return p


def _row(recipe: str, task: str = "do_compile", outcome: str = "succeeded", started=1000.0, completed=1010.0):
    return {"recipe": recipe, "task": task, "outcome": outcome, "started": started, "completed": completed}


@pytest.mark.parametrize("name", ["cmake-native", "gcc-cross-aarch64", "gcc-crosssdk-x86_64"])
def test_classifier_accepts(name):
    assert nl.is_native_or_cross(name)


@pytest.mark.parametrize("name", ["busybox", "gcc-cross-canadian-aarch64", "nativesdk-cmake"])
def test_classifier_rejects(name):
    assert not nl.is_native_or_cross(name)


def test_validators():
    assert nl.valid_recipe("gtk+3-native")
    assert not nl.valid_recipe("../../etc")
    assert not nl.valid_recipe("a..b")
    assert not nl.valid_recipe("")
    assert nl.valid_task("do_compile")
    assert not nl.valid_task("compile")
    assert nl.valid_hash(H1)
    assert not nl.valid_hash("abc")
    assert not nl.valid_hash("A" * 40)


def test_task_selection():
    art = {
        "tasks": [
            _row("a-native"),
            _row("b-native", outcome="failed"),
            _row("c-native", outcome=None),
            _row("d-native", task="do_compile_setscene"),
            _row("busybox"),
        ]
    }
    assert [r["recipe"] for r in nl.executed_native_tasks(art)] == ["a-native", "b-native"]
    assert nl.restored_native_count(art) == 1


def test_find_only_in_window(tmp_path):
    root = tmp_path / "stamps"
    _sig(root, "x-native", "do_compile", H1, 1000.0 - 500)  # too old
    good = _sig(root, "x-native", "do_compile", H2, 1005.0, arch="other")
    assert nl.find_task_sigdata([root], _row("x-native")) == good
    assert nl.find_task_sigdata([root], _row("x-native", started=None)) is None
    assert nl.find_task_sigdata([root], _row("x-native", completed=None)) is None


def test_multiconfig_root(tmp_path):
    tmpdir = tmp_path / "tmp"
    (tmpdir / "stamps").mkdir(parents=True)
    mc = tmp_path / "tmp-mc" / "stamps"
    mc.mkdir(parents=True)
    roots = nl.stamp_roots(tmpdir, tmp_path)
    assert roots == [tmpdir / "stamps", mc]
    f = _sig(mc, "x-native", "do_compile", H1, 1005.0)
    assert nl.find_task_sigdata(roots, _row("x-native")) == f


def test_capture_copies_and_manifest(tmp_path):
    root = tmp_path / "stamps"
    _sig(root, "x-native", "do_compile", H1, 1005.0)
    _sig(root, "busybox", "do_compile", H2, 1005.0)
    sstate, run = tmp_path / "sstate", tmp_path / "run"
    art = {
        "tasks": [
            _row("x-native"),
            _row("busybox"),
            _row("gcc-cross-canadian-aarch64"),
            _row("miss-native"),
            _row("y-native", task="do_x_setscene"),
        ]
    }
    res = nl.capture_run(art, roots=[root], sstate_dir=sstate, run_dir=run)
    assert (res.executed, res.restored, res.copied, res.missing, res.invalid, res.failed) == (2, 1, 1, 1, 0, 0)
    entry = sstate / ".bakar" / "native-sigdata" / "x-native" / f"do_compile.{H1}.sigdata"
    assert entry.read_text() == "sig"
    assert abs(entry.stat().st_mtime - time.time()) < 60  # copy time, not source mtime
    assert not list((sstate / ".bakar" / "native-sigdata").rglob("*busybox*"))
    manifest = json.loads((run / "native-signatures.json").read_text())
    assert manifest == {"schema": 1, "tasks": [{"recipe": "x-native", "task": "do_compile", "hash": H1}]}
    # rerun replaces, leaving exactly one entry
    nl.capture_run(art, roots=[root], sstate_dir=sstate, run_dir=run)
    assert [p.name for p in entry.parent.glob("*.sigdata")] == [entry.name]
    assert [p.name for p in entry.parent.iterdir() if p.name != entry.name] == [entry.name + ".seen"]
    assert nl.ledger_entry(sstate, "x-native", "do_compile", H1) == entry
    assert nl.ledger_entry(sstate, "../x", "do_compile", H1) is None


def test_traversal_recipe_skipped(tmp_path):
    sstate, run = tmp_path / "sstate", tmp_path / "run"
    art = {"tasks": [_row("../../etc-native")]}
    res = nl.capture_run(art, roots=[tmp_path], sstate_dir=sstate, run_dir=run)
    assert res.invalid == 1 and res.copied == 0
    assert not (tmp_path / "etc-native").exists()
    assert not sstate.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_readonly_destination_counted(tmp_path):
    root = tmp_path / "stamps"
    _sig(root, "x-native", "do_compile", H1, 1005.0)
    sstate = tmp_path / "sstate"
    sstate.mkdir()
    sstate.chmod(0o500)
    try:
        res = nl.capture_run({"tasks": [_row("x-native")]}, roots=[root], sstate_dir=sstate, run_dir=tmp_path / "run")
    finally:
        sstate.chmod(0o700)
    assert res.failed == 1 and res.copied == 0


def test_symlinked_stamp_entry_is_never_captured(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret")
    os.utime(secret, (1005.0, 1005.0))
    root = tmp_path / "stamps"
    d = root / "x86_64-linux" / "x-native"
    d.mkdir(parents=True)
    (d / f"1.0-r0.do_compile.sigdata.{H1}").symlink_to(secret)
    assert nl.find_task_sigdata([root], _row("x-native")) is None
    sstate = tmp_path / "sstate"
    res = nl.capture_run({"tasks": [_row("x-native")]}, roots=[root], sstate_dir=sstate, run_dir=tmp_path / "run")
    assert (res.copied, res.missing) == (0, 1)
    assert not sstate.exists()


def test_symlinked_arch_directory_leaving_the_root_is_never_captured(tmp_path):
    outside = tmp_path / "outside"
    _sig(outside, "x-native", "do_compile", H1, 1005.0, arch="elsewhere")
    root = tmp_path / "stamps"
    root.mkdir()
    (root / "x86_64-linux").symlink_to(outside / "elsewhere")
    assert nl.find_task_sigdata([root], _row("x-native")) is None


def test_symlinked_stamps_root_is_still_searched(tmp_path):
    real = tmp_path / "fast-disk" / "stamps"
    good = _sig(real, "x-native", "do_compile", H1, 1005.0)
    link = tmp_path / "tmp" / "stamps"
    link.parent.mkdir()
    link.symlink_to(real)
    found = nl.find_task_sigdata([link], _row("x-native"))
    assert found is not None and found.resolve() == good.resolve()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_unwritable_run_dir_reports_manifest_failure(tmp_path):
    root = tmp_path / "stamps"
    _sig(root, "x-native", "do_compile", H1, 1005.0)
    run = tmp_path / "run"
    run.mkdir()
    run.chmod(0o500)
    try:
        res = nl.capture_run({"tasks": [_row("x-native")]}, roots=[root], sstate_dir=tmp_path / "sstate", run_dir=run)
    finally:
        run.chmod(0o700)
    assert res.manifest_failed
    assert not list(run.iterdir())


def test_manifest_write_leaves_no_temp_file(tmp_path):
    root = tmp_path / "stamps"
    _sig(root, "x-native", "do_compile", H1, 1005.0)
    run = tmp_path / "run"
    res = nl.capture_run({"tasks": [_row("x-native")]}, roots=[root], sstate_dir=tmp_path / "sstate", run_dir=run)
    assert not res.manifest_failed
    assert [p.name for p in run.iterdir()] == ["native-signatures.json"]


def test_effective_sstate_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    assert nl.effective_sstate_dir(None) is None
    assert nl.effective_sstate_dir("") is None
    assert nl.effective_sstate_dir(str(tmp_path / "cfg")) == tmp_path / "cfg"
    monkeypatch.setenv("SSTATE_DIR", str(tmp_path / "env"))
    assert nl.effective_sstate_dir(str(tmp_path / "cfg")) == tmp_path / "env"


def test_stamps_entry_by_hash(tmp_path):
    r1, r2 = tmp_path / "s1", tmp_path / "s2"
    r1.mkdir()
    f = _sig(r2, "x-native", "do_compile", H1, 5.0)
    assert nl.stamps_entry([r1, r2], "x-native", "do_compile", H1) == f
    assert nl.stamps_entry([r1, r2], "x-native", "do_compile", H2) is None
    assert nl.stamps_entry([r1, r2], "../x", "do_compile", H1) is None


def test_earlier_ledger_excludes_at_or_after(tmp_path):
    d = tmp_path / ".bakar" / "native-sigdata" / "x-native"
    d.mkdir(parents=True)
    for h, m in ((H1, 100.0), (H2, 200.0), ("c" * 40, 300.0)):
        p = d / f"do_compile.{h}.sigdata"
        p.write_text("x")
        os.utime(p, (m, m))
    got = nl.earlier_ledger_signatures(tmp_path, "x-native", "do_compile", before=300.0)
    assert [(h, m) for h, _, m in got] == [(H2, 200.0), (H1, 100.0)]
    assert nl.earlier_ledger_signatures(tmp_path, "nope-native", "do_compile", before=300.0) == []


def _siginfo(  # noqa: PLR0913
    ns: Path, recipe: str, task: str, h: str, mtime: float, xx="ab", yy="cd"
) -> Path:
    d = ns / xx / yy
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"sstate:{recipe}:arch:1.0:r0:arch:12:{h}_{task[3:]}.tar.zst.siginfo"
    p.write_text("x")
    os.utime(p, (mtime, mtime))
    return p


def test_scan_sstate_siginfo(tmp_path):
    ns = tmp_path / "sstate"
    old = _siginfo(ns, "x-native", "do_populate_sysroot", H1, 100.0)
    _siginfo(ns, "x-native", "do_populate_sysroot", H2, 500.0)  # not before
    _siginfo(ns, "x-native", "do_compile", "c" * 40, 100.0)  # other task
    _siginfo(ns, "other", "do_populate_sysroot", "d" * 40, 100.0)  # unwanted recipe
    res = nl.scan_sstate_siginfo(
        [ns], {"x-native": "do_populate_sysroot"}, before=400.0, deadline=time.monotonic() + 60
    )
    assert res.complete
    assert res.found == {"x-native": [(H1, old, 100.0)]}
    found, complete = res
    assert complete and found == res.found


def test_scan_honours_deadline(tmp_path):
    ns = tmp_path / "sstate"
    _siginfo(ns, "x-native", "do_populate_sysroot", H1, 100.0)
    res = nl.scan_sstate_siginfo([ns], {"x-native": "do_populate_sysroot"}, before=400.0, deadline=time.monotonic() - 1)
    assert not res.complete
    assert res.found == {}


# --- re-capture of an already-ledgered signature must not move its mtime ---

_RECIPE, _TASK = "foo-native", "do_populate_sysroot"


def _run_once(tmp_path: Path, idx: int, h: str) -> tuple[dict, Path, Path, float]:
    """One real ``capture_run`` over a fresh stamps tree; returns (artifact, run_dir, stamps, start)."""
    stamps = tmp_path / f"stamps{idx}"
    start = time.time()
    time.sleep(0.05)
    _sig(stamps, _RECIPE, _TASK, h, time.time())
    end = time.time() + 0.05
    art = {"tasks": [_row(_RECIPE, _TASK, started=start, completed=end)], "build": {"started": start, "completed": end}}
    run_dir = tmp_path / f"run{idx}"
    res = nl.capture_run(art, roots=[stamps], sstate_dir=tmp_path / "sstate", run_dir=run_dir)
    assert res.copied == 1
    assert res.failed == 0
    time.sleep(0.1)
    return art, run_dir, stamps, start


def test_recapture_same_hash_keeps_first_capture_mtime(tmp_path):
    """Scenario A: run 2 rebuilds hash H (sstate cleaned); run 2's earlier lookup must still see H."""
    sstate = tmp_path / "sstate"
    _run_once(tmp_path, 0, H1)
    entry = nl.ledger_entry(sstate, _RECIPE, _TASK, H1)
    assert entry is not None
    first_mtime = entry.stat().st_mtime
    _, run2, _, start2 = _run_once(tmp_path, 1, H1)
    assert entry.stat().st_mtime == first_mtime
    earlier = nl.earlier_ledger_signatures(sstate, _RECIPE, _TASK, before=start2)
    assert [h for h, _, _ in earlier] == [H1]
    manifest = json.loads((run2 / "native-signatures.json").read_text())
    assert manifest["tasks"] == [{"recipe": _RECIPE, "task": _TASK, "hash": H1}]
    assert not list(entry.parent.glob(".*.part"))


def test_recapture_older_hash_stays_visible_to_middle_run(tmp_path):
    """Scenario B: run1 h1, run2 h2, run3 rebuilds h1 -> run2 still sees h1 as its previous."""
    sstate = tmp_path / "sstate"
    _run_once(tmp_path, 0, H1)
    _, _, _, start2 = _run_once(tmp_path, 1, H2)
    _run_once(tmp_path, 2, H1)
    earlier = nl.earlier_ledger_signatures(sstate, _RECIPE, _TASK, before=start2)
    assert [h for h, _, _ in earlier] == [H1]


def test_natives_report_recaptured_same_hash_is_unchanged(tmp_path, monkeypatch):
    """Scenario A end to end: a cleaned-and-rebuilt identical signature reports unchanged."""
    from bakar import insights_natives as mod

    def _no_helper(*_a, **_k):
        raise AssertionError("helper must not run for an unchanged signature")

    monkeypatch.setattr(mod.subprocess, "run", _no_helper)
    lib = tmp_path / "bitbake" / "lib"
    (lib / "bb").mkdir(parents=True)
    (lib / "bb" / "siggen.py").write_text("")
    _run_once(tmp_path, 0, H1)
    art2, run2, stamps2, _ = _run_once(tmp_path, 1, H1)
    rep = mod.natives_report(
        art2,
        run_dir=run2,
        sstate_dir=tmp_path / "sstate",
        sstate_namespaces=[],
        stamp_roots=[stamps2],
        bitbake_lib=lib,
    )
    assert (rep.unchanged, rep.no_previous, rep.attributed) == (1, 0, 0)


def test_find_rejects_signature_newer_than_window(tmp_path):
    root = tmp_path / "stamps"
    _sig(root, "x-native", "do_compile", H1, 1010.0 + nl.DEFAULT_SLACK_SECONDS + 60)  # too new
    assert nl.find_task_sigdata([root], _row("x-native")) is None
    good = _sig(root, "x-native", "do_compile", H2, 1005.0, arch="other")
    assert nl.find_task_sigdata([root], _row("x-native")) == good


def test_failed_copy_leaves_no_temp_and_is_counted(tmp_path, monkeypatch):
    root = tmp_path / "stamps"
    _sig(root, "x-native", "do_compile", H1, 1005.0)
    sstate, run = tmp_path / "sstate", tmp_path / "run"

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(nl.shutil, "copyfile", _boom)
    res = nl.capture_run({"tasks": [_row("x-native")]}, roots=[root], sstate_dir=sstate, run_dir=run)
    assert (res.copied, res.failed) == (0, 1)
    recipe_dir = nl.ledger_root(sstate) / "x-native"
    assert list(recipe_dir.iterdir()) == []
    assert (run / "native-signatures.json").is_file()


def test_orphan_temp_file_ignored_by_lookups(tmp_path):
    sstate = tmp_path / "sstate"
    recipe_dir = nl.ledger_root(sstate) / "x-native"
    recipe_dir.mkdir(parents=True)
    (recipe_dir / ".tmpabc123.part").write_text("partial")
    (recipe_dir / f".do_compile.{H1}.sigdata.part").write_text("partial")
    assert nl.earlier_ledger_signatures(sstate, "x-native", "do_compile", before=time.time() + 60) == []
    assert nl.ledger_entry(sstate, "x-native", "do_compile", H1) is None


def test_rows_named_by_pf_are_classified_and_captured_under_the_pn(tmp_path):
    # A real normalized event row carries the PF ("quilt-native-0.69-r0"); the stamps
    # directory, the ledger and the manifest are all keyed by the PN ("quilt-native").
    root = tmp_path / "stamps"
    _sig(root, "quilt-native", "do_configure", H1, 1005.0)
    sstate, run = tmp_path / "sstate", tmp_path / "run"
    art = {
        "tasks": [
            _row("quilt-native-0.69-r0", task="do_configure"),
            _row("quilt-native-0.69-r0", task="do_install_setscene"),
            _row("gcc-cross-x86_64-14.2.0-r0", task="do_compile_setscene"),
            _row("busybox-1.37.0-r0"),
        ]
    }
    assert [r["task"] for r in nl.executed_native_tasks(art)] == ["do_configure"]
    assert nl.restored_native_count(art) == 2
    res = nl.capture_run(art, roots=[root], sstate_dir=sstate, run_dir=run)
    assert (res.executed, res.copied, res.missing, res.invalid) == (1, 1, 0, 0)
    assert nl.ledger_entry(sstate, "quilt-native", "do_configure", H1) is not None
    manifest = json.loads((run / "native-signatures.json").read_text())
    assert manifest["tasks"] == [{"recipe": "quilt-native", "task": "do_configure", "hash": H1}]


@pytest.mark.parametrize(
    ("pf", "pn"),
    [
        ("quilt-native-0.69-r0", "quilt-native"),
        ("gcc-cross-x86_64-14.2.0-r0", "gcc-cross-x86_64"),
        ("python3-native-3.13.2-r1", "python3-native"),
        ("cmake-native", "cmake-native"),
    ],
)
def test_row_pn_strips_version_and_revision(pf, pn):
    assert nl.row_pn({"recipe": pf}) == pn


def test_stale_stamp_before_the_build_started_is_missing_not_captured(tmp_path):
    # A task that failed before writing a signature must not pick up the file the
    # previous build left inside the +-120s slack, but before this run began.
    root = tmp_path / "stamps"
    _sig(root, "x-native", "do_configure", H1, 950.0)  # inside started-120, before the build
    sstate = tmp_path / "sstate"
    row = _row("x-native", task="do_configure", outcome="failed", started=1000.0, completed=1010.0)
    res = nl.capture_run(
        {"build": {"started": 990.0}, "tasks": [row]}, roots=[root], sstate_dir=sstate, run_dir=tmp_path / "run"
    )
    assert (res.executed, res.copied, res.missing) == (1, 0, 1)
    assert json.loads((tmp_path / "run" / "native-signatures.json").read_text())["tasks"] == []


def test_stamp_written_inside_the_build_is_still_captured(tmp_path):
    root = tmp_path / "stamps"
    _sig(root, "x-native", "do_configure", H1, 995.0)  # before the task, after the build began
    row = _row("x-native", task="do_configure", started=1000.0, completed=1010.0)
    art = {"build": {"started": 990.0}, "tasks": [row]}
    res = nl.capture_run(art, roots=[root], sstate_dir=tmp_path / "sstate", run_dir=tmp_path / "run")
    assert (res.copied, res.missing) == (1, 0)
    # An artifact without a build window keeps the task-window behaviour.
    art = {"tasks": [row]}
    res = nl.capture_run(art, roots=[root], sstate_dir=tmp_path / "sstate2", run_dir=tmp_path / "run2")
    assert (res.copied, res.missing) == (1, 0)


def _entry(sstate, task, h, mtime):
    p = nl.ledger_root(sstate) / "x-native" / f"{task}.{h}.sigdata"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("sig")
    os.utime(p, (mtime, mtime))
    return p


def test_rebuilt_signature_orders_after_its_replacement(tmp_path):
    # A built, replaced by B, then A built again: A is the predecessor of the next run.
    sstate = tmp_path / "sstate"
    a = _entry(sstate, "do_configure", H1, 100.0)
    _entry(sstate, "do_configure", H2, 200.0)
    src = tmp_path / "src"
    src.write_text("sig")
    nl._copy_into_ledger(src, a)  # recapture of an existing entry
    marker = a.with_name(a.name + ".seen")
    assert len(marker.read_text().splitlines()) == 1  # one sighting line appended
    assert a.stat().st_mtime == 100.0  # the entry itself is untouched
    nl._copy_into_ledger(src, a)
    assert len(marker.read_text().splitlines()) == 2  # history grows, it is not overwritten
    marker.write_text("300.0\n450.0\n")  # rebuilt at 300, and again by the run being analysed at 450
    got = nl.earlier_ledger_signatures(sstate, "x-native", "do_configure", before=400.0)
    assert [h for h, _, _ in got] == [H1, H2]  # the 450 sighting does not hide the one at 300
    # Analysing the run that preceded the recapture still sees B as the newest.
    got = nl.earlier_ledger_signatures(sstate, "x-native", "do_configure", before=250.0)
    assert [h for h, _, _ in got] == [H2, H1]


def test_garbage_in_a_seen_marker_is_ignored(tmp_path):
    sstate = tmp_path / "sstate"
    a = _entry(sstate, "do_configure", H1, 100.0)
    _entry(sstate, "do_configure", H2, 200.0)
    a.with_name(a.name + ".seen").write_bytes(b"nan-not\n\xff\xfe\n300.0\n")
    got = nl.earlier_ledger_signatures(sstate, "x-native", "do_configure", before=400.0)
    assert [h for h, _, _ in got] == [H1, H2]


def test_seen_marker_is_never_returned_as_a_signature(tmp_path):
    sstate = tmp_path / "sstate"
    a = _entry(sstate, "do_configure", H1, 100.0)
    a.with_name(a.name + ".seen").write_text("")
    got = nl.earlier_ledger_signatures(sstate, "x-native", "do_configure", before=400.0)
    assert [p.name for _, p, _ in got] == [a.name]


def test_seen_marker_is_not_written_through_a_symlink(tmp_path):
    sstate = tmp_path / "sstate"
    a = _entry(sstate, "do_configure", H1, 100.0)
    victim = tmp_path / "victim"
    victim.write_text("keep")
    os.utime(victim, (500.0, 500.0))
    a.with_name(a.name + ".seen").symlink_to(victim)
    src = tmp_path / "src"
    src.write_text("sig")
    nl._copy_into_ledger(src, a)
    assert victim.read_text() == "keep"
    assert victim.stat().st_mtime == 500.0  # the marker write refused the link
