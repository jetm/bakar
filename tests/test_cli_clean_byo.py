"""Tests for the BYO/generic form of ``bakar clean``.

``bakar clean`` historically resolved only NXP/TI BSP build dirs
(``workspace/<family>/build``). A meta-avocado workspace is generic/BYO with a
machine build dir at ``workspace/build-<yaml-stem>/build``, which the BSP-only
arg surface could not reach. The BYO form mirrors ``bakar build my.yml``: pass
the kas YAML positionally and clean *that* build dir.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.cli import app

if TYPE_CHECKING:
    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


def test_clean_byo_meta_avocado_yaml_wipes_build_stem_dir(
    runner: _CliRunner, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bakar clean <meta-avocado-yaml>`` wipes ``workspace/build-<stem>/build``.

    A meta-avocado YAML resolves to a generic family with the workspace at the
    parent of ``meta-avocado/``, so ``bsp_root`` is ``workspace/build-<stem>``.
    The BSP-only ``--bsp nxp|ti`` ladder cannot express this, so the positional
    YAML form is the only path that reaches the machine build dir.
    """
    import shutil

    (tmp_path / ".bakar.toml").write_text("")
    yaml = tmp_path / "meta-avocado" / "kas" / "machine" / "qtest.yml"
    yaml.parent.mkdir(parents=True)
    yaml.write_text("machine: avocado-qemuarm64\n")
    build_dir = tmp_path / "build-qtest" / "build"
    (build_dir / "tmp" / "work" / "r0").mkdir(parents=True)
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    removed: list[str] = []
    monkeypatch.setattr(shutil, "rmtree", lambda path, *a, **kw: removed.append(str(path)))

    result = runner.invoke(app, ["clean", "meta-avocado/kas/machine/qtest.yml"])

    assert result.exit_code == 0, result.output
    assert str(build_dir) in removed, f"expected build-qtest/build wiped, got {removed!r}"
