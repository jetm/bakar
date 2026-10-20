"""Tests for the container-to-host recipe-log path translation in triage.

``translate_container_path`` is the shared helper extracted from
``_find_recipe_log``; both are exercised against synthetic ``tmp_path``
fixtures so the tests never touch the real host filesystem. The kas.log
sample mirrors the ``Logfile of failure stored in: /work/...`` shape that
``_RECIPE_LOG_RE`` matches (see ``tests/conftest.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bakar.cli import app
from bakar.commands import triage as triage_cmd
from bakar.triage import _find_recipe_log, analyse, read_failure_log, translate_container_path


@pytest.mark.unit
def test_translate_container_path_rewrites_work_prefix(tmp_path: Path) -> None:
    """A ``/work/``-prefixed path maps to the workspace-rooted host path."""
    workspace = tmp_path / "nxp"
    container_path = "/work/build/tmp/deploy/linux-imx/temp/log.do_compile"

    result = translate_container_path(container_path, workspace)

    assert result == f"{workspace}/build/tmp/deploy/linux-imx/temp/log.do_compile"
    assert result.startswith(str(workspace) + "/")
    # Only the leading prefix is rewritten, so the result no longer begins
    # with the container "/work/" mount root.
    assert not result.startswith("/work/")


@pytest.mark.unit
def test_translate_container_path_passes_non_work_unchanged(tmp_path: Path) -> None:
    """A path not under ``/work/`` is already a host path and passes through."""
    workspace = tmp_path / "nxp"
    host_path = str(tmp_path / "some" / "host" / "log.do_compile")

    assert translate_container_path(host_path, workspace) == host_path
    # A leading "/workspace" must not be mistaken for the "/work/" prefix.
    assert translate_container_path("/workspace/foo", workspace) == "/workspace/foo"


@pytest.mark.unit
def test_find_recipe_log_returns_translated_host_path(tmp_path: Path) -> None:
    """``_find_recipe_log`` resolves the Logfile hint to the host path.

    The kas.log names an in-container ``/work/...`` path; the target file is
    created at the translated host location so the ``host_path.is_file()``
    gate passes and the host path is returned.
    """
    workspace = tmp_path / "nxp"
    container_path = "/work/build/tmp/deploy/linux-imx/temp/log.do_compile"
    expected = Path(translate_container_path(container_path, workspace))
    expected.parent.mkdir(parents=True)
    expected.write_text("do_compile failed\n")

    kas_log = tmp_path / "kas.log"
    kas_log.write_text(f"NOTE: Executing Tasks\nERROR: Logfile of failure stored in: {container_path}\n")

    result = _find_recipe_log(kas_log, workspace)

    assert result == expected
    assert result is not None
    assert str(result).startswith(str(workspace) + "/")
    assert not str(result).startswith("/work/")


# --- translation against the run's recorded mount root -----------------------

_RUN = "20260101-000000-1"


def _make_run(runs_dir: Path, *, work_mount: Path | None, logfile: str | None) -> Path:
    run_dir = runs_dir / _RUN
    run_dir.mkdir(parents=True)
    lines: list[dict[str, str]] = [{"event": "step_fail", "step": "kas_build", "error": "boom"}]
    if work_mount is not None:
        lines.append(
            {
                "event": "build_config",
                "family": "generic",
                "machine": "m",
                "mode": "container",
                "work_mount": str(work_mount),
            }
        )
    (run_dir / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))
    if logfile is not None:
        failures = [{"recipe": "r", "task": "do_compile", "logfile": logfile}]
        (run_dir / "bitbake-events.json").write_text(json.dumps({"failures": failures}))
    return run_dir


def _write_log(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("first\nTAIL-MARKER\n")


def _registered(monkeypatch: pytest.MonkeyPatch, run_dir: Path) -> None:
    monkeypatch.setattr(triage_cmd, "_registered_run_dir", lambda _rid: run_dir)
    monkeypatch.setattr(triage_cmd, "_find_workspace_from_cwd", lambda: None)


@pytest.mark.unit
def test_registered_meta_avocado_run_json_uses_recorded_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    log = ws / "build-s/build/tmp/work/r/temp/log.do_compile"
    _write_log(log)
    run_dir = _make_run(
        ws / "build-s/build/runs", work_mount=ws, logfile="/work/build-s/build/tmp/work/r/temp/log.do_compile"
    )
    _registered(monkeypatch, run_dir)

    result = CliRunner().invoke(app, ["triage", _RUN, "--json"])

    doc = json.loads(result.stdout)
    assert doc["recipe_log"] == str(log)
    assert "TAIL-MARKER" in doc["recipe_log_tail"]
    assert doc["recipe_log_note"] is None


@pytest.mark.unit
def test_registered_meta_avocado_run_text_uses_recorded_mount(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = tmp_path / "ws"
    _write_log(ws / "build-s/build/tmp/work/r/temp/log.do_compile")
    failures = [{"recipe": "r", "task": "do_compile", "logfile": "/work/build-s/build/tmp/work/r/temp/log.do_compile"}]

    triage_cmd._print_structured_failures(failures, ws / "build-s", [], ws)

    assert "TAIL-MARKER" in capsys.readouterr().err


@pytest.mark.unit
def test_nxp_workspace_run_translates_against_recorded_nxp_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    log = ws / "nxp/build/tmp/work/r/temp/log.do_compile"
    _write_log(log)
    _make_run(ws / "nxp/build/runs", work_mount=ws / "nxp", logfile="/work/build/tmp/work/r/temp/log.do_compile")
    monkeypatch.setattr(triage_cmd, "_registered_run_dir", lambda _rid: None)

    result = CliRunner().invoke(app, ["triage", _RUN, "--json", "--workspace", str(ws)])

    assert json.loads(result.stdout)["recipe_log"] == str(log)


@pytest.mark.unit
def test_registered_run_kas_log_hint_uses_recorded_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    log = ws / "build-s/build/tmp/work/r/temp/log.do_compile"
    _write_log(log)
    run_dir = _make_run(ws / "build-s/build/runs", work_mount=ws, logfile=None)
    (run_dir / "kas.log").write_text(
        "ERROR: Logfile of failure stored in: /work/build-s/build/tmp/work/r/temp/log.do_compile\n"
    )

    report = analyse(run_dir, ws / "build-s", translate_root=ws)
    assert report.recipe_log == log

    _registered(monkeypatch, run_dir)
    result = CliRunner().invoke(app, ["triage", _RUN, "--json"])
    assert json.loads(result.stdout)["recipe_log"] == str(log)


@pytest.mark.unit
def test_kas_yaml_run_without_events_json_uses_recorded_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``-k`` shape: report_root is ``<ws>/build-s`` while ``/work/`` maps to ``<ws>``.

    With no bitbake-events.json, ``analyse`` falls back to the kas.log hint. It
    translates against the root it is handed, so passing it ``report_root``
    doubled the ``build-s`` segment and the hint resolved to None.
    """
    ws = tmp_path / "ws"
    build_root = ws / "build-s"
    log = build_root / "build/tmp/work/r/temp/log.do_compile"
    _write_log(log)
    run_dir = _make_run(build_root / "build/runs", work_mount=ws, logfile=None)
    (run_dir / "kas.log").write_text(
        "ERROR: Logfile of failure stored in: /work/build-s/build/tmp/work/r/temp/log.do_compile\n"
    )
    monkeypatch.setattr(
        triage_cmd,
        "_resolve_triage_dirs",
        lambda _kas_yaml, _workspace: ([(build_root / "build/runs", "generic")], build_root, "label", ws),
    )

    result = CliRunner().invoke(app, ["triage", _RUN, "--json"])

    assert json.loads(result.stdout)["recipe_log"] == str(log)


@pytest.mark.unit
def test_recorded_mount_not_containing_run_is_ignored(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    run_dir = _make_run(ws / "build-s/build/runs", work_mount=tmp_path / "elsewhere", logfile=None)

    fallback = ws / "build-s"
    assert triage_cmd._translation_root_for(run_dir, fallback) == fallback


@pytest.mark.unit
def test_recorded_mount_containing_run_is_used(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    run_dir = _make_run(ws / "build-s/build/runs", work_mount=ws, logfile=None)

    assert triage_cmd._translation_root_for(run_dir, ws / "build-s") == ws


@pytest.mark.unit
def test_root_mount_cannot_widen_containment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    outside = tmp_path / "outside.log"
    outside.write_text("secret\n")
    run_dir = _make_run(ws / "build-s/build/runs", work_mount=Path("/"), logfile=f"/work{outside}")
    _registered(monkeypatch, run_dir)

    result = CliRunner().invoke(app, ["triage", _RUN, "--json"])

    doc = json.loads(result.stdout)
    assert doc["recipe_log_tail"] == []
    assert doc["recipe_log_note"] == "logfile outside workspace, not read"
    direct = read_failure_log(f"/work{outside}", ws / "build-s", translate_root=Path("/"))
    assert direct.state == "outside"
    assert direct.tail == []


@pytest.mark.unit
def test_no_translate_root_behaves_as_before(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _write_log(ws / "build/tmp/log.do_compile")

    log = read_failure_log("/work/build/tmp/log.do_compile", ws)

    assert log.state == "read"
    assert log.path == ws / "build/tmp/log.do_compile"
    run_dir = _make_run(ws / "build/runs", work_mount=None, logfile=None)
    assert triage_cmd._translation_root_for(run_dir, ws) == ws
