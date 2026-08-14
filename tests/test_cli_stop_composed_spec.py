"""``bakar stop`` must accept the same colon-composed kas YAML spec as ``bakar build``.

``bakar build a.yml:b.yml:c.yml`` is the documented way to layer feature
overlays onto a machine config, and ``build`` splits that arg via
``split_kas_yaml_arg`` before dispatching. ``stop`` did not, so handing it the
spec that started a build treated the whole colon-joined string as one path and
died with "kas YAML not found" - leaving no supported way to stop that build.

The second test pins the consequence that made this worth fixing: with the arg
unsplit, family inference falls back to a BSP name and ``cfg.bsp_root`` becomes
``workspace/<family>`` instead of the meta-avocado ``workspace/build-<stem>``,
so ``stop_build`` scans a runs directory that does not exist and reports "no
running build found" while the build is still going.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bakar.commands.stop as stop_cmd
from bakar.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def avocado_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A meta-avocado workspace laid out the way a real BYO build leaves one.

    The machine YAML lives inside ``meta-avocado/``, which is what makes
    ``is_meta_avocado`` true and sends ``bsp_root`` to ``build-<yaml-stem>``
    rather than ``workspace/<family>``. The run dir is created there so a
    correctly-resolved stop has something to find.
    """
    (tmp_path / ".bakar.toml").write_text("")
    kas = tmp_path / "meta-avocado" / "kas"
    (kas / "machine").mkdir(parents=True)
    (kas / "feature").mkdir(parents=True)
    (kas / "machine" / "imx93-frdm.yml").write_text("machine: avocado-imx93-frdm\n")
    (kas / "feature" / "encrypted-var.yml").write_text("header:\n  version: 16\n")
    (kas / "feature" / "ftpm.yml").write_text("header:\n  version: 16\n")
    (tmp_path / "build-imx93-frdm" / "build" / "runs" / "20260814-114115").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_stop_accepts_colon_composed_spec(
    runner: _CliRunner, avocado_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact spec that starts a build is accepted by ``stop`` unchanged."""
    calls: list[Path] = []

    def _rec(bsp_root: Path, cfg: object = None, *, force: bool = False, grace_seconds: float = 0) -> bool:
        calls.append(bsp_root)
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _rec)

    result = runner.invoke(
        app,
        [
            "stop",
            "meta-avocado/kas/machine/imx93-frdm.yml"
            ":meta-avocado/kas/feature/encrypted-var.yml"
            ":meta-avocado/kas/feature/ftpm.yml",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "kas YAML not found" not in result.output
    assert len(calls) == 1


def test_stop_composed_spec_resolves_to_the_build_runs_dir(
    runner: _CliRunner, avocado_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stop`` targets the same bsp_root the build wrote its runs under.

    Accepting the arg is not enough - it has to resolve to the directory that
    actually holds the run, or stop reports "no running build found" against a
    live build.
    """
    calls: list[Path] = []

    def _rec(bsp_root: Path, cfg: object = None, *, force: bool = False, grace_seconds: float = 0) -> bool:
        calls.append(bsp_root)
        return True

    monkeypatch.setattr(stop_cmd.build_stop, "stop_build", _rec)

    result = runner.invoke(
        app,
        [
            "stop",
            "meta-avocado/kas/machine/imx93-frdm.yml"
            ":meta-avocado/kas/feature/encrypted-var.yml"
            ":meta-avocado/kas/feature/ftpm.yml",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [avocado_workspace / "build-imx93-frdm"]
    assert (calls[0] / "build" / "runs" / "20260814-114115").is_dir()
