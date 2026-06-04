from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from bakar.vendor_config import load_vendor_presets

_VALID_FAMILIES = {"nxp", "ti", "generic", "bbsetup"}


@dataclass
class PresetSpec:
    """One resolved release from a PresetEntry.

    nxp/ti releases carry manifest + branch; bbsetup/generic releases carry
    kas_yaml.  machine/distro/image are optional on all families.
    """

    family: str
    manifest: str | None = None
    branch: str | None = None
    kas_yaml: Path | None = None
    machine: str | None = None
    distro: str | None = None
    image: str | None = None


@dataclass
class PresetEntry:
    name: str
    family: str
    machine: str | None = None
    distro: str | None = None
    image: str | None = None
    manifest: str | None = None
    branch: str | None = None
    manifests: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    kas_yaml: str | None = None
    kas_yamls: list[str] = field(default_factory=list)
    container_image: str | None = None
    tuning_overlay: str | None = None

    def __post_init__(self) -> None:
        if self.family not in _VALID_FAMILIES:
            raise ValueError(
                f"PresetEntry '{self.name}': family must be one of {sorted(_VALID_FAMILIES)}, got '{self.family}'"
            )

        has_single = bool(self.manifest or self.kas_yaml)
        has_multi = bool(self.manifests or self.kas_yamls)

        if (self.manifest and self.manifests) or (self.kas_yaml and self.kas_yamls):
            raise ValueError(
                f"PresetEntry '{self.name}': set single-release or multi-release fields, not both"
                " (manifest vs manifests, or kas_yaml vs kas_yamls)"
            )

        if not has_single and not has_multi:
            raise ValueError(
                f"PresetEntry '{self.name}': specifies no build target"
                " (set manifest/branch for nxp/ti, kas_yaml for generic/bbsetup,"
                " or the plural multi-release equivalents)"
            )

        # Family-specific field checks so resolve() never hits Path(None) or manifest=None.
        if self.family in {"nxp", "ti"}:
            if self.kas_yaml and not self.manifest:
                raise ValueError(
                    f"PresetEntry '{self.name}': family '{self.family}' requires"
                    " 'manifest' (not 'kas_yaml') for single-release builds"
                )
            if self.kas_yamls and not self.manifests:
                raise ValueError(
                    f"PresetEntry '{self.name}': family '{self.family}' requires"
                    " 'manifests' (not 'kas_yamls') for multi-release builds"
                )
        if self.family in {"generic", "bbsetup"}:
            if self.manifest and not self.kas_yaml:
                raise ValueError(
                    f"PresetEntry '{self.name}': family '{self.family}' requires"
                    " 'kas_yaml' (not 'manifest') for single-release builds"
                )
            if self.manifests and not self.kas_yamls:
                raise ValueError(
                    f"PresetEntry '{self.name}': family '{self.family}' requires"
                    " 'kas_yamls' (not 'manifests') for multi-release builds"
                )

        if self.family in {"nxp", "ti"}:
            if self.manifests and len(self.manifests) != len(self.branches):
                raise ValueError(
                    f"PresetEntry '{self.name}': manifests and branches must have the same length"
                    f" (got {len(self.manifests)} manifests and {len(self.branches)} branches)"
                )
            if self.manifest and self.branches:
                raise ValueError(
                    f"PresetEntry '{self.name}': single 'manifest' cannot be paired with plural 'branches'"
                    " — use 'manifests' and 'branches' together for multi-release builds"
                )

    def resolve(self) -> list[PresetSpec]:
        """Return one PresetSpec per release defined by this entry."""
        if self.family in {"nxp", "ti"}:
            manifests = self.manifests or [self.manifest]
            branches = self.branches or [self.branch]
            return [
                PresetSpec(
                    family=self.family,
                    manifest=m,
                    branch=b,
                    machine=self.machine,
                    distro=self.distro,
                    image=self.image,
                )
                for m, b in zip(manifests, branches, strict=True)
            ]
        # bbsetup / generic
        kas_yamls = self.kas_yamls or [self.kas_yaml]
        return [
            PresetSpec(
                family=self.family,
                kas_yaml=Path(ky),  # type: ignore[arg-type]
                machine=self.machine,
                image=self.image,
            )
            for ky in kas_yamls
        ]


def load_presets(config_path: Path | None = None, vendors_path: Path | None = None) -> list[PresetEntry]:
    """Load named presets from config.toml and vendors.toml.

    Reads [[presets]] from ~/.config/bakar/config.toml and [[presets]] from
    ~/.config/bakar/vendors.toml (via load_vendor_presets), merges them, and
    raises ValueError naming any duplicate preset name across both sources.
    Returns [] when neither file has a [[presets]] table. Propagates parse
    errors raw.
    """
    if config_path is None:
        config_path = Path.home() / ".config" / "bakar" / "config.toml"

    user_dicts: list[dict] = []
    if config_path.exists():
        with config_path.open("rb") as f:
            data = tomllib.load(f)
        user_dicts = data.get("presets", [])

    vendor_dicts: list[dict] = load_vendor_presets(vendors_path)

    # Duplicate name detection across both sources.
    user_names = {d["name"] for d in user_dicts if "name" in d}
    for d in vendor_dicts:
        vendor_name = d.get("name")
        if vendor_name and vendor_name in user_names:
            raise ValueError(f"Duplicate preset name '{vendor_name}' found in both config.toml and vendors.toml")

    entries = []
    for d in user_dicts + vendor_dicts:
        if "name" not in d:
            raise ValueError("Preset entry is missing required 'name' field")
        try:
            entries.append(PresetEntry(**d))
        except TypeError as exc:
            raise ValueError(f"Invalid preset entry: {exc}") from exc
    return entries
