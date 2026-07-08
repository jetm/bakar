"""Tests for the ``bakar stress-parse`` command.

The CLI handler in ``bakar.commands.stress_parse`` does not call
``subprocess.run`` directly and does not spawn parallel workers; it
dispatches sequentially to three step modules:

* ``step_override.apply`` - applies the bitbake override
* ``step_kas.regenerate_yaml`` - regenerates the kas YAML
* ``step_stress_parse.run`` - the actual loop of N ``bitbake -p`` runs

The race-detection signal lives in the summary dict returned by
``step_stress_parse.run`` (``summary["failed"]`` > 0 + ``failure_signatures``
populated with one of ``FORK_RACE_SIGNATURES``). The CLI inspects that
dict to decide the exit code, so the tests patch the step modules at the
handler's import site and assert against the CLI exit code and output.

The ``--runs/-n`` flag controls iteration count; the default is 10. The
task prompt referred to "parallel invocations" / ``--parallel``; the
implementation is sequential and the flag is ``--runs``, so the tests
exercise sequential dispatch instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from bakar.cli import app
from bakar.fork_race_signatures import FORK_RACE_SIGNATURES

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner

pytestmark = pytest.mark.unit


def _make_workspace(tmp_path: Path) -> Path:
    """Workspace with a ``.bakar.toml`` marker so ``_workspace_from_cwd`` resolves it."""
    (tmp_path / ".bakar.toml").write_text("")
    return tmp_path


def _clean_summary(runs: int) -> dict:
    """Return a stress-parse summary representing ``runs`` clean iterations."""
    return {
        "bsp_family": "nxp",
        "manifest": "imx-6.6.52-2.2.2.xml",
        "machine": "imx95-var-dart",
        "image": "core-image-minimal",
        "target": "world",
        "runs": runs,
        "passed": runs,
        "failed": 0,
        "elapsed_seconds": [1.0] * runs,
        "exit_codes": [0] * runs,
        "cache_cleared_pre_iter": [False] * runs,
        "runtime_cleared_pre_iter": [False] * runs,
        "override": {
            "state": "active",
            "branch": "br-2.12",
            "sha": "d092d2436",
            "upstream_version": "2.12.1",
            "bsp_version": "2.12.1",
        },
        "env": {},
        "failure_signatures": [],
    }


def _failing_summary(runs: int, fail_index: int = 1) -> dict:
    """Summary with one iteration tripping a real FORK_RACE_SIGNATURES pattern.

    Picks a signature from the canonical list (``parser thread killed/died``
    by default) so the test would break if either the signature regex or the
    CLI's failure-handling branch regressed.
    """
    summary = _clean_summary(runs)
    summary["passed"] = runs - 1
    summary["failed"] = 1
    # Use a literal pattern from FORK_RACE_SIGNATURES so the matched line
    # is something a real scan would produce.
    pattern = next(p.pattern for p in FORK_RACE_SIGNATURES if p.pattern == r"parser thread killed/died")
    summary["failure_signatures"] = [
        {
            "run": fail_index,
            "pattern": pattern,
            "match": "ERROR: parser thread killed/died after fork",
        }
    ]
    return summary


def _patch_steps(
    *,
    summary: dict,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Return the patch context managers for the three step boundary calls.

    The CLI handler imports the step modules as ``step_override``,
    ``step_kas``, and ``step_stress_parse``; patching at those attribute
    paths scopes the fakes to this command without leaking across other
    test modules.
    """
    override_patcher = patch("bakar.commands.stress_parse.step_override.apply", return_value=None)
    kas_patcher = patch("bakar.commands.stress_parse.step_kas.regenerate_yaml", return_value=None)
    stress_patcher = patch(
        "bakar.commands.stress_parse.step_stress_parse.run",
        return_value=summary,
    )
    return override_patcher, kas_patcher, stress_patcher


def test_stress_parse_runs_count_matches_flag(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--runs 4`` propagates to step_stress_parse.run's ``runs=`` kwarg.

    Sequential dispatch: the CLI calls the step once with ``runs=N``. The
    step itself is the loop, so the assertion is on the kwarg, not on call
    count.
    """
    workspace = _make_workspace(tmp_path)
    monkeypatch.chdir(workspace)
    override_p, kas_p, stress_p = _patch_steps(summary=_clean_summary(4))

    with override_p, kas_p, stress_p as mock_run:
        result = runner.invoke(app, ["stress-parse", "--runs", "4", "--manifest", "imx-6.6.52-2.2.2.xml"])

    assert result.exit_code == 0, result.output
    assert mock_run.call_count == 1, f"expected one dispatch to step.run, got {mock_run.call_count}"
    assert mock_run.call_args.kwargs["runs"] == 4


def test_stress_parse_default_runs_is_ten(runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``--runs`` flag means the documented default of 10 iterations.

    The default is a contract: scripts that pin behaviour by omission would
    break silently if the value drifted, so it gets its own assertion.
    """
    workspace = _make_workspace(tmp_path)
    monkeypatch.chdir(workspace)
    override_p, kas_p, stress_p = _patch_steps(summary=_clean_summary(10))

    with override_p, kas_p, stress_p as mock_run:
        result = runner.invoke(app, ["stress-parse", "--manifest", "imx-6.6.52-2.2.2.xml"])

    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["runs"] == 10


def test_stress_parse_race_signature_exits_nonzero(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A summary with ``failed > 0`` and a FORK_RACE_SIGNATURES hit must exit 1.

    The CLI prints the failing-run line for each signature and raises
    ``typer.Exit(code=1)``. The asserted output mention of the matched
    line proves the failure branch ran, not just any non-zero exit.
    """
    workspace = _make_workspace(tmp_path)
    monkeypatch.chdir(workspace)
    summary = _failing_summary(runs=3, fail_index=2)
    override_p, kas_p, stress_p = _patch_steps(summary=summary)

    with override_p, kas_p, stress_p:
        result = runner.invoke(
            app,
            ["stress-parse", "--runs", "3", "--manifest", "imx-6.6.52-2.2.2.xml"],
        )

    assert result.exit_code == 1, result.output
    # The summary table renders the failure count and the per-signature line
    # carries the run index plus the matched text. Both must appear.
    assert "run 2" in result.output
    assert "parser thread killed/died" in result.output


def test_stress_parse_clean_run_exits_zero(runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A summary with ``failed=0`` exits 0 and reports the pass count.

    Falsifier: if the CLI accidentally exited non-zero on a clean summary
    (e.g. inverted comparison), the assertion would catch it; if it failed
    to render the summary table, the pass-count assertion would catch it.
    """
    workspace = _make_workspace(tmp_path)
    monkeypatch.chdir(workspace)
    override_p, kas_p, stress_p = _patch_steps(summary=_clean_summary(5))

    with override_p, kas_p, stress_p:
        result = runner.invoke(
            app,
            ["stress-parse", "--runs", "5", "--manifest", "imx-6.6.52-2.2.2.xml"],
        )

    assert result.exit_code == 0, result.output
    # The Rich summary table renders the passed count.
    assert "5" in result.output
    assert "passed" in result.output


def test_stress_parse_invalid_runs_rejected(runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--runs 0`` is rejected before any step dispatch.

    The validation guard at the top of the handler exits 2 with a message
    naming the constraint. None of the three step modules should be called.
    """
    workspace = _make_workspace(tmp_path)
    monkeypatch.chdir(workspace)
    override_p, kas_p, stress_p = _patch_steps(summary=_clean_summary(1))

    with override_p as mock_apply, kas_p as mock_yaml, stress_p as mock_run:
        result = runner.invoke(
            app,
            ["stress-parse", "--runs", "0", "--manifest", "imx-6.6.52-2.2.2.xml"],
        )

    assert result.exit_code == 2, result.output
    assert "--runs must be >= 1" in result.output
    assert mock_apply.call_count == 0
    assert mock_yaml.call_count == 0
    assert mock_run.call_count == 0


def test_stress_parse_target_flag_forwarded(runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--target <name>`` overrides the default 'world' on the step call.

    Confirms the option-to-kwarg wiring; a typo in the Annotated
    declaration or in the step call would surface here.
    """
    workspace = _make_workspace(tmp_path)
    monkeypatch.chdir(workspace)
    override_p, kas_p, stress_p = _patch_steps(summary=_clean_summary(1))

    with override_p, kas_p, stress_p as mock_run:
        result = runner.invoke(
            app,
            [
                "stress-parse",
                "--runs",
                "1",
                "--target",
                "core-image-minimal",
                "--manifest",
                "imx-6.6.52-2.2.2.xml",
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["target"] == "core-image-minimal"
