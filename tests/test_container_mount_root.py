"""The container mount root is one value: KAS_WORK_DIR, the event-log path and
the build-UI path translator must all agree on it, for every BSP family."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.observability import RunLogger
from bakar.steps.kas_build import _build_env, _container_eventlog_path, _ui_logfile_translator
from bakar.triage import translate_container_path
from tests.conftest import make_build_config

if TYPE_CHECKING:
    from pathlib import Path

    from bakar.config import BuildConfig

pytestmark = pytest.mark.unit

_META_STEM = "avocado-x"


def _cfg(family: str, workspace: Path, *, host_mode: bool = False) -> BuildConfig:
    if family in ("nxp", "ti"):
        return make_build_config(workspace=workspace, bsp_family=family, host_mode=host_mode)
    if family == "generic":
        yml = workspace / "byo" / "my.yml"
        yml.parent.mkdir(parents=True)
        yml.write_text("header: {version: 14}\n")
    else:
        yml = workspace / "meta-avocado" / "kas" / f"{_META_STEM}.yml"
        yml.parent.mkdir(parents=True)
        yml.write_text("header: {version: 14}\n")
    return make_build_config(workspace=workspace, bsp_family="generic", kas_yaml_override=yml, host_mode=host_mode)


FAMILIES = ["nxp", "ti", "generic", "meta-avocado"]


@pytest.mark.parametrize("family", FAMILIES)
def test_kas_work_dir_root(family: str, tmp_path: Path) -> None:
    cfg = _cfg(family, tmp_path)
    if family == "meta-avocado":
        assert cfg.is_meta_avocado
        assert cfg.kas_work_dir == cfg.workspace
        assert cfg.bsp_root == tmp_path / f"build-{_META_STEM}"
    else:
        assert cfg.kas_work_dir == cfg.bsp_root
    if family in ("nxp", "ti"):
        assert cfg.runs_dir.is_relative_to(cfg.kas_work_dir)


@pytest.mark.parametrize("family", FAMILIES)
def test_build_env_kas_work_dir_matches(family: str, tmp_path: Path) -> None:
    cfg = _cfg(family, tmp_path)
    assert _build_env(cfg)["KAS_WORK_DIR"] == str(cfg.kas_work_dir)


@pytest.mark.parametrize("family", FAMILIES)
def test_eventlog_path_maps_back_to_host_run_dir(family: str, tmp_path: Path) -> None:
    cfg = _cfg(family, tmp_path)
    log = RunLogger(runs_dir=cfg.runs_dir)
    container = _container_eventlog_path(cfg, log)
    assert container.startswith("/work/")
    assert translate_container_path(container, cfg.kas_work_dir) == str(log.eventlog_path)


def test_meta_avocado_ui_translator_has_no_doubled_build_dir(tmp_path: Path) -> None:
    cfg = _cfg("meta-avocado", tmp_path)
    translate = _ui_logfile_translator(cfg)
    assert translate is not None
    got = translate(f"/work/build-{_META_STEM}/build/tmp/work/x/temp/log.do_compile")
    assert got == f"{tmp_path}/build-{_META_STEM}/build/tmp/work/x/temp/log.do_compile"
    assert f"build-{_META_STEM}/build-{_META_STEM}" not in got


@pytest.mark.parametrize("family", ["nxp", "ti", "generic"])
def test_ui_translator_root_matches_kas_work_dir(family: str, tmp_path: Path) -> None:
    cfg = _cfg(family, tmp_path)
    translate = _ui_logfile_translator(cfg)
    assert translate is not None
    assert translate("/work/build/x/log") == f"{cfg.kas_work_dir}/build/x/log"


@pytest.mark.parametrize("family", FAMILIES)
def test_host_mode_has_no_translator(family: str, tmp_path: Path) -> None:
    assert _ui_logfile_translator(_cfg(family, tmp_path, host_mode=True)) is None
