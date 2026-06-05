"""Ordering tests for ``--show-layers`` on the manifest build path.

Like the BYO path, the manifest path keys ``_print_layer_hashes`` off
``cfg.bblayers_conf`` via ``collect_layer_hashes``. kas materializes
``bblayers.conf`` during ``step_kas.run_build``, so on a fresh build the conf
does not exist until the build has run. Task 1.2 moved ``_print_layer_hashes``
in ``_run_manifest_build`` from before sync to after a successful
``run_build``, matching the BYO and bbsetup paths.

These tests call ``_run_manifest_build`` directly with every collaborator
stubbed and assert the relative ordering of ``_print_layer_hashes`` and
``step_kas.run_build`` via a shared parent ``MagicMock``. Sync and setup-env
are skipped by stubbing ``detect`` to report a fully-synced workspace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from bakar.commands.build import _BuildCtx, _run_manifest_build

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _fake_cfg(tmp_path: Path) -> MagicMock:
    cfg = MagicMock()
    cfg.machine = "imx8mp-var-dart"
    cfg.bsp_root = tmp_path
    cfg.kas_yaml = tmp_path / "kas.yml"
    cfg.bblayers_conf = tmp_path / "build" / "conf" / "bblayers.conf"
    return cfg


def _synced_state() -> MagicMock:
    """A workspace state that skips both sync and setup-env."""
    state = MagicMock()
    state.needs_repo_sync = False
    state.needs_setup_env = False
    return state


def _ctx(*, show_layers: bool, dry_run: bool) -> _BuildCtx:
    return _BuildCtx(
        overlay_source=MagicMock(name="overlay_source"),
        extra_overlays=[],
        bsp=MagicMock(name="bsp"),
        family="nxp",
        effective_show_layers=show_layers,
        dry_run=dry_run,
        keep_going=False,
        skip_doctor=True,
        skip_sync=False,
    )


def _run(*, show_layers: bool, dry_run: bool, tmp_path: Path) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Drive ``_run_manifest_build`` with collaborators recorded on a shared parent mock.

    Returns ``(parent, print_layer_hashes, run_build)`` so callers can assert
    both call counts and relative ordering via ``parent.mock_calls``.
    """
    parent = MagicMock()
    print_layer_hashes = parent.print_layer_hashes
    run_build = parent.run_build
    run_build.return_value = 0

    cfg = _fake_cfg(tmp_path)
    log = MagicMock()
    log.run_id = "20260603-000000"

    with (
        patch("bakar.commands.build._run_doctor_gate", parent.run_doctor_gate),
        patch("bakar.commands.build.detect", return_value=_synced_state()),
        patch("bakar.commands.build.step_override.apply", parent.step_override_apply),
        patch("bakar.commands.build.step_kas.regenerate_yaml", parent.regenerate_yaml),
        patch("bakar.commands.build.step_kas.run_build", run_build),
        patch("bakar.commands.build._tuning_extra_overlays", return_value=[]),
        patch("bakar.commands.build._print_layer_hashes", print_layer_hashes),
        patch("bakar.commands.build.console", parent.console),
    ):
        _run_manifest_build(cfg, log, _ctx(show_layers=show_layers, dry_run=dry_run))

    return parent, print_layer_hashes, run_build


def _ordered_names(parent: MagicMock) -> list[str]:
    """The top-level attribute names of the recorded calls, in order."""
    return [name.split(".", 1)[0] for name, _args, _kwargs in parent.mock_calls if name]


def test_real_build_prints_layers_after_run_build(tmp_path: Path) -> None:
    """A real build (``dry_run=False``) prints the table once, after ``run_build``."""
    parent, print_layer_hashes, run_build = _run(show_layers=True, dry_run=False, tmp_path=tmp_path)

    assert print_layer_hashes.call_count == 1
    assert run_build.call_count == 1

    names = _ordered_names(parent)
    assert "run_build" in names
    assert "print_layer_hashes" in names
    assert names.index("print_layer_hashes") > names.index("run_build"), (
        f"expected _print_layer_hashes after run_build on a real build, got order {names}"
    )


def test_show_layers_disabled_never_prints(tmp_path: Path) -> None:
    """With ``effective_show_layers`` false, the table is never rendered."""
    _parent, print_layer_hashes, _run_build = _run(show_layers=False, dry_run=False, tmp_path=tmp_path)

    assert print_layer_hashes.call_count == 0
