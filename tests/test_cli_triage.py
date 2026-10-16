"""Tests for the ``bakar triage`` command.

These are Category A CliRunner tests for ``bakar.commands.triage``. The
workspace layout mirrors what the production code expects:
``find_runs`` / ``_find_run`` walk ``<workspace>/{nxp,ti}/build/runs/``,
not ``<workspace>/build/runs/`` directly. The shared ``fake_run_dir``
fixture writes under ``<tmp>/build/runs/<ts>/`` so this file builds its
own run dirs under ``<workspace>/nxp/build/runs/`` to match the discovery
rules. ``SAMPLE_EVENTS_JSONL`` and ``SAMPLE_KAS_LOG`` from conftest are
reused verbatim so the failing-step / recipe-log assertions stay in sync
with the rest of the suite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.cli import app
from tests.conftest import SAMPLE_EVENTS_JSONL, SAMPLE_KAS_LOG

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point HOME at an empty dir so the CLI callback never reads the real ``~/.config/bakar``."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


# Events file with only step_start/step_end (no step_fail). analyse() must
# leave ``failing_step=None`` so the command renders the "no step_fail
# events found" branch and exits 0.
CLEAN_EVENTS_JSONL = (
    '{"event": "step_start", "step": "kas-build", "ts": "2026-05-29T12:00:00Z"}\n'
    '{"event": "step_end", "step": "kas-build", "ts": "2026-05-29T12:05:00Z"}\n'
)


def _make_workspace(tmp_path: Path) -> Path:
    """Workspace with a ``.bakar.toml`` marker so ``_workspace_from_cwd`` picks it up."""
    (tmp_path / ".bakar.toml").write_text("")
    return tmp_path


def _make_run(workspace: Path, ts: str, events: str, kas_log: str) -> Path:
    """Build a run dir under ``<workspace>/nxp/build/runs/<ts>/`` (the layout ``find_runs`` walks)."""
    run = workspace / "nxp" / "build" / "runs" / ts
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(events)
    (run / "kas.log").write_text(kas_log)
    return run


def test_triage_failing_run_surfaces_step_name(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run dir with a ``step_fail`` event must surface that step name in the output."""
    workspace = _make_workspace(tmp_path)
    _make_run(workspace, "20260529-120000", SAMPLE_EVENTS_JSONL, SAMPLE_KAS_LOG)
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["triage"])

    assert result.exit_code == 0, result.output
    # "kas-build" is the step name from SAMPLE_EVENTS_JSONL's step_fail.
    assert "kas-build" in result.output


def test_triage_recipe_error_mentions_recipe_name(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recipe-level ``ERROR: ... do_compile`` line must surface the recipe (linux-imx)."""
    workspace = _make_workspace(tmp_path)
    _make_run(workspace, "20260529-120000", SAMPLE_EVENTS_JSONL, SAMPLE_KAS_LOG)
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["triage"])

    assert result.exit_code == 0, result.output
    assert "linux-imx" in result.output


def test_triage_clean_run_reports_no_step_fail(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run with no ``step_fail`` events exits 0 and prints the no-failures message."""
    workspace = _make_workspace(tmp_path)
    _make_run(workspace, "20260529-120000", CLEAN_EVENTS_JSONL, "NOTE: clean run, no errors\n")
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["triage"])

    assert result.exit_code == 0, result.output
    assert "no step_fail events found" in result.output


def test_triage_reports_a_hung_post_build_capture_distinctly_from_a_clean_run(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose build succeeded and whose post-build graph capture then
    hung must NOT render the same "no step_fail events found" message a
    genuinely clean run gets - that line is false of a file that plainly
    contains one, and it would hide the hang from the operator."""
    workspace = _make_workspace(tmp_path)
    events = (
        '{"event": "step_start", "step": "kas_build", "ts": "2026-05-29T13:00:00Z"}\n'
        '{"event": "step_ok", "step": "kas_build", "ts": "2026-05-29T13:05:00Z"}\n'
        '{"event": "step_fail", "step": "graph_capture", '
        '"reason": "no exit after 900s", "ts": "2026-05-29T13:20:00Z"}\n'
    )
    _make_run(workspace, "20260529-130000", events, "NOTE: Tasks Summary: Attempted 1 tasks, 0 failed.\n")
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["triage"])

    assert result.exit_code == 0, result.output
    assert "no step_fail events found" not in result.output
    assert "graph_capture" in result.output
    assert "no exit after 900s" in result.output


def test_triage_explicit_run_id_selects_named_run(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two run dirs side by side; an explicit older run id must select that run, not the newer auto-pick."""
    workspace = _make_workspace(tmp_path)
    older = "20260101-000000"
    newer = "20260601-000000"
    # Newer run carries a step_fail; older run is clean. Asking for the
    # older id by name must select the clean run (no failing step in
    # output) rather than the newer auto-selected failing run.
    _make_run(workspace, older, CLEAN_EVENTS_JSONL, "NOTE: older run, clean\n")
    _make_run(workspace, newer, SAMPLE_EVENTS_JSONL, SAMPLE_KAS_LOG)
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["triage", older])

    assert result.exit_code == 0, result.output
    assert older in result.output
    # The clean run has no step_fail; the success branch must fire.
    assert "no step_fail events found" in result.output


def test_triage_no_runs_directory_exits_nonzero(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace with no ``build/runs/`` at all must exit non-zero with a helpful message."""
    workspace = _make_workspace(tmp_path)
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["triage"])

    assert result.exit_code != 0
    assert "No runs found" in result.output


def test_triage_explicit_unknown_run_id_exits_nonzero(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit run id that does not match any run dir must exit non-zero."""
    workspace = _make_workspace(tmp_path)
    _make_run(workspace, "20260529-120000", SAMPLE_EVENTS_JSONL, SAMPLE_KAS_LOG)
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["triage", "99990101-000000"])

    assert result.exit_code != 0
    assert "99990101-000000" in result.output


# ---------------------------------------------------------------------------
# --json flag tests
# ---------------------------------------------------------------------------


def _parse_json_output(output: str) -> dict:
    """Extract the JSON object from triage output.

    ``bakar triage`` always emits a ``:: triage <run_id>`` header line via
    Rich before the JSON payload. Strip everything up to the first ``{``.
    """
    import json

    json_start = output.index("{")
    return json.loads(output[json_start:])


def test_triage_json_unstructured_failing_run(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` on an unstructured failing run emits valid JSON with the failing step."""
    workspace = _make_workspace(tmp_path)
    _make_run(workspace, "20260529-120000", SAMPLE_EVENTS_JSONL, SAMPLE_KAS_LOG)
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["triage", "--json"])

    assert result.exit_code == 0, result.output
    doc = _parse_json_output(result.output)
    assert doc["version"] == 1
    assert doc["run_id"] == "20260529-120000"
    assert doc["failing_step"] is not None
    assert "suggestions" in doc


def test_triage_json_unstructured_clean_run(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` on an unstructured clean run emits JSON with ``failing_step`` null."""
    workspace = _make_workspace(tmp_path)
    _make_run(workspace, "20260529-120000", CLEAN_EVENTS_JSONL, "NOTE: clean run\n")
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["triage", "--json"])

    assert result.exit_code == 0, result.output
    doc = _parse_json_output(result.output)
    assert doc["version"] == 1
    assert doc["failing_step"] is None
    assert doc["fail_reason"] is None
    assert doc["suggestions"] == []


def test_triage_json_structured_failing_run(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` on a run with ``bitbake-events.json`` emits the structured fields."""
    import json

    workspace = _make_workspace(tmp_path)
    run = _make_run(workspace, "20260529-120000", SAMPLE_EVENTS_JSONL, SAMPLE_KAS_LOG)
    (run / "bitbake-events.json").write_text(
        json.dumps(
            {
                "failures": [
                    {
                        "recipe": "linux-imx",
                        "task": "do_compile",
                        "logfile": "/work/build/tmp/log/linux-imx.log",
                        "errprinted": True,
                    }
                ]
            }
        )
    )
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["triage", "--json"])

    assert result.exit_code == 0, result.output
    doc = _parse_json_output(result.output)
    assert doc["version"] == 1
    assert doc["run_id"] == "20260529-120000"
    assert doc["failing_step"] == "linux-imx:do_compile"
    assert doc["fail_reason"] is None
    assert doc["recipe_log"] == "/work/build/tmp/log/linux-imx.log"
    assert doc["suggestions"] == []


def test_triage_json_structured_clean_run(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--json`` with an empty ``failures[]`` in bitbake-events.json returns all nulls."""
    import json

    workspace = _make_workspace(tmp_path)
    run = _make_run(workspace, "20260529-120000", CLEAN_EVENTS_JSONL, "NOTE: clean\n")
    (run / "bitbake-events.json").write_text(json.dumps({"failures": []}))
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["triage", "--json"])

    assert result.exit_code == 0, result.output
    doc = _parse_json_output(result.output)
    assert doc["version"] == 1
    assert doc["failing_step"] is None
    assert doc["fail_reason"] is None
    assert doc["recipe_log"] is None
    assert doc["suggestions"] == []


def test_triage_json_short_flag(runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``-j`` is accepted as an alias for ``--json``."""
    workspace = _make_workspace(tmp_path)
    _make_run(workspace, "20260529-120000", CLEAN_EVENTS_JSONL, "NOTE: clean\n")
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["triage", "-j"])

    assert result.exit_code == 0, result.output
    doc = _parse_json_output(result.output)
    assert doc["version"] == 1


# ---------------------------------------------------------------------------
# Run selection: --preset / --run / --kas-yaml
# ---------------------------------------------------------------------------


def _make_fanout_run(workspace: Path, subdir: str, ts: str, events: str, kas_log: str) -> Path:
    """Build a preset fan-out run dir under ``<ws>/build/<subdir>/build/runs/<ts>/``."""
    run = workspace / "build" / subdir / "build" / "runs" / ts
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(events)
    (run / "kas.log").write_text(kas_log)
    return run


def _two_preset_workspace(tmp_path: Path) -> tuple[Path, str, str]:
    """Workspace whose default pick (newest failing run) is NOT the clean run.

    ``alpha`` holds the newer failing run, so a selector that is ignored lands on
    it; ``beta`` holds the older clean run that ``--preset beta`` / ``--run`` must reach.
    """
    workspace = _make_workspace(tmp_path)
    beta_ts = "20260101-000000"
    alpha_ts = "20260601-000000"
    _make_fanout_run(workspace, "distro-alpha-1.0", alpha_ts, SAMPLE_EVENTS_JSONL, SAMPLE_KAS_LOG)
    _make_fanout_run(workspace, "distro-beta-2.0", beta_ts, CLEAN_EVENTS_JSONL, "NOTE: beta run, clean\n")
    return workspace, alpha_ts, beta_ts


def test_triage_default_picks_the_newest_failing_fanout_run(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline for the selector tests: with no selector the failing alpha run wins."""
    workspace, alpha_ts, beta_ts = _two_preset_workspace(tmp_path)
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["triage"])

    assert result.exit_code == 0, result.output
    assert f"triage {alpha_ts}" in result.output
    assert beta_ts not in result.output


def test_triage_preset_selects_the_matching_fanout_dir(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--preset beta`` must pick the older clean beta run over the default failing alpha run."""
    workspace, alpha_ts, beta_ts = _two_preset_workspace(tmp_path)
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["triage", "--preset", "beta"])

    assert result.exit_code == 0, result.output
    assert f"triage {beta_ts}" in result.output
    assert alpha_ts not in result.output
    assert "no step_fail events found" in result.output


def test_triage_run_option_selects_the_named_fanout_run(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--run <id>`` must pick the older clean run, not the default failing one."""
    workspace, alpha_ts, beta_ts = _two_preset_workspace(tmp_path)
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["triage", "--run", beta_ts])

    assert result.exit_code == 0, result.output
    assert f"triage {beta_ts}" in result.output
    assert alpha_ts not in result.output
    assert "no step_fail events found" in result.output


def test_triage_kas_yaml_reads_the_runs_next_to_that_yaml(
    runner: _CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-k my.yml`` reads ``<yaml-parent>/build/runs`` and ignores the workspace's own runs."""
    (tmp_path / "ws").mkdir()
    workspace = _make_workspace(tmp_path / "ws")
    _make_run(workspace, "20260901-000000", CLEAN_EVENTS_JSONL, "NOTE: decoy workspace run\n")
    byo = tmp_path / "byo"
    run = byo / "build" / "runs" / "20260301-000000"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(SAMPLE_EVENTS_JSONL)
    (run / "kas.log").write_text(SAMPLE_KAS_LOG)
    kas_yaml = byo / "my.yml"
    kas_yaml.write_text("header:\n  version: 14\n")
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["triage", "-k", str(kas_yaml)])

    assert result.exit_code == 0, result.output
    assert "triage 20260301-000000" in result.output
    assert "20260901-000000" not in result.output
    assert "kas-build" in result.output
