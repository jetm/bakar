"""Unit tests for :mod:`bakar.inspect_parse`.

All tests are pure (no subprocess, no container, no filesystem I/O beyond
reading the fixture files at module load time).  Fixture files live under
``tests/fixtures/`` and capture representative ``bitbake -e`` and
``layer.conf`` text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar.inspect_parse import (
    extract_var_history,
    parse_env_vars,
    parse_layer_conf,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def env_text() -> str:
    return (FIXTURES / "bitbake_env_sample.txt").read_text()


@pytest.fixture(scope="module")
def layer_conf_text() -> str:
    return (FIXTURES / "layer_conf_sample.conf").read_text()


# ===========================================================================
# extract_var_history
# ===========================================================================


class TestExtractVarHistory:
    """Tests for extract_var_history()."""

    def test_single_op_returns_one_location(self, env_text: str) -> None:
        """DISTRO has exactly one 'set' op - expect one location."""
        result = extract_var_history(env_text, "DISTRO")
        assert result == ["/path/to/build/conf/local.conf:10"]

    def test_multi_op_returns_ordered_locations(self, env_text: str) -> None:
        """MACHINE has two 'set' ops - both returned in order."""
        result = extract_var_history(env_text, "MACHINE")
        assert result == [
            "/path/to/build/conf/local.conf:5",
            "/path/to/meta-imx/conf/machine/imx8mp-lpddr4-evk.conf:1",
        ]

    def test_three_op_append_returns_all(self, env_text: str) -> None:
        """SRC_URI has three ops (set + two appends)."""
        result = extract_var_history(env_text, "SRC_URI")
        assert len(result) == 3
        assert result[0] == "/path/to/poky/meta/recipes-core/busybox/busybox_1.36.1.bb:15"
        assert result[1] == "/path/to/meta-imx/recipes-core/busybox/busybox_%.bbappend:1"
        assert result[2] == "/path/to/meta-imx/recipes-core/busybox/busybox_%.bbappend:5"

    def test_no_history_recorded_returns_empty(self, env_text: str) -> None:
        """NO_HISTORY_VAR has the [no history recorded] sentinel - return []."""
        result = extract_var_history(env_text, "NO_HISTORY_VAR")
        assert result == []

    def test_missing_var_returns_empty(self, env_text: str) -> None:
        """A variable not present at all returns []."""
        result = extract_var_history(env_text, "COMPLETELY_MISSING_VAR")
        assert result == []

    def test_does_not_raise_on_empty_text(self) -> None:
        """Empty text returns [] without raising."""
        result = extract_var_history("", "MACHINE")
        assert result == []

    def test_does_not_raise_on_garbage_text(self) -> None:
        """Garbage input returns [] without raising."""
        result = extract_var_history("not bitbake output\n123\n!!!", "MACHINE")
        assert result == []

    def test_automatic_origin_returned(self, env_text: str) -> None:
        """WORKDIR uses [automatic] as origin - still captured as file:line."""
        result = extract_var_history(env_text, "WORKDIR")
        assert result == ["[automatic]:1"]

    def test_returns_list_type(self, env_text: str) -> None:
        result = extract_var_history(env_text, "PN")
        assert isinstance(result, list)

    def test_inline_fixture(self) -> None:
        """Self-contained inline fixture - not dependent on the file fixture."""
        text = (
            "#\n"
            "# $BB_NUMBER_THREADS\n"
            "#   set /etc/local.conf:7\n"
            '#     "8"\n'
            'BB_NUMBER_THREADS="8"\n'
        )
        result = extract_var_history(text, "BB_NUMBER_THREADS")
        assert result == ["/etc/local.conf:7"]

    def test_inline_no_history(self) -> None:
        text = (
            "#\n"
            "# $SOME_VAR\n"
            "#   [no history recorded]\n"
            '#   "val"\n'
            'SOME_VAR="val"\n'
        )
        result = extract_var_history(text, "SOME_VAR")
        assert result == []


# ===========================================================================
# parse_env_vars
# ===========================================================================


class TestParseEnvVars:
    """Tests for parse_env_vars()."""

    def test_single_name_found(self, env_text: str) -> None:
        result = parse_env_vars(env_text, ["MACHINE"])
        assert result == {"MACHINE": "imx8mp-lpddr4-evk"}

    def test_multiple_names_found(self, env_text: str) -> None:
        result = parse_env_vars(env_text, ["PN", "PV", "DISTRO"])
        assert result == {
            "PN": "imx-image-multimedia",
            "PV": "1.0",
            "DISTRO": "fsl-imx-wayland",
        }

    def test_missing_name_omitted(self, env_text: str) -> None:
        """Names not present in env_text are omitted (not key-with-None)."""
        result = parse_env_vars(env_text, ["PN", "NONEXISTENT_VAR_XYZ"])
        assert "NONEXISTENT_VAR_XYZ" not in result
        assert result == {"PN": "imx-image-multimedia"}

    def test_empty_names_returns_empty(self, env_text: str) -> None:
        result = parse_env_vars(env_text, [])
        assert result == {}

    def test_empty_text_returns_empty(self) -> None:
        result = parse_env_vars("", ["MACHINE", "DISTRO"])
        assert result == {}

    def test_packages_multiword_value(self, env_text: str) -> None:
        result = parse_env_vars(env_text, ["PACKAGES"])
        assert "PACKAGES" in result
        packages = result["PACKAGES"].split()
        assert "busybox" in packages
        assert "busybox-dbg" in packages

    def test_depends_simple(self, env_text: str) -> None:
        result = parse_env_vars(env_text, ["DEPENDS"])
        assert result == {"DEPENDS": "virtual/libc"}

    def test_all_standard_names(self, env_text: str) -> None:
        """All six named variables from the task spec are extractable."""
        names = ["PN", "PV", "SRC_URI", "WORKDIR", "PACKAGES", "DEPENDS"]
        result = parse_env_vars(env_text, names)
        assert set(result.keys()) == set(names)

    def test_returns_dict_type(self, env_text: str) -> None:
        result = parse_env_vars(env_text, ["PN"])
        assert isinstance(result, dict)

    def test_inline_fixture(self) -> None:
        text = 'MYVAR="hello world"\nOTHER="x"\n'
        result = parse_env_vars(text, ["MYVAR", "OTHER", "MISSING"])
        assert result["MYVAR"] == "hello world"
        assert result["OTHER"] == "x"
        assert "MISSING" not in result

    def test_last_occurrence_wins(self) -> None:
        """When a variable appears twice, the last value is returned."""
        text = 'MACHINE="first"\nMACHINE="second"\n'
        result = parse_env_vars(text, ["MACHINE"])
        assert result["MACHINE"] == "second"


# ===========================================================================
# parse_layer_conf
# ===========================================================================


class TestParseLayerConf:
    """Tests for parse_layer_conf()."""

    def test_priority_extracted(self, layer_conf_text: str) -> None:
        result = parse_layer_conf(layer_conf_text)
        assert result.get("BBFILE_PRIORITY") == "6"

    def test_compat_extracted(self, layer_conf_text: str) -> None:
        result = parse_layer_conf(layer_conf_text)
        assert result.get("LAYERSERIES_COMPAT") == "scarthgap wrynose"

    def test_version_extracted(self, layer_conf_text: str) -> None:
        result = parse_layer_conf(layer_conf_text)
        assert result.get("LAYERVERSION") == "3"

    def test_all_three_keys_present(self, layer_conf_text: str) -> None:
        result = parse_layer_conf(layer_conf_text)
        assert "BBFILE_PRIORITY" in result
        assert "LAYERSERIES_COMPAT" in result
        assert "LAYERVERSION" in result

    def test_empty_string_returns_empty(self) -> None:
        assert parse_layer_conf("") == {}

    def test_whitespace_only_returns_empty(self) -> None:
        assert parse_layer_conf("   \n  \t  \n") == {}

    def test_malformed_returns_empty(self) -> None:
        """Malformed text with no recognizable fields returns {}."""
        result = parse_layer_conf("this is not layer.conf\njunk\n123\n")
        assert result == {}

    def test_partial_conf_priority_only(self) -> None:
        """Conf with only BBFILE_PRIORITY returns just that key."""
        text = 'BBFILE_PRIORITY_mylay = "9"\n'
        result = parse_layer_conf(text)
        assert result == {"BBFILE_PRIORITY": "9"}
        assert "LAYERSERIES_COMPAT" not in result
        assert "LAYERVERSION" not in result

    def test_partial_conf_no_version(self) -> None:
        """Conf without LAYERVERSION returns PRIORITY and COMPAT only."""
        text = (
            'BBFILE_PRIORITY_foo = "5"\n'
            'LAYERSERIES_COMPAT_foo = "styhead"\n'
        )
        result = parse_layer_conf(text)
        assert result.get("BBFILE_PRIORITY") == "5"
        assert result.get("LAYERSERIES_COMPAT") == "styhead"
        assert "LAYERVERSION" not in result

    def test_does_not_raise_on_none_like_edge_case(self) -> None:
        """Extra-defensive: a string of only newlines returns {}."""
        result = parse_layer_conf("\n\n\n")
        assert result == {}

    def test_returns_dict_type(self, layer_conf_text: str) -> None:
        result = parse_layer_conf(layer_conf_text)
        assert isinstance(result, dict)

    def test_raspberrypi_layer_style(self) -> None:
        """Match the layer.conf format from meta-raspberrypi."""
        text = (
            'BBFILE_PRIORITY_raspberrypi = "9"\n'
            'LAYERSERIES_COMPAT_raspberrypi = "wrynose"\n'
        )
        result = parse_layer_conf(text)
        assert result["BBFILE_PRIORITY"] == "9"
        assert result["LAYERSERIES_COMPAT"] == "wrynose"
        assert "LAYERVERSION" not in result

    def test_multi_compat_releases(self) -> None:
        """Multiple release names in LAYERSERIES_COMPAT are returned verbatim."""
        text = 'LAYERSERIES_COMPAT_bar = "scarthgap whinlatter wrynose"\n'
        result = parse_layer_conf(text)
        assert result.get("LAYERSERIES_COMPAT") == "scarthgap whinlatter wrynose"
