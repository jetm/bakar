from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

_VALID_FAMILIES = {"nxp", "ti", "generic", "bbsetup"}
_MAX_REGEX_LEN = 200


@dataclass
class VendorEntry:
    name: str
    family: str
    manifest_regex: str
    repo_url: str | None = None
    kas_container_image: str | None = None
    default_machine: str | None = None
    default_distro: str | None = None
    default_image: str | None = None
    default_manifest: str | None = None
    default_branch: str | None = None
    branch_by_manifest_prefix: dict[str, str] | None = None
    tuning_overlay: str | None = None

    def __post_init__(self) -> None:
        if self.family not in _VALID_FAMILIES:
            raise ValueError(
                f"VendorEntry '{self.name}': family must be one of {sorted(_VALID_FAMILIES)}, got '{self.family}'"
            )
        if len(self.manifest_regex) > _MAX_REGEX_LEN:
            raise ValueError(
                f"VendorEntry '{self.name}': manifest_regex exceeds"
                f" {_MAX_REGEX_LEN} characters (got {len(self.manifest_regex)})"
            )
        try:
            re.compile(self.manifest_regex)
        except re.error as exc:
            raise ValueError(
                f"VendorEntry '{self.name}': manifest_regex is not a valid regular expression: {exc}"
            ) from exc


def load_vendors(path: Path | None = None) -> list[VendorEntry]:
    """Load vendor entries from a TOML config file.

    Returns an empty list if the file does not exist.
    """
    if path is None:
        path = Path.home() / ".config" / "bakar" / "vendors.toml"

    if not path.exists():
        return []

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: invalid TOML: {exc}") from exc

    try:
        entries = [
            VendorEntry(**{("kas_container_image" if k == "container_image" else k): v for k, v in item.items()})
            for item in data.get("vendors", [])
        ]
    except TypeError as exc:
        raise ValueError(f"{path}: invalid vendor entry: {exc}") from exc

    return entries


def load_vendor_presets(path: Path | None = None) -> list[dict]:
    """Load vendor-shipped preset dicts from a TOML config file.

    Reads the [[presets]] array-of-tables from vendors.toml and returns
    raw dicts for further processing by load_presets() in preset_config.py.
    Returns an empty list if the file does not exist or has no [[presets]]
    section.
    """
    if path is None:
        path = Path.home() / ".config" / "bakar" / "vendors.toml"

    if not path.exists():
        return []

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: invalid TOML: {exc}") from exc

    return data.get("presets", [])
