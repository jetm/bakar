"""Tests for the BYO/generic form of ``bakar clean``.

``bakar clean`` historically resolved only NXP/TI BSP build dirs
(``workspace/<family>/build``). A meta-avocado workspace is generic/BYO with a
machine build dir at ``workspace/build-<yaml-stem>/build``, which the BSP-only
arg surface could not reach. The BYO form mirrors ``bakar build my.yml``: pass
the kas YAML positionally and clean *that* build dir.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from bakar.cli import app

if TYPE_CHECKING:
    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


def test_clean_byo_meta_avocado_yaml_wipes_build_stem_dir(
    runner: _CliRunner, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bakar clean <meta-avocado-yaml>`` wipes ``workspace/build-<stem>/build``.

    A meta-avocado YAML resolves to a generic family with the workspace at the
    parent of ``meta-avocado/``, so ``bsp_root`` is ``workspace/build-<stem>``.
    The BSP-only ``--bsp nxp|ti`` ladder cannot express this, so the positional
    YAML form is the only path that reaches the machine build dir.
    """
    (tmp_path / ".bakar.toml").write_text("")
    yaml = tmp_path / "meta-avocado" / "kas" / "machine" / "qtest.yml"
    yaml.parent.mkdir(parents=True)
    yaml.write_text("machine: avocado-qemuarm64\n")
    build_dir = tmp_path / "build-qtest" / "build"
    (build_dir / "tmp" / "work" / "r0").mkdir(parents=True)
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    # The descriptor-based removal walks entries relative to a held directory
    # fd, so only the tree root is removed by absolute path (os.rmdir). Record
    # those root removals.
    removed: list[str] = []
    real_rmdir = os.rmdir

    def _recording_rmdir(path, *a, **kw):
        if kw.get("dir_fd") is None:
            removed.append(str(path))
        real_rmdir(path, *a, **kw)

    monkeypatch.setattr(os, "rmdir", _recording_rmdir)

    result = runner.invoke(app, ["clean", "meta-avocado/kas/machine/qtest.yml"])

    assert result.exit_code == 0, result.output
    trash = build_dir.with_name(f".bakar-trash-{os.getpid()}-{build_dir.name}")
    assert str(trash) in removed, f"expected build-qtest/build wiped, got {removed!r}"
    assert not build_dir.exists()
    assert not trash.exists()
    # Every removal ran against a per-target trash sibling, never the shared bare name.
    assert all(f".bakar-trash-{os.getpid()}-" in r for r in removed), removed
