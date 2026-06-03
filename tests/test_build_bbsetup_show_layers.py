"""CliRunner tests for ``--show-layers`` on the bbsetup build path.

``_run_bbsetup_build`` renders the layer-hash table by calling
``_print_layer_hashes(cfg)`` after ``write_bbsetup_yaml`` when the effective
toggle is set: either ``--show-layers`` on the command line or the persisted
``show_hashes`` user-config key. These tests drive the bbsetup dispatch
(``_bbsetup_workspace`` returning a setup dir) with every real collaborator
short-circuited so no build runs, and observe whether ``_print_layer_hashes``
is invoked.

The dispatch in ``build()`` only routes to ``_run_bbsetup_build`` when no
positional kas YAML and no ``--manifest`` are given AND ``_bbsetup_workspace``
returns a non-None setup dir, so that helper is patched to a fake path. The
doctor pre-flight is suppressed with ``--skip-doctor``. ``_USER_CONFIG`` is
controlled by patching ``_load_user_config_safe`` (the app callback that the
CliRunner invocation triggers) so the no-flag case has ``show_hashes=False``
and the toggle case has it True.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from bakar.cli import app
from bakar.user_config import UserConfig

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _fake_resolve(setup_dir: Path) -> MagicMock:
    """A ``BuildConfig`` stand-in carrying only the attributes the bbsetup path reads."""
    cfg = MagicMock()
    cfg.image = "core-image-minimal"
    cfg.kas_yaml = setup_dir / "kas.yml"
    cfg.bsp_root = setup_dir
    cfg.runs_dir = setup_dir / "build" / "runs"
    cfg.container_image = "ghcr.io/example/kas:latest"
    cfg.sstate_mirror_url = None
    return cfg


def _runlogger_cm() -> MagicMock:
    """A ``RunLogger`` replacement usable as a context manager."""
    cm = MagicMock()
    log = MagicMock()
    log.run_id = "20260603-000000"
    cm.return_value.__enter__.return_value = log
    cm.return_value.__exit__.return_value = False
    return cm


def _invoke_bbsetup_build(
    runner: CliRunner,
    tmp_path: Path,
    *,
    show_layers: bool,
    show_hashes: bool,
) -> tuple[int, MagicMock, str]:
    """Drive ``bakar build`` down the bbsetup path with all collaborators stubbed.

    Returns ``(exit_code, print_layer_hashes_mock, output)``. The bbsetup
    dispatch is forced by patching ``_bbsetup_workspace`` to return a real
    ``tmp_path`` dir; ``resolve``, ``translate_bbsetup_config``,
    ``write_bbsetup_yaml``, ``RunLogger`` and ``step_kas.run_build`` are stubbed
    so no real build runs. ``_load_user_config_safe`` is patched so the app
    callback installs a ``UserConfig`` with the requested ``show_hashes`` value.
    """
    setup_dir = tmp_path / "ws"
    setup_dir.mkdir()

    print_layer_hashes = MagicMock()
    user_config = UserConfig(show_hashes=show_hashes)
    args = ["build", "--skip-doctor"]
    if show_layers:
        args.append("--show-layers")

    with (
        patch("bakar.commands._app._load_user_config_safe", return_value=user_config),
        patch("bakar.commands._app._get_vendors", return_value=[]),
        patch("bakar.commands.build._bbsetup_workspace", return_value=setup_dir),
        patch("bakar.commands.build.resolve", return_value=_fake_resolve(setup_dir)),
        patch(
            "bakar.commands.build.translate_bbsetup_config",
            return_value={"machine": "qemux86-64"},
        ),
        patch("bakar.commands.build.write_bbsetup_yaml"),
        patch("bakar.commands.build._print_layer_hashes", print_layer_hashes),
        patch("bakar.commands.build.RunLogger", _runlogger_cm()),
        patch("bakar.commands.build.step_kas.run_build", return_value=0),
    ):
        result = runner.invoke(app, args)

    return result.exit_code, print_layer_hashes, result.output


def test_show_layers_flag_reaches_print_layer_hashes(runner: CliRunner, tmp_path: Path) -> None:
    """``--show-layers`` on the bbsetup path invokes ``_print_layer_hashes(cfg)``."""
    exit_code, print_layer_hashes, output = _invoke_bbsetup_build(
        runner, tmp_path, show_layers=True, show_hashes=False
    )

    assert exit_code == 0, output
    assert print_layer_hashes.call_count == 1, (
        f"expected _print_layer_hashes to be called once with --show-layers, "
        f"got {print_layer_hashes.call_count} call(s)"
    )


def test_no_flag_no_toggle_skips_print_layer_hashes(runner: CliRunner, tmp_path: Path) -> None:
    """Without ``--show-layers`` and with ``show_hashes`` false, the table is not rendered."""
    exit_code, print_layer_hashes, output = _invoke_bbsetup_build(
        runner, tmp_path, show_layers=False, show_hashes=False
    )

    assert exit_code == 0, output
    assert print_layer_hashes.call_count == 0, (
        "expected _print_layer_hashes NOT to be called when neither --show-layers "
        f"nor show_hashes is set, got {print_layer_hashes.call_count} call(s)"
    )
