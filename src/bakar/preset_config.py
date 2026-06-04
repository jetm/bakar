from __future__ import annotations

from dataclasses import dataclass, field

_VALID_FAMILIES = {"nxp", "ti", "generic", "bbsetup"}


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

        if self.family in {"nxp", "ti"} and self.manifests and len(self.manifests) != len(self.branches):
            raise ValueError(
                f"PresetEntry '{self.name}': manifests and branches must have the same length"
                f" (got {len(self.manifests)} manifests and {len(self.branches)} branches)"
            )
