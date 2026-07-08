"""Tests for the ``bakar gen-kas`` command.

Drives the command through the Typer ``CliRunner``, monkeypatching the two
kas writers imported into ``bakar.commands.gen_kas`` (line 18:
``write_bbsetup_yaml, write_yaml``) so no real YAML rendering or disk
writes happen.

Two branches are covered:

* bbsetup workspace - ``manifest is None`` and the workspace looks like an
  initialized ``bitbake-setup`` workspace, so ``write_bbsetup_yaml`` is
  called and the resulting path is printed.
* main dispatch - any NXP/TI/generic workspace; ``write_yaml(opts)`` is
  called with a ``KasGenOptions`` and the output path is printed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import bakar.commands.gen_kas as gen_kas_module
from bakar.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


# Minimal valid bitbake-setup config (subset of the shape checked by
# ``is_bbsetup_workspace`` - both ``data`` and ``bitbake-config`` must be
# present as top-level keys).
_VALID_BBSETUP_CONFIG: dict = {
    "type": "registry",
    "name": "oe-nodistro-wrynose",
    "data": {
        "sources": {
            "openembedded-core": {
                "git-remote": {
                    "uri": "https://git.openembedded.org/openembedded-core",
                    "branch": "wrynose",
                }
            }
        }
    },
    "bitbake-config": {
        "name": "nodistro",
        "bb-layers": ["openembedded-core/meta"],
    },
}


@pytest.fixture
def bbsetup_workspace(tmp_path: Path) -> Path:
    """A tmp dir laid out like an initialized ``bitbake-setup`` workspace."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config-upstream.json").write_text(json.dumps(_VALID_BBSETUP_CONFIG), encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "init-build-env").write_text("", encoding="utf-8")
    return tmp_path


def test_bbsetup_workspace_calls_write_bbsetup_yaml(
    runner: _CliRunner, bbsetup_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``manifest is None`` + bbsetup workspace dispatches to write_bbsetup_yaml.

    The main-branch writer must NOT be called, and the printed path must
    match the value the bbsetup writer returns.
    """
    expected = bbsetup_workspace / "kas-bbsetup.yml"
    bbsetup_calls: list[dict] = []
    main_calls: list[object] = []

    def fake_bbsetup(
        setup_dir: Path,
        *,
        target: str = "core-image-minimal",
        machine_override: str | None = None,
        distro_override: str | None = None,
    ) -> Path:
        bbsetup_calls.append(
            {
                "setup_dir": setup_dir,
                "target": target,
                "machine_override": machine_override,
                "distro_override": distro_override,
            }
        )
        return expected

    monkeypatch.setattr(gen_kas_module, "write_bbsetup_yaml", fake_bbsetup)
    monkeypatch.setattr(gen_kas_module, "write_yaml", main_calls.append)

    result = runner.invoke(app, ["gen-kas", "--workspace", str(bbsetup_workspace)])

    assert result.exit_code == 0, result.output
    assert len(bbsetup_calls) == 1, f"expected one bbsetup write, got {bbsetup_calls!r}"
    assert bbsetup_calls[0]["setup_dir"] == bbsetup_workspace.resolve()
    assert main_calls == [], "main dispatch writer must not run on bbsetup path"
    # Rich may wrap long paths with line breaks; collapse whitespace before
    # checking that the printed path matches what the writer returned.
    flat_output = "".join(result.output.split())
    assert "wrote" in result.output
    assert "kas-bbsetup.yml" in flat_output


def test_main_dispatch_calls_write_yaml(
    runner: _CliRunner, nxp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An NXP workspace with a manifest dispatches to ``write_yaml(opts)``.

    The bbsetup writer must NOT be called; the printed path must include the
    default ``kas-nxp.yml`` filename produced by the cfg resolution.
    """
    main_calls: list[object] = []
    bbsetup_calls: list[object] = []

    monkeypatch.setattr(gen_kas_module, "write_bbsetup_yaml", lambda *a, **kw: bbsetup_calls.append((a, kw)))
    monkeypatch.setattr(gen_kas_module, "write_yaml", main_calls.append)

    result = runner.invoke(
        app,
        [
            "gen-kas",
            "--workspace",
            str(nxp_workspace),
            "--manifest",
            "imx-6.12.49-2.2.0.xml",
        ],
    )

    assert result.exit_code == 0, result.output
    assert bbsetup_calls == [], "bbsetup writer must not run on main dispatch path"
    assert len(main_calls) == 1, f"expected one write_yaml call, got {main_calls!r}"
    # Rich may wrap long paths with line breaks; flatten before checking.
    flat_output = "".join(result.output.split())
    assert "kas-nxp.yml" in flat_output
