"""Unit tests for bakar.workspace_config."""

from __future__ import annotations

import re
import textwrap
from typing import TYPE_CHECKING

import pytest

from bakar import workspace_config
from bakar.workspace_config import WorkspaceConfig, load_workspace_config

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

# Peridio's exact comment-only .bakar.toml content. A real-world generic-family
# workspace that must parse to all-defaults without raising.
PERIDIO_MARKER = (
    "# bakar workspace marker for the Peridio Yocto workspace.\n"
    "# The bitbake tree shipped with this workspace lives at ./bitbake/ and is\n"
    "# picked up automatically by bakar for hashserv binary discovery."
)


@pytest.mark.unit
def test_missing_file_returns_defaults(tmp_path: Path) -> None:
    result = load_workspace_config(tmp_path)
    assert result == WorkspaceConfig()
    assert result.nxp_manifest is None
    assert result.nxp_machine is None
    assert result.ti_manifest is None
    assert result.generic_kas_yaml is None
    assert result.generic_machine is None


@pytest.mark.unit
def test_comment_only_file_returns_defaults(tmp_path: Path) -> None:
    """Peridio's comment-only marker parses to all-defaults, no raise."""
    (tmp_path / ".bakar.toml").write_text(PERIDIO_MARKER)

    result = load_workspace_config(tmp_path)

    assert result == WorkspaceConfig()


@pytest.mark.unit
def test_nxp_section_only_populates_nxp_fields(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        # bakar workspace root.

        [defaults.nxp]
        manifest = "imx-6.6.52-2.2.2.xml"
        machine  = "imx8mp-var-dart"
        distro   = "fsl-imx-xwayland"
        image    = "core-image-minimal"
    """)
    (tmp_path / ".bakar.toml").write_text(toml_content)

    cfg = load_workspace_config(tmp_path)

    assert cfg.nxp_manifest == "imx-6.6.52-2.2.2.xml"
    assert cfg.nxp_machine == "imx8mp-var-dart"
    assert cfg.nxp_distro == "fsl-imx-xwayland"
    assert cfg.nxp_image == "core-image-minimal"
    # Other families stay at defaults.
    assert cfg.ti_manifest is None
    assert cfg.ti_machine is None
    assert cfg.generic_kas_yaml is None
    assert cfg.generic_machine is None


@pytest.mark.unit
def test_nxp_and_ti_sections_populate_both(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        # bakar workspace root.

        [defaults.nxp]
        manifest = "imx-6.6.52-2.2.2.xml"
        machine  = "imx8mp-var-dart"

        [defaults.ti]
        manifest = "processor-sdk-scarthgap.txt"
        machine  = "am62x-var-som"
        distro   = "arago"
        image    = "var-thin-image"
    """)
    (tmp_path / ".bakar.toml").write_text(toml_content)

    cfg = load_workspace_config(tmp_path)

    assert cfg.nxp_manifest == "imx-6.6.52-2.2.2.xml"
    assert cfg.nxp_machine == "imx8mp-var-dart"
    assert cfg.ti_manifest == "processor-sdk-scarthgap.txt"
    assert cfg.ti_machine == "am62x-var-som"
    assert cfg.ti_distro == "arago"
    assert cfg.ti_image == "var-thin-image"
    # NXP distro/image untouched.
    assert cfg.nxp_distro is None
    assert cfg.nxp_image is None


@pytest.mark.unit
def test_invalid_toml_raises_valueerror_with_path(tmp_path: Path) -> None:
    config_file = tmp_path / ".bakar.toml"
    config_file.write_text("not valid toml [[[[\n")

    with pytest.raises(ValueError, match=re.escape(str(config_file))):
        load_workspace_config(tmp_path)


@pytest.mark.unit
def test_type_mismatch_raises_valueerror_with_path(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        [defaults.nxp]
        machine = 123
    """)
    config_file = tmp_path / ".bakar.toml"
    config_file.write_text(toml_content)

    with pytest.raises(ValueError, match=re.escape(str(config_file))):
        load_workspace_config(tmp_path)


@pytest.mark.unit
def test_unknown_key_in_known_section_is_ignored(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        [defaults.nxp]
        machine = "imx93-var-som"
        bogus_key = "ignored"
    """)
    (tmp_path / ".bakar.toml").write_text(toml_content)

    cfg = load_workspace_config(tmp_path)

    assert cfg.nxp_machine == "imx93-var-som"
    assert not hasattr(cfg, "bogus_key")


@pytest.mark.unit
def test_unknown_section_is_ignored(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        [defaults.unknown_family]
        machine = "mystery-board"

        [defaults.nxp]
        machine = "imx93-var-som"
    """)
    (tmp_path / ".bakar.toml").write_text(toml_content)

    cfg = load_workspace_config(tmp_path)

    assert cfg.nxp_machine == "imx93-var-som"
    # Nothing from the unknown family leaked in.
    assert cfg.generic_machine is None
    assert cfg.ti_machine is None


# Round-trip tests for write_workspace_config (task 1.2). They skip until the
# writer lands, then verify write -> load preserves every value.
_has_writer = hasattr(workspace_config, "write_workspace_config")
_requires_writer = pytest.mark.skipif(
    not _has_writer,
    reason="write_workspace_config not yet implemented (task 1.2)",
)


@pytest.mark.unit
@_requires_writer
def test_write_load_round_trip_nxp(tmp_path: Path) -> None:
    settings = {
        "manifest": "imx-6.6.52-2.2.2.xml",
        "machine": "imx8mp-var-dart",
        "distro": "fsl-imx-xwayland",
        "image": "core-image-minimal",
    }
    workspace_config.write_workspace_config(tmp_path, "nxp", settings)

    cfg = load_workspace_config(tmp_path)

    assert cfg.nxp_manifest == "imx-6.6.52-2.2.2.xml"
    assert cfg.nxp_machine == "imx8mp-var-dart"
    assert cfg.nxp_distro == "fsl-imx-xwayland"
    assert cfg.nxp_image == "core-image-minimal"


@pytest.mark.unit
@_requires_writer
def test_write_load_round_trip_ti(tmp_path: Path) -> None:
    settings = {
        "manifest": "processor-sdk-scarthgap.txt",
        "machine": "am62x-var-som",
        "distro": "arago",
        "image": "var-thin-image",
    }
    workspace_config.write_workspace_config(tmp_path, "ti", settings)

    cfg = load_workspace_config(tmp_path)

    assert cfg.ti_manifest == "processor-sdk-scarthgap.txt"
    assert cfg.ti_machine == "am62x-var-som"
    assert cfg.ti_distro == "arago"
    assert cfg.ti_image == "var-thin-image"


@pytest.mark.unit
@_requires_writer
def test_write_load_round_trip_generic(tmp_path: Path) -> None:
    settings = {
        "kas_yaml": "avocado-bspctl.yml",
        "machine": "qemux86-64",
    }
    workspace_config.write_workspace_config(tmp_path, "generic", settings)

    cfg = load_workspace_config(tmp_path)

    assert cfg.generic_kas_yaml == "avocado-bspctl.yml"
    assert cfg.generic_machine == "qemux86-64"
