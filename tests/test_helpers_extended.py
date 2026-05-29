"""Extended hermetic tests for ``bakar.commands._helpers``.

Targets the error/dispatch paths missed by the existing test suite: workspace
detection failure, manifest-precedence (CLI > env > default) inside
``_dispatch_bsp``, YAML dispatch (NXP / TI / generic / not-found / unparseable),
the cwd-based BSP family detection, the non-PASS diagnosis render, and the
run-directory lookup helper.

The task brief used educated-guess names; the actual surface is:

- ``_workspace_from_cwd`` (no marker) -> ``typer.Exit(2)``
- ``_workspace_from_cwd`` walks parents looking for ``.bakar.toml`` / ``nxp`` / ``ti``
- ``_dispatch_bsp(manifest_arg)`` inlines the CLI > env > default precedence
- ``_dispatch_from_yaml(yaml_path)`` reads the YAML and classifies it
- ``_bsp_from_cwd(workspace)`` returns ``nxp`` / ``ti`` based on cwd
- ``_print_diagnosis(results)`` renders the Rich diagnosis table
- ``_find_run(runs_dirs, run_id)`` resolves run directories across roots
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
import typer

from bakar.commands._helpers import (
    _bsp_from_cwd,
    _dispatch_bsp,
    _dispatch_from_yaml,
    _find_run,
    _print_diagnosis,
    _workspace_from_cwd,
)
from bakar.config import DEFAULT_NXP_MANIFEST
from bakar.diagnostics import CheckResult, Severity, Status

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _workspace_from_cwd
# ---------------------------------------------------------------------------


def test_workspace_from_cwd_no_marker_raises_typer_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no .bakar.toml / nxp/ / ti/ in cwd or any parent, exit(2)."""
    # Use an isolated subdirectory; tmp_path's ancestors may carry markers from
    # the host but a freshly created child cannot.
    isolated = tmp_path / "lonely"
    isolated.mkdir()
    monkeypatch.chdir(isolated)

    # Walk-up will still hit tmp_path parents; force the resolved cwd to a
    # path with no ancestors carrying markers by stubbing Path.cwd to return
    # the isolated dir alone via a path with no parent traversal helpers.
    # Simpler: assert exit is raised when no markers exist anywhere up to /tmp.
    # On the off chance an ancestor carries a marker we skip rather than fail.
    cur = isolated.resolve()
    for cand in (cur, *cur.parents):
        if (cand / ".bakar.toml").is_file() or (cand / "nxp").is_dir() or (cand / "ti").is_dir():
            pytest.skip(f"ancestor {cand} carries a BSP marker; cannot test no-marker path")

    with pytest.raises(typer.Exit) as exc:
        _workspace_from_cwd()
    assert exc.value.exit_code == 2


def test_workspace_from_cwd_finds_marker_by_walking_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Marker at tmp_path; chdir into a subdir; helper returns tmp_path."""
    (tmp_path / ".bakar.toml").write_text("")
    subdir = tmp_path / "build" / "deep" / "nested"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)

    result = _workspace_from_cwd()

    assert result.resolve() == tmp_path.resolve()


def test_workspace_from_cwd_finds_nxp_subdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Presence of an nxp/ subdir is enough to mark a workspace root."""
    (tmp_path / "nxp").mkdir()
    monkeypatch.chdir(tmp_path)

    result = _workspace_from_cwd()

    assert result.resolve() == tmp_path.resolve()


def test_workspace_from_cwd_finds_ti_subdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Presence of a ti/ subdir is enough to mark a workspace root."""
    (tmp_path / "ti").mkdir()
    monkeypatch.chdir(tmp_path)

    result = _workspace_from_cwd()

    assert result.resolve() == tmp_path.resolve()


# ---------------------------------------------------------------------------
# _dispatch_bsp - manifest precedence (CLI > env > default)
# ---------------------------------------------------------------------------


_VALID_TI_MANIFEST = "processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt"


def test_dispatch_bsp_explicit_arg_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit manifest_arg wins over BAKAR_MANIFEST env var."""
    monkeypatch.setenv("BAKAR_MANIFEST", _VALID_TI_MANIFEST)

    family, _model = _dispatch_bsp(manifest_arg="imx-6.6.52-2.2.2.xml")

    assert family == "nxp", "explicit NXP arg must override TI env var"


def test_dispatch_bsp_env_beats_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no arg, BAKAR_MANIFEST env var beats the NXP default."""
    monkeypatch.setenv("BAKAR_MANIFEST", _VALID_TI_MANIFEST)

    family, _model = _dispatch_bsp(manifest_arg=None)

    assert family == "ti", "env var must override the NXP default when no arg is given"


def test_dispatch_bsp_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no arg and no env, the NXP default manifest is used."""
    monkeypatch.delenv("BAKAR_MANIFEST", raising=False)

    family, _model = _dispatch_bsp(manifest_arg=None)

    # The default is an NXP manifest filename.
    assert family == "nxp"
    assert DEFAULT_NXP_MANIFEST.startswith("imx-")


def test_dispatch_bsp_unknown_shape_raises_typer_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A manifest filename matching neither NXP nor TI exits(2)."""
    monkeypatch.delenv("BAKAR_MANIFEST", raising=False)

    with pytest.raises(typer.Exit) as exc:
        _dispatch_bsp(manifest_arg="random-garbage-name.txt")

    assert exc.value.exit_code == 2


# ---------------------------------------------------------------------------
# _dispatch_from_yaml
# ---------------------------------------------------------------------------


def test_dispatch_from_yaml_missing_file_raises_typer_exit(tmp_path: Path) -> None:
    """A non-existent YAML path is rejected with exit(2)."""
    missing = tmp_path / "does-not-exist.yml"

    with pytest.raises(typer.Exit) as exc:
        _dispatch_from_yaml(missing)

    assert exc.value.exit_code == 2


def test_dispatch_from_yaml_nxp_machine(tmp_path: Path) -> None:
    """A YAML declaring an i.MX machine classifies as NXP."""
    yaml_path = tmp_path / "nxp.yml"
    yaml_path.write_text("header:\n  version: 14\nmachine: imx8mp-var-dart\n")

    family, model = _dispatch_from_yaml(yaml_path)

    assert family == "nxp"
    assert model is not None


def test_dispatch_from_yaml_ti_machine(tmp_path: Path) -> None:
    """A YAML declaring a TI Sitara machine classifies as TI."""
    yaml_path = tmp_path / "ti.yml"
    yaml_path.write_text("header:\n  version: 14\nmachine: am62xx-evm\n")

    family, model = _dispatch_from_yaml(yaml_path)

    assert family == "ti"
    assert model is not None


def test_dispatch_from_yaml_generic_machine_returns_none_model(tmp_path: Path) -> None:
    """A YAML with a non-NXP/TI machine classifies as generic; model is None."""
    yaml_path = tmp_path / "generic.yml"
    yaml_path.write_text("header:\n  version: 14\nmachine: qemux86-64\n")

    family, model = _dispatch_from_yaml(yaml_path)

    assert family == "generic"
    assert model is None


def test_dispatch_from_yaml_unparseable_raises_typer_exit(tmp_path: Path) -> None:
    """A YAML with neither machine: nor repos: is rejected as 'unknown'."""
    yaml_path = tmp_path / "empty.yml"
    # A bare comment yields a dict with no machine and no repos.
    yaml_path.write_text("# nothing here\n")

    with pytest.raises(typer.Exit) as exc:
        _dispatch_from_yaml(yaml_path)

    assert exc.value.exit_code == 2


# ---------------------------------------------------------------------------
# _bsp_from_cwd
# ---------------------------------------------------------------------------


def test_bsp_from_cwd_nxp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cwd inside workspace/nxp/ returns 'nxp'."""
    (tmp_path / "nxp").mkdir()
    monkeypatch.chdir(tmp_path / "nxp")

    assert _bsp_from_cwd(tmp_path) == "nxp"


def test_bsp_from_cwd_ti(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cwd inside workspace/ti/ returns 'ti'."""
    (tmp_path / "ti").mkdir()
    monkeypatch.chdir(tmp_path / "ti")

    assert _bsp_from_cwd(tmp_path) == "ti"


def test_bsp_from_cwd_workspace_root_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cwd at the workspace root (no first-part component) returns None."""
    monkeypatch.chdir(tmp_path)

    assert _bsp_from_cwd(tmp_path) is None


def test_bsp_from_cwd_outside_workspace_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cwd not under the workspace returns None (relative_to raises ValueError)."""
    other = tmp_path / "outside"
    other.mkdir()
    inside = tmp_path / "ws"
    inside.mkdir()
    monkeypatch.chdir(other)

    assert _bsp_from_cwd(inside) is None


def test_bsp_from_cwd_unrelated_subdir_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cwd under workspace/<other>/ returns None (only nxp/ti are recognized)."""
    other = tmp_path / "build"
    other.mkdir()
    monkeypatch.chdir(other)

    assert _bsp_from_cwd(tmp_path) is None


# ---------------------------------------------------------------------------
# _print_diagnosis
# ---------------------------------------------------------------------------


def test_print_diagnosis_all_pass_prints_summary(capsys: pytest.CaptureFixture[str]) -> None:
    """All-PASS results print the short summary, not the table.

    ``bakar.commands._app`` builds ``console = Console(stderr=True)``, so the
    diagnostic output lands on stderr - read ``capsys.readouterr().err``.
    """
    results = [
        CheckResult(name="docker", severity=Severity.BLOCK, status=Status.PASS, message="ok"),
        CheckResult(name="kas", severity=Severity.BLOCK, status=Status.PASS, message="ok"),
    ]

    _print_diagnosis(results)

    err = capsys.readouterr().err
    assert "2/2 checks passed" in err


def test_print_diagnosis_with_fail_renders_table_and_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-PASS result triggers the table render and surfaces fix hints."""
    results = [
        CheckResult(name="docker", severity=Severity.BLOCK, status=Status.PASS, message="ok"),
        CheckResult(
            name="kas",
            severity=Severity.BLOCK,
            status=Status.FAIL,
            message="kas not installed",
            fix_hint="pip install kas",
        ),
        CheckResult(
            name="optional-tool",
            severity=Severity.WARN,
            status=Status.FAIL,
            message="missing",
        ),
        CheckResult(name="skipped", severity=Severity.INFO, status=Status.SKIP, message="n/a"),
    ]

    _print_diagnosis(results)

    err = capsys.readouterr().err
    # Table headers and each check name must appear.
    assert "Pre-flight diagnosis" in err
    assert "kas" in err
    assert "optional-tool" in err
    assert "skipped" in err
    # Fix hint surfaces only for FAILs that supplied one.
    assert "pip install kas" in err


def test_print_diagnosis_info_fail_uses_cyan_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An INFO-severity FAIL drives the cyan branch of the status_colour dict."""
    results = [
        CheckResult(name="ok-check", severity=Severity.BLOCK, status=Status.PASS, message="ok"),
        CheckResult(
            name="info-fail",
            severity=Severity.INFO,
            status=Status.FAIL,
            message="informational",
        ),
    ]

    _print_diagnosis(results)

    err = capsys.readouterr().err
    assert "info-fail" in err
    assert "informational" in err


# ---------------------------------------------------------------------------
# _find_run
# ---------------------------------------------------------------------------


def test_find_run_latest_when_no_id(tmp_path: Path) -> None:
    """With run_id=None and multiple runs, the newest (highest name) wins."""
    runs_dir = tmp_path / "nxp" / "build" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "20260101-000000").mkdir()
    (runs_dir / "20260301-120000").mkdir()
    newest = runs_dir / "20260529-000000"
    newest.mkdir()

    result = _find_run([(runs_dir, "nxp")], None)

    assert result is not None
    found, label = result
    assert found == newest
    assert label == "nxp"


def test_find_run_explicit_id(tmp_path: Path) -> None:
    """An explicit run_id selects exactly that directory."""
    runs_dir = tmp_path / "ti" / "build" / "runs"
    runs_dir.mkdir(parents=True)
    target = runs_dir / "20260301-120000"
    target.mkdir()
    (runs_dir / "20260529-000000").mkdir()

    result = _find_run([(runs_dir, "ti")], "20260301-120000")

    assert result is not None
    found, label = result
    assert found == target
    assert label == "ti"


def test_find_run_missing_runs_dir_skipped(tmp_path: Path) -> None:
    """A runs_dir that does not exist is silently skipped."""
    missing = tmp_path / "absent" / "runs"
    present = tmp_path / "present" / "runs"
    present.mkdir(parents=True)
    only_run = present / "20260529-000000"
    only_run.mkdir()

    result = _find_run([(missing, "nxp"), (present, "generic")], None)

    assert result is not None
    found, label = result
    assert found == only_run
    assert label == "generic"


def test_find_run_no_candidates_returns_none(tmp_path: Path) -> None:
    """No runs anywhere -> None."""
    only = tmp_path / "runs"
    only.mkdir()  # exists but empty

    result = _find_run([(only, "nxp")], None)

    assert result is None


def test_find_run_explicit_id_not_found_returns_none(tmp_path: Path) -> None:
    """Explicit ID with no matching directory returns None."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "20260101-000000").mkdir()

    result = _find_run([(runs_dir, "nxp")], "nope-not-here")

    assert result is None


def test_find_run_ignores_non_directory_entries(tmp_path: Path) -> None:
    """Stray files inside runs/ are ignored; only directories count."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "stray.txt").write_text("not a run")
    real = runs_dir / "20260529-000000"
    real.mkdir()

    result = _find_run([(runs_dir, "nxp")], None)

    assert result is not None
    found, _ = result
    assert found == real


# ---------------------------------------------------------------------------
# _dispatch_bsp env-var hygiene (defensive: don't leak across tests)
# ---------------------------------------------------------------------------


def test_dispatch_bsp_does_not_mutate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """_dispatch_bsp reads BAKAR_MANIFEST but does not write to os.environ."""
    monkeypatch.setenv("BAKAR_MANIFEST", DEFAULT_NXP_MANIFEST)
    before = dict(os.environ)

    _dispatch_bsp(manifest_arg=None)

    assert dict(os.environ) == before
