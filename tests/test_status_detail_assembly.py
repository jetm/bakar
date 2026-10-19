"""Assembly of the ``bakar status --detail`` snapshot."""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar.commands import _status_detail as sd

pytestmark = pytest.mark.unit

NOW = 10_000.0
HOST = {
    "disk_free_gb": {"build": 12.5, "sstate": None},
    "mem_available_mb": 2048,
    "pressure": {"cpu": 1.0, "io": None, "memory": 0.5},
}


def _measure(build, sstate, **_kw):
    _measure.calls.append((build, sstate))
    return dict(HOST)


_measure.calls = []


def _detail(tmp_path: Path, **kw):
    rd = kw.pop("run_dir", tmp_path / "build" / "runs" / "r1")
    if rd is not None:
        rd.mkdir(parents=True, exist_ok=True)
    args = {"state": "running", "progress": None, "has_eventlog": True, "unit_log": None} | kw
    return sd.build_detail(run_dir=rd, now=NOW, measure=_measure, **args)


def _running(*started):
    return {
        "tasks_failed": 0,
        "failures": [],
        "running": [{"recipe": f"r{i}-1.0-r0", "task": "do_compile", "started": s} for i, s in enumerate(started)],
    }


def test_running_tasks_ordered_by_seconds_descending(tmp_path):
    d = _detail(tmp_path, progress=_running(NOW - 5, NOW - 300, NOW - 20))
    assert [r["seconds"] for r in d["running_tasks"]] == [300, 20, 5]
    assert d["running_tasks"][0] == {"recipe": "r1-1.0-r0", "task": "do_compile", "seconds": 300}


def test_running_tasks_capped_at_16(tmp_path):
    d = _detail(tmp_path, progress=_running(*[NOW - i for i in range(20)]))
    assert len(d["running_tasks"]) == 16


def test_running_tasks_none_for_succeeded(tmp_path):
    assert _detail(tmp_path, state="succeeded", progress=_running(NOW - 1))["running_tasks"] is None


def test_running_tasks_none_without_eventlog(tmp_path):
    assert _detail(tmp_path, has_eventlog=False, progress=_running(NOW - 1))["running_tasks"] is None


def test_running_seconds_none_for_missing_start_sorts_last(tmp_path):
    d = _detail(tmp_path, progress=_running(None, NOW - 7))
    assert [r["seconds"] for r in d["running_tasks"]] == [7, None]


def test_running_seconds_clamped_at_zero(tmp_path):
    d = _detail(tmp_path, progress=_running(NOW + 50))
    assert d["running_tasks"][0]["seconds"] == 0


def test_failed_tasks_two_names(tmp_path):
    p = {
        "tasks_failed": 2,
        "failures": [{"recipe": "a-1-r0", "task": "do_fetch"}, {"recipe": "b-2-r0", "task": "do_compile"}],
    }
    assert _detail(tmp_path, progress=p)["failed_tasks"] == {
        "count": 2,
        "names": ["a-1-r0:do_fetch", "b-2-r0:do_compile"],
    }


def test_failed_tasks_count_exceeds_names(tmp_path):
    p = {"tasks_failed": 7, "failures": [{"recipe": f"r{i}", "task": "t"} for i in range(5)]}
    f = _detail(tmp_path, progress=p)["failed_tasks"]
    assert f["count"] == 7
    assert len(f["names"]) == 5


def test_failed_tasks_zero_with_setscene_only(tmp_path):
    p = {"tasks_failed": 0, "failures": [], "tasks_setscene_rerun": 3}
    assert _detail(tmp_path, progress=p)["failed_tasks"] == {"count": 0, "names": []}


def test_failed_tasks_none_without_eventlog(tmp_path):
    assert _detail(tmp_path, has_eventlog=False, progress=_running())["failed_tasks"] is None


def test_last_line_prefers_kas_log_else_unit_log(tmp_path):
    unit = tmp_path / "unit.log"
    unit.write_text("from unit\n")
    rd = tmp_path / "build" / "runs" / "r1"
    assert _detail(tmp_path, unit_log=unit)["last_line"] == "from unit"
    (rd / "kas.log").write_text("from kas\n")
    assert _detail(tmp_path, unit_log=unit)["last_line"] == "from kas"


def test_last_line_with_trailing_blank_lines(tmp_path):
    p = tmp_path / "l.log"
    p.write_text("first\nsecond\n\n  \n\n")
    assert sd.last_line(p) == "second"


def test_last_line_none_for_missing_or_blank(tmp_path):
    assert sd.last_line(tmp_path / "nope") is None
    p = tmp_path / "blank.log"
    p.write_text("\n  \n")
    assert sd.last_line(p) is None


def test_last_line_strips_escape_and_control_characters(tmp_path):
    p = tmp_path / "l.log"
    p.write_bytes(b"\x1b]0;window title\x07\x1b[31mred\x1b[0m\ttext\x07 end\n")
    assert sd.last_line(p) == "red text end"


def test_last_line_keeps_text_after_last_carriage_return(tmp_path):
    p = tmp_path / "l.log"
    p.write_text("Parsing 10%\rParsing 55%\rParsing 99%\n")
    assert sd.last_line(p) == "Parsing 99%"


def test_last_line_cut_to_200_characters(tmp_path):
    p = tmp_path / "l.log"
    p.write_text("x" * 500 + "\n")
    assert sd.last_line(p) == "x" * 200


def test_last_line_does_not_read_whole_file(tmp_path, monkeypatch):
    p = tmp_path / "big.log"
    p.write_bytes(b"a" * (1024 * 1024) + b"\nshort\n")

    def boom(*_a, **_k):
        raise AssertionError("whole-file read")

    monkeypatch.setattr(Path, "read_text", boom)
    monkeypatch.setattr(Path, "read_bytes", boom)
    assert sd.last_line(p) == "short"


def test_host_fallback_uses_runs_parent(tmp_path):
    _measure.calls.clear()
    d = _detail(tmp_path, progress=_running())
    assert _measure.calls == [(tmp_path / "build", None)]
    assert d["disk_free_gb"] == HOST["disk_free_gb"]
    assert d["mem_available_mb"] == 2048


def test_host_uses_recorded_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(sd.observability, "last_build_paths", lambda rd: (Path("/t"), Path("/s")))
    _measure.calls.clear()
    _detail(tmp_path, progress=_running())
    assert _measure.calls == [(Path("/t"), Path("/s"))]


def test_host_without_run_dir_measures_none(tmp_path):
    _measure.calls.clear()
    d = _detail(tmp_path, run_dir=None)
    assert _measure.calls == [(None, None)]
    assert d["last_line"] is None


def test_detail_lines_running_without_failures(tmp_path):
    d = _detail(tmp_path, progress=_running(NOW - 723))
    lines = sd.detail_lines("r1 running", d)
    assert len(lines) == 4
    assert lines[1] == "running: r0-1.0-r0:do_compile 12m03s"
    assert lines[2].startswith("host: disk build 12.5G sstate ?G free, mem 2048M available")
    assert "io ?" in lines[2]
    assert lines[3] == "last: ?"


def test_detail_lines_succeeded_has_three_lines(tmp_path):
    d = _detail(tmp_path, state="succeeded", progress=_running())
    assert len(sd.detail_lines("r1 succeeded", d)) == 3


def test_detail_lines_failed_and_more(tmp_path):
    p = _running(*[NOW - i for i in range(8)])
    p.update(tasks_failed=1, failures=[{"recipe": "z-1-r0", "task": "do_x"}])
    lines = sd.detail_lines("s", _detail(tmp_path, progress=p))
    assert lines[1].endswith("(+3 more)")
    assert lines[2] == "failed: 1 (z-1-r0:do_x)"


def test_detail_lines_prints_question_mark_for_none():
    d = {
        "running_tasks": None,
        "failed_tasks": None,
        "last_line": None,
        "disk_free_gb": {"build": None, "sstate": None},
        "mem_available_mb": None,
        "pressure": None,
    }
    lines = sd.detail_lines("s", d)
    assert "None" not in "\n".join(lines)
    assert lines[1] == "host: disk build ?G sstate ?G free, mem ?M available, pressure cpu ? io ? mem ?"
