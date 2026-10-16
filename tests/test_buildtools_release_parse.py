"""Tests for parsing the oe-core release codename from layer.conf text."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bakar.buildtools import parse_layerseries_corenames, resolve_oe_core_release_key

if TYPE_CHECKING:
    from pathlib import Path


def test_wrynose_line() -> None:
    assert parse_layerseries_corenames('LAYERSERIES_CORENAMES = "wrynose"\n') == "wrynose"


def test_scarthgap_line() -> None:
    text = 'BBFILE_PRIORITY_core = "5"\nLAYERSERIES_CORENAMES = "scarthgap"\n'
    assert parse_layerseries_corenames(text) == "scarthgap"


def test_weak_assignment_form() -> None:
    assert parse_layerseries_corenames('LAYERSERIES_CORENAMES ?= "wrynose"') == "wrynose"


def test_suffixed_variable_ignored() -> None:
    assert parse_layerseries_corenames('LAYERSERIES_CORENAMES_foo = "x"') is None


def test_suffixed_variable_skipped_before_real_one() -> None:
    text = 'LAYERSERIES_CORENAMES_foo = "x"\nLAYERSERIES_CORENAMES = "wrynose"\n'
    assert parse_layerseries_corenames(text) == "wrynose"


def test_no_assignment_returns_none() -> None:
    assert parse_layerseries_corenames('BBPATH .= ":${LAYERDIR}"\n') is None


def test_empty_value_returns_none() -> None:
    assert parse_layerseries_corenames('LAYERSERIES_CORENAMES = ""') is None


def test_two_names_returns_first() -> None:
    assert parse_layerseries_corenames('LAYERSERIES_CORENAMES = "wrynose walnascar"') == "wrynose"


def test_resolve_reads_workspace_layer_conf(tmp_path: Path) -> None:
    conf_dir = tmp_path / "openembedded-core" / "meta" / "conf"
    conf_dir.mkdir(parents=True)
    (conf_dir / "layer.conf").write_text('LAYERSERIES_CORENAMES = "wrynose"\n')
    assert resolve_oe_core_release_key(tmp_path) == "wrynose"


def test_resolve_missing_file_returns_none(tmp_path: Path) -> None:
    assert resolve_oe_core_release_key(tmp_path) is None
