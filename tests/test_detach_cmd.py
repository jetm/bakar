"""Tests for the shared ``--detach`` front (commands/_detach_cmd.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bakar import detach
from bakar.commands import _detach_cmd

ARGV = ["--detach", "build", "x.yml"]


def _handle(log: Path, state="running", run_id="20260101-000000", code=None):
    return detach.Handle(run_id, "bakar-detach-abc", None, str(log), state, code)


def _run(monkeypatch, handle=None, linger=True, exc=None):
    seen = {}

    def fake_launch(request):
        seen["request"] = request
        if exc:
            raise exc
        return handle

    monkeypatch.setattr(detach, "launch", fake_launch)
    monkeypatch.setattr(detach, "linger_enabled", lambda: linger)
    with pytest.raises(SystemExit) as info:
        _detach_cmd.detach_and_exit(ARGV, Path("/work"))
    return info.value.code, seen


def test_handle_line_is_the_only_stdout(monkeypatch, capsys, tmp_path):
    code, seen = _run(monkeypatch, _handle(tmp_path / "l.log"))
    out, err = capsys.readouterr()
    assert code == 0
    lines = out.splitlines()
    assert len(lines) == 1
    assert set(json.loads(lines[0])) == {"run_id", "unit", "host", "log", "state"}
    assert "hint:" in err
    assert seen["request"].argv == ["build", "x.yml"]
    assert seen["request"].cwd == Path("/work")


def test_unavailable_exits_2_with_reason_and_no_handle(monkeypatch, capsys):
    code, _ = _run(monkeypatch, exc=detach.DetachUnavailable("user manager unreachable"))
    out, err = capsys.readouterr()
    assert code == 2
    assert out == ""
    assert "user manager unreachable" in err


def test_early_failure_exits_with_sentinel_code_and_log_tail(monkeypatch, capsys, tmp_path):
    log = tmp_path / "l.log"
    log.write_text("\n".join(f"line{i}" for i in range(30)) + "\n")
    code, _ = _run(monkeypatch, _handle(log, state="failed", code=3))
    out, err = capsys.readouterr()
    assert code == 3
    assert len(out.splitlines()) == 1
    assert "line29" in err
    assert "line10" in err
    assert "line9\n" not in err


def test_linger_false_warns(monkeypatch, capsys, tmp_path):
    _run(monkeypatch, _handle(tmp_path / "l.log"), linger=False)
    out, err = capsys.readouterr()
    assert "loginctl enable-linger" in err
    assert "enable-linger" not in out


def test_linger_unknown_does_not_warn(monkeypatch, capsys, tmp_path):
    _run(monkeypatch, _handle(tmp_path / "l.log"), linger=None)
    assert "enable-linger" not in capsys.readouterr().err


def test_lifecycle_lines_name_run_id_or_unit(monkeypatch, capsys, tmp_path):
    _run(monkeypatch, _handle(tmp_path / "l.log"))
    out, err = capsys.readouterr()
    assert "bakar wait 20260101-000000" in err
    assert "hint" not in out
    _run(monkeypatch, _handle(tmp_path / "l.log", state="starting", run_id=None))
    out, err = capsys.readouterr()
    assert "bakar wait bakar-detach-abc" in err
    assert "hint" not in out
