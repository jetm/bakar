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
    """``--runs 4`` propagates to the ``runs`` field of step_stress_parse.run's context.

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
    assert mock_run.call_args.kwargs["ctx"].runs == 4


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
    assert mock_run.call_args.kwargs["ctx"].runs == 10


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
    assert mock_run.call_args.kwargs["ctx"].target == "core-image-minimal"


# ---------------------------------------------------------------------------
# BYO (bring-your-own kas YAML) dispatch
#
# Mirrors ``bakar build``'s BYO form: a positional kas YAML instead of
# ``--manifest``, for generic workspaces (e.g. meta-avocado) with no
# manifest to dispatch a BSP family from.
# ---------------------------------------------------------------------------


def _make_generic_yaml(tmp_path: Path) -> Path:
    """A kas YAML with no NXP/TI machine signal -> dispatches to 'generic'."""
    yaml_path = tmp_path / "qemux86-64.yml"
    yaml_path.write_text("machine: qemux86-64\n")
    return yaml_path


def test_stress_parse_byo_positional_kas_yaml_dispatches_without_manifest(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A positional kas YAML (no ``--manifest``) resolves via generic dispatch.

    Regression: before this, omitting ``--manifest`` fell through to the NXP
    manifest default and crashed deep in ``kas.py:parse_manifest`` with a
    ``FileNotFoundError`` on a nonexistent ``.repo/manifests/imx-*.xml``.
    """
    workspace = _make_workspace(tmp_path)
    monkeypatch.chdir(workspace)
    kas_yaml = _make_generic_yaml(workspace)
    override_p, kas_p, stress_p = _patch_steps(summary=_clean_summary(1))

    with override_p as mock_apply, kas_p as mock_regen, stress_p as mock_run:
        result = runner.invoke(app, ["stress-parse", "--runs", "1", str(kas_yaml)])

    assert result.exit_code == 0, result.output
    assert mock_run.call_count == 1
    # generic mode skips both the bitbake override and the manifest-driven
    # kas YAML regeneration - the user's YAML is already the source.
    assert mock_apply.call_count == 0
    assert mock_regen.call_count == 0


def test_stress_parse_byo_and_manifest_together_exits_2(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing both a positional kas YAML and ``--manifest`` is rejected."""
    workspace = _make_workspace(tmp_path)
    monkeypatch.chdir(workspace)
    kas_yaml = _make_generic_yaml(workspace)
    override_p, kas_p, stress_p = _patch_steps(summary=_clean_summary(1))

    with override_p as mock_apply, kas_p as mock_regen, stress_p as mock_run:
        result = runner.invoke(
            app,
            ["stress-parse", str(kas_yaml), "--manifest", "imx-6.6.52-2.2.2.xml"],
        )

    assert result.exit_code == 2, result.output
    assert "choose either a positional kas YAML or --manifest, not both" in result.output
    assert mock_apply.call_count == 0
    assert mock_regen.call_count == 0
    assert mock_run.call_count == 0


def test_stress_parse_byo_colon_overlay_reaches_extra_overlays(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main.yml:overlay.yml`` threads the overlay into the step context's extra_overlays.

    Mirrors the ``bakar bitbake`` colon-overlay wiring test
    (``tests/test_kas_colon_overlay.py::test_bitbake_colon_arg_extra_overlay_in_ctx``):
    the user-supplied overlay must actually reach the step call, not just
    parse without error.
    """
    workspace = _make_workspace(tmp_path)
    monkeypatch.chdir(workspace)
    kas_yaml = _make_generic_yaml(workspace)
    overlay = workspace / "bringup.yml"
    overlay.write_text("header:\n  version: 14\n")
    kas_arg = f"{kas_yaml}:{overlay}"

    override_p, kas_p, stress_p = _patch_steps(summary=_clean_summary(1))

    with override_p, kas_p, stress_p as mock_run:
        result = runner.invoke(app, ["stress-parse", "--runs", "1", kas_arg])

    assert result.exit_code == 0, result.output
    extra_overlays = mock_run.call_args.kwargs["ctx"].extra_overlays
    assert any(p.resolve() == overlay.resolve() for p in extra_overlays), (
        f"user overlay not found in extra_overlays: {extra_overlays!r}"
    )


# ---------------------------------------------------------------------------
# Context packing: the command's eleven flags, and the ten values it hands
# the step. Nothing else in this file would notice a field transposed or
# dropped while packing either one.
# ---------------------------------------------------------------------------


def _fake_python(tmp_path: Path) -> Path:
    """An executable file ``--python`` will accept."""
    exe = tmp_path / "ctx-python"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


# Every command flag paired with the _StressParseCtx field it must land in,
# and the value it carries. Drives both command-context tests below.
_CTX_FLAG_CASES: list[tuple[list[str], str, object]] = [
    (["ctx-machine.yml"], "kas_yaml", "ctx-machine.yml"),
    (["--runs", "7"], "runs", 7),
    (["--target", "ctx-target"], "target", "ctx-target"),
    (["--parse-threads", "3"], "parse_threads", 3),
    (["--machine", "ctx-machine"], "machine", "ctx-machine"),
    (["--image", "ctx-image"], "image", "ctx-image"),
    (["--manifest", "ctx-manifest.xml"], "manifest", "ctx-manifest.xml"),
    (["--branch", "ctx-branch"], "branch", "ctx-branch"),
    (["--label", "ctx-label"], "label", "ctx-label"),
]

_CTX_DEFAULTS: dict[str, object] = {
    "kas_yaml": None,
    "runs": 10,
    "target": "world",
    "parse_threads": None,
    "machine": None,
    "image": None,
    "manifest": None,
    "branch": None,
    "workspace": None,
    "label": None,
    "python": None,
}


def test_stress_parse_ctx_carries_every_flag_unchanged(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every _StressParseCtx field equals the flag ``stress_parse()`` was invoked with.

    Packing eleven CLI parameters into a context is wrong in a way nothing
    else here sees: transpose two fields or drop one and the command still
    runs, ``--help`` is unchanged, and every other test stays green. This one
    captures the context object and compares it field-by-field against a flag
    set where no value is the default.
    """
    import dataclasses

    import bakar.commands.stress_parse as sp

    workspace = _make_workspace(tmp_path)
    monkeypatch.chdir(workspace)
    exe = _fake_python(tmp_path)

    captured: list[sp._StressParseCtx] = []
    monkeypatch.setattr(sp, "_stress_parse_impl", captured.append)

    argv = ["stress-parse"]
    expected: dict[str, object] = {}
    for flag, field, value in _CTX_FLAG_CASES:
        argv += flag
        expected[field] = value
    argv += ["--workspace", str(workspace), "--python", str(exe)]
    expected["workspace"] = workspace.resolve()
    expected["python"] = exe

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert len(captured) == 1, f"expected one ctx, got {len(captured)}"
    ctx = captured[0]
    for field, want in expected.items():
        got = getattr(ctx, field)
        assert got == want, f"_StressParseCtx.{field}: expected {want!r}, got {got!r}"
    # Guards against a field being added to the dataclass but left unasserted.
    assert {f.name for f in dataclasses.fields(ctx)} == set(expected)


@pytest.mark.parametrize("case", _CTX_FLAG_CASES, ids=lambda c: c[1])
def test_stress_parse_ctx_one_flag_moves_one_field(
    case: tuple[list[str], str, object],
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One flag at a time moves exactly one _StressParseCtx field.

    The all-flags test above cannot catch a transposition between two fields
    that share a default - nine of these eleven default to ``None``, so a
    swapped pair reads as ``None`` on both sides whenever neither flag is
    passed. Setting one flag per invocation pins each field to its own flag
    and asserts its neighbours are still at their defaults.
    """
    import bakar.commands.stress_parse as sp

    flag, field, value = case
    workspace = _make_workspace(tmp_path)
    monkeypatch.chdir(workspace)

    captured: list[sp._StressParseCtx] = []
    monkeypatch.setattr(sp, "_stress_parse_impl", captured.append)

    result = runner.invoke(app, ["stress-parse", *flag])
    assert result.exit_code == 0, result.output

    expected = {**_CTX_DEFAULTS, field: value}
    ctx = captured[0]
    for name, want in expected.items():
        got = getattr(ctx, name)
        assert got == want, f"{flag} -> _StressParseCtx.{name}: expected {want!r}, got {got!r}"


# Flags that pass through the command into StressParseContext, paired with the
# step-context field they must land in.
_STEP_FLAG_CASES: list[tuple[list[str], str, object]] = [
    (["--runs", "7"], "runs", 7),
    (["--target", "ctx-target"], "target", "ctx-target"),
    (["--parse-threads", "3"], "parse_threads", 3),
    (["--label", "ctx-label"], "label", "ctx-label"),
]

_STEP_DEFAULTS: dict[str, object] = {
    "runs": 10,
    "target": "world",
    "parse_threads": None,
    "label": None,
    "python_executable": None,
}


def test_stress_parse_step_context_carries_every_value(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every StressParseContext field the command fills matches what was asked for.

    The command packs ten values for the step. ``cfg``, ``log``, ``bsp`` and
    ``overlay_source`` are derived rather than passed, so they are asserted
    through the values they carry; the pass-through values are asserted
    verbatim.
    """
    import dataclasses

    from bakar.steps.stress_parse import StressParseContext

    workspace = _make_workspace(tmp_path)
    monkeypatch.chdir(workspace)
    exe = _fake_python(tmp_path)

    override_p, kas_p, stress_p = _patch_steps(summary=_clean_summary(7))
    with override_p, kas_p, stress_p as mock_run:
        result = runner.invoke(
            app,
            [
                "stress-parse",
                "--manifest",
                "imx-6.6.52-2.2.2.xml",
                "--machine",
                "ctx-machine",
                "--image",
                "ctx-image",
                "--runs",
                "7",
                "--target",
                "ctx-target",
                "--parse-threads",
                "3",
                "--label",
                "ctx-label",
                "--python",
                str(exe),
            ],
        )

    assert result.exit_code == 0, result.output
    ctx = mock_run.call_args.kwargs["ctx"]
    assert isinstance(ctx, StressParseContext)

    assert ctx.runs == 7
    assert ctx.target == "ctx-target"
    assert ctx.parse_threads == 3
    assert ctx.label == "ctx-label"
    assert ctx.python_executable == exe.resolve()
    assert ctx.extra_overlays is not None
    # Derived values: the command resolves these rather than passing them through.
    assert ctx.cfg.machine == "ctx-machine"
    assert ctx.cfg.image == "ctx-image"
    assert ctx.cfg.manifest == "imx-6.6.52-2.2.2.xml"
    assert ctx.log is not None
    assert ctx.overlay_source.name.startswith("bakar-tuning")
    # Guards against a field being added to the step context but left unasserted.
    assert {f.name for f in dataclasses.fields(ctx)} == {
        "cfg",
        "log",
        "overlay_source",
        "runs",
        "target",
        "parse_threads",
        "extra_overlays",
        "label",
        "python_executable",
    }


@pytest.mark.parametrize("case", _STEP_FLAG_CASES, ids=lambda c: c[1])
def test_stress_parse_step_context_one_flag_moves_one_field(
    case: tuple[list[str], str, object],
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One flag at a time moves exactly one pass-through StressParseContext field.

    Three of these five default to ``None``, so the all-values test above
    cannot see a transposition between two of them while both are unset.
    """
    flag, field, value = case
    workspace = _make_workspace(tmp_path)
    monkeypatch.chdir(workspace)

    override_p, kas_p, stress_p = _patch_steps(summary=_clean_summary(1))
    with override_p, kas_p, stress_p as mock_run:
        result = runner.invoke(app, ["stress-parse", "--manifest", "imx-6.6.52-2.2.2.xml", *flag])

    assert result.exit_code == 0, result.output
    ctx = mock_run.call_args.kwargs["ctx"]
    expected = {**_STEP_DEFAULTS, field: value}
    for name, want in expected.items():
        got = getattr(ctx, name)
        assert got == want, f"{flag} -> StressParseContext.{name}: expected {want!r}, got {got!r}"
