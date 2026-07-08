"""Tests for the ``bakar diff`` command.

Drives the command through the Typer ``CliRunner``. The NXP path
monkeypatches ``diff_manifests`` on ``bakar.commands.diff`` (where the
``diff`` function looks it up - mock pattern from
``tests/test_cli_layers.py``) so no manifest parsing happens; the BYO
path monkeypatches ``subprocess.run`` (pattern from
``tests/test_layer_hashes.py``) to capture the ``kas diff`` argv without
running a real ``kas``.

``_dispatch_bsp`` only ever returns ``nxp`` or ``ti``; the BYO/kas-diff
fallback in ``diff.py`` is the non-NXP family, so the BYO path is
exercised by passing a TI ``--manifest``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bakar.commands.diff as diff_module
from bakar.cli import app
from bakar.manifest_diff import LayerDiff
from tests._fakes import Completed as _Completed

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit

_TI_MANIFEST = "processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt"


@pytest.fixture
def ti_workspace(tmp_path: Path) -> Path:
    """A workspace with a ``ti/`` subdir so workspace detection picks ti."""
    (tmp_path / "ti").mkdir()
    return tmp_path


@pytest.mark.unit
def test_nxp_renders_changed_and_unchanged_rows(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The NXP path renders one changed row (both 8-char SHAs) and one unchanged row."""
    changed = LayerDiff(
        layer="poky",
        old_sha="aaaaaaaa1111111111111111111111111111aaaa",
        new_sha="bbbbbbbb2222222222222222222222222222bbbb",
        commit_count=5,
    )
    unchanged = LayerDiff(
        layer="meta-freescale",
        old_sha="cccccccc3333333333333333333333333333cccc",
        new_sha="cccccccc3333333333333333333333333333cccc",
        commit_count=0,
    )
    monkeypatch.setattr(diff_module, "diff_manifests", lambda old, new, *, checkout_root=None: [changed, unchanged])

    result = runner.invoke(
        app,
        ["diff", "old.xml", "new.xml", "--workspace", str(nxp_workspace)],
    )

    assert result.exit_code == 0, result.output
    # Changed layer: both SHAs truncated to 8 chars, marked changed.
    assert "poky" in result.output
    assert "aaaaaaaa" in result.output
    assert "bbbbbbbb" in result.output
    assert "changed" in result.output
    # Unchanged layer: same SHA both sides, marked unchanged.
    assert "meta-freescale" in result.output
    assert "cccccccc" in result.output
    assert "unchanged" in result.output


@pytest.mark.unit
def test_byo_invokes_kas_diff_with_both_configs(
    runner: _CliRunner, ti_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The BYO path invokes ``kas diff`` with both config paths in argv."""
    captured: dict[str, list[str]] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        return _Completed(0)

    monkeypatch.setattr(diff_module.subprocess, "run", fake_run)

    result = runner.invoke(
        app,
        ["diff", "a.yml", "b.yml", "--manifest", _TI_MANIFEST, "--workspace", str(ti_workspace)],
    )

    assert result.exit_code == 0, result.output
    argv = captured["argv"]
    # exe is "kas" or "kas-container" depending on host_mode auto-detect.
    assert argv[0] in ("kas", "kas-container")
    assert argv[1] == "diff"
    assert "a.yml" in argv
    assert "b.yml" in argv


@pytest.mark.unit
def test_yaml_inputs_reach_kas_diff_without_manifest(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.yml` arguments delegate to ``kas diff`` even when family defaults to NXP.

    Dispatch keys on the input file type, not the workspace family: two kas
    configs must reach ``kas diff`` rather than the manifest-XML path, even
    with no ``--manifest`` (which defaults the family to NXP).
    """
    captured: dict[str, list[str]] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        return _Completed(0)

    monkeypatch.setattr(diff_module.subprocess, "run", fake_run)
    # diff_manifests must NOT be reached for .yml inputs.
    monkeypatch.setattr(
        diff_module,
        "diff_manifests",
        lambda *a, **k: pytest.fail("diff_manifests called for .yml inputs"),
    )

    result = runner.invoke(app, ["diff", "a.yml", "b.yml", "--workspace", str(nxp_workspace)])

    assert result.exit_code == 0, result.output
    argv = captured["argv"]
    assert argv[1] == "diff"
    assert "a.yml" in argv
    assert "b.yml" in argv


@pytest.mark.unit
def test_byo_nonzero_return_propagates(runner: _CliRunner, ti_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero ``kas diff`` return propagates to a non-zero exit."""

    def fake_run(argv, **kwargs):
        return _Completed(3)

    monkeypatch.setattr(diff_module.subprocess, "run", fake_run)

    result = runner.invoke(
        app,
        ["diff", "a.yml", "b.yml", "--manifest", _TI_MANIFEST, "--workspace", str(ti_workspace)],
    )

    assert result.exit_code != 0, result.output
