from __future__ import annotations

import tomllib
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING

import tomli_w

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
    "kas_container_image",
}

# [build] booleans; workspace tier (None = not set, falls back to user config).
_BOOL_FIELDS = {
    "ccache",
    "rm_work",
}

# Host-threshold fields are numeric (reject non-numeric, reject non-positive).
_HOST_FIELDS = {
    "host_inotify_instances",
    "host_inotify_watches",
    "host_swappiness_max",
    "host_nofile_soft",
    "host_mem_min_gb",
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
    # [build] - workspace-tier override; None means "not set" (falls back to
    # the user config then the built-in default in config.resolve()).
    kas_container_image: str | None = None
    # [build] booleans, workspace tier. None = not set -> user config -> default
    # (ccache default True, rm_work default False) in config.resolve().
    ccache: bool | None = None
    rm_work: bool | None = None
    # [host] - workspace-tier override; None means "not set" (falls back to
    # the user config then the built-in floor in config.resolve()).
    host_inotify_instances: int | None = None
    host_inotify_watches: int | None = None
    host_swappiness_max: int | None = None
    host_nofile_soft: int | None = None
    host_mem_min_gb: float | None = None


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
# Top-level [build] table -> build fields. Not under [defaults]; the container
# image is not family-scoped. Mirrors user_config.py's _BUILD_KEYS subset.
_BUILD_KEYS = {
    "kas_container_image": "kas_container_image",
    "ccache": "ccache",
    "rm_work": "rm_work",
}
# Top-level [host] table -> host_* fields. Not under [defaults]; host thresholds
# are not family-scoped. Mirrors user_config.py's _HOST_KEYS.
_HOST_KEYS = {
    "inotify_instances": "host_inotify_instances",
    "inotify_watches": "host_inotify_watches",
    "swappiness_max": "host_swappiness_max",
    "nofile_soft": "host_nofile_soft",
    "mem_min_gb": "host_mem_min_gb",
}


def _check_type(field: str, value: object, path: Path) -> None:
    if field in _STR_FIELDS and not isinstance(value, str):
        raise ValueError(f"{path}: '{field}' must be a string, got {type(value).__name__}")
    if field in _BOOL_FIELDS and not isinstance(value, bool):
        raise ValueError(f"{path}: '{field}' must be a boolean, got {type(value).__name__}")
    if field in _HOST_FIELDS:
        # bool is an int subclass; reject it explicitly so True/False can't pose
        # as a count.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path}: '{field}' must be a number, got {type(value).__name__}")
        if field != "host_mem_min_gb" and not isinstance(value, int):
            # The four count fields are int-only, matching UserConfig's _INT_FIELDS
            # rejection; without this a float would silently truncate in resolve().
            raise ValueError(f"{path}: '{field}' must be an integer, got {type(value).__name__}")
        if value <= 0:
            raise ValueError(f"{path}: '{field}' must be positive, got {value}")


def _require_table(value: object, name: str, path: Path) -> dict:
    """Return ``value`` as a dict, raising when it is present but not a table.

    An absent section arrives here as the ``{}`` default and passes through. A
    scalar like ``build = "x"`` is rejected loudly instead of being silently
    skipped, which would otherwise drop the whole section's overrides.
    """
    if not isinstance(value, dict):
        # ValueError, not TypeError: the loader's whole contract is ValueError for
        # malformed config (parse error, _check_type mismatch), and callers catch
        # ValueError. A non-table section is the same error class.
        raise ValueError(f"{path}: '{name}' must be a table, got {type(value).__name__}")  # noqa: TRY004
    return value


def load_workspace_config(workspace: Path) -> WorkspaceConfig:
    """Load ``<workspace>/.bakar.toml`` into a :class:`WorkspaceConfig`.

    Returns an all-defaults ``WorkspaceConfig()`` when the file is absent or
    carries no recognized sections (e.g. a comment-only marker file). Raises
    ``ValueError`` (with the file path in the message) on a TOML parse error or
    a type mismatch. Reads ``[defaults.<family>]`` build targets, a top-level
    ``[build]`` table (``kas_container_image``), and a top-level ``[host]``
    table of numeric thresholds. An unrecognized key under
    a recognized ``[defaults.<family>]`` section or under ``[build]`` or
    ``[host]`` emits a
    :class:`UserWarning` naming the unknown key and the recognized keys, then is
    ignored; unrecognized top-level sections are ignored without warning. A
    recognized section (``[defaults]``, ``[defaults.<family>]``, ``[build]``,
    ``[host]``) that is present but not a table raises ``ValueError``.
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

    defaults = _require_table(data.get("defaults", {}), "defaults", path)
    for section, mapping in (("nxp", _NXP_KEYS), ("ti", _TI_KEYS), ("generic", _GENERIC_KEYS)):
        section_data = _require_table(defaults.get(section, {}), f"defaults.{section}", path)
        for key in section_data:
            if key not in mapping:
                recognized = ", ".join(sorted(mapping))
                warnings.warn(
                    f"{path}: unknown key '{key}' in [defaults.{section}]; recognized keys: {recognized}",
                    stacklevel=2,
                )
                continue
            _check_type(mapping[key], section_data[key], path)
            values[mapping[key]] = section_data[key]

    build = _require_table(data.get("build", {}), "build", path)
    for key in build:
        if key not in _BUILD_KEYS:
            recognized = ", ".join(sorted(_BUILD_KEYS))
            warnings.warn(
                f"{path}: unknown key '{key}' in [build]; recognized keys: {recognized}",
                stacklevel=2,
            )
            continue
        _check_type(_BUILD_KEYS[key], build[key], path)
        values[_BUILD_KEYS[key]] = build[key]

    host = _require_table(data.get("host", {}), "host", path)
    for key in host:
        if key not in _HOST_KEYS:
            recognized = ", ".join(sorted(_HOST_KEYS))
            warnings.warn(
                f"{path}: unknown key '{key}' in [host]; recognized keys: {recognized}",
                stacklevel=2,
            )
            continue
        _check_type(_HOST_KEYS[key], host[key], path)
        values[_HOST_KEYS[key]] = host[key]

    return WorkspaceConfig(**values)


def write_workspace_config(workspace: Path, family: str, settings: dict[str, str]) -> None:
    """Write ``<workspace>/.bakar.toml`` with one ``[defaults.<family>]`` section.

    Emits a leading comment line, exactly one ``[defaults.<family>]`` table, and
    one ``key = value`` line per entry in ``settings``. The keys are the bare
    TOML key names (``manifest``, ``machine``, ``distro``, ``image``,
    ``kas_yaml``) that :func:`load_workspace_config` reads back, so the file
    round-trips: writing ``{"machine": "X"}`` for family ``"nxp"`` then loading
    yields ``WorkspaceConfig(nxp_machine="X")``.
    """
    path = workspace / ".bakar.toml"
    data = {"defaults": {family: dict(settings)}}
    with path.open("wb") as f:
        f.write(b"# bakar workspace root.\n\n")
        tomli_w.dump(data, f)
