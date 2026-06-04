from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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

        # Family-specific single-release field check: nxp/ti must use manifest,
        # bbsetup/generic must use kas_yaml. This prevents Path(None) in resolve().
        if self.family in {"nxp", "ti"} and self.kas_yaml and not self.manifest:
            raise ValueError(
                f"PresetEntry '{self.name}': family '{self.family}' requires"
                " 'manifest' (not 'kas_yaml') for single-release builds"
            )
        if self.family in {"generic", "bbsetup"} and self.manifest and not self.kas_yaml:
            raise ValueError(
                f"PresetEntry '{self.name}': family '{self.family}' requires"
                " 'kas_yaml' (not 'manifest') for single-release builds"
            )

        if self.family in {"nxp", "ti"} and self.manifests and len(self.manifests) != len(self.branches):
            raise ValueError(
                f"PresetEntry '{self.name}': manifests and branches must have the same length"
                f" (got {len(self.manifests)} manifests and {len(self.branches)} branches)"
            )

    def resolve(self) -> list[PresetSpec]:
        """Return one PresetSpec per release defined by this entry."""
        if self.family in {"nxp", "ti"}:
            if self.manifests:
                return [
                    PresetSpec(
                        family=self.family,
                        manifest=m,
                        branch=b,
                        machine=self.machine,
                        distro=self.distro,
                        image=self.image,
                    )
                    for m, b in zip(self.manifests, self.branches, strict=True)
                ]
            return [
                PresetSpec(
                    family=self.family,
                    manifest=self.manifest,
                    branch=self.branch,
                    machine=self.machine,
                    distro=self.distro,
                    image=self.image,
                )
            ]
        # bbsetup / generic
        if self.kas_yamls:
            return [
                PresetSpec(
                    family=self.family,
                    kas_yaml=Path(ky),
                    machine=self.machine,
                    image=self.image,
                )
                for ky in self.kas_yamls
            ]
        return [
            PresetSpec(
                family=self.family,
                kas_yaml=Path(self.kas_yaml),  # type: ignore[arg-type]
                machine=self.machine,
                image=self.image,
            )
        ]
