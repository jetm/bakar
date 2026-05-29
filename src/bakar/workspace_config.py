from __future__ import annotations

import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_STR_FIELDS = {
    "nxp_manifest",
    "nxp_machine",
    "nxp_distro",
    "nxp_image",
    "ti_manifest",
    "ti_machine",
    "ti_distro",
    "ti_image",
    "generic_kas_yaml",
    "generic_machine",
}


@dataclass
class WorkspaceConfig:
    # [defaults.nxp]
    nxp_manifest: str | None = None
    nxp_machine: str | None = None
    nxp_distro: str | None = None
    nxp_image: str | None = None
    # [defaults.ti]
    ti_manifest: str | None = None
    ti_machine: str | None = None
    ti_distro: str | None = None
    ti_image: str | None = None
    # [defaults.generic]
    generic_kas_yaml: str | None = None
    generic_machine: str | None = None


# Maps a (section, key) pair onto a WorkspaceConfig field name. The nxp_/ti_/
# generic_ prefixes keep the dataclass flat (one field per TOML key) so
# config.resolve()'s pick() calls map one-to-one without restructuring. Mirrors
# the schema and key names used by user_config.py's config.toml.
_NXP_KEYS = {
    "manifest": "nxp_manifest",
    "machine": "nxp_machine",
    "distro": "nxp_distro",
    "image": "nxp_image",
}
_TI_KEYS = {
    "manifest": "ti_manifest",
    "machine": "ti_machine",
    "distro": "ti_distro",
    "image": "ti_image",
}
_GENERIC_KEYS = {
    "kas_yaml": "generic_kas_yaml",
    "machine": "generic_machine",
}


def _check_type(field: str, value: object, path: Path) -> None:
    if field in _STR_FIELDS and not isinstance(value, str):
        raise ValueError(f"{path}: '{field}' must be a string, got {type(value).__name__}")


def load_workspace_config(workspace: Path) -> WorkspaceConfig:
    """Load ``<workspace>/.bakar.toml`` into a :class:`WorkspaceConfig`.

    Returns an all-defaults ``WorkspaceConfig()`` when the file is absent or
    carries no ``[defaults.<family>]`` sections (e.g. a comment-only marker
    file). Raises ``ValueError`` (with the file path in the message) on a TOML
    parse error or a type mismatch. Unknown keys and unknown sections are
    silently ignored.
    """
    path = workspace / ".bakar.toml"

    if not path.exists():
        return WorkspaceConfig()

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: invalid TOML: {exc}") from exc

    values: dict[str, object] = {}

    defaults = data.get("defaults", {})
    if isinstance(defaults, dict):
        for section, mapping in (("nxp", _NXP_KEYS), ("ti", _TI_KEYS), ("generic", _GENERIC_KEYS)):
            section_data = defaults.get(section, {})
            if not isinstance(section_data, dict):
                continue
            for key, field in mapping.items():
                if key in section_data:
                    _check_type(field, section_data[key], path)
                    values[field] = section_data[key]

    return WorkspaceConfig(**values)
