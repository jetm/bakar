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
