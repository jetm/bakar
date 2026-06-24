from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomli_w

# Bumped whenever a forward migration is added below. A config.toml without a
# config_version field is treated as version 0 and migrated to this version.
CURRENT_CONFIG_VERSION = 3

_STR_FIELDS = {
    "nxp_machine",
    "nxp_distro",
    "nxp_image",
    "nxp_manifest",
    "nxp_repo_url",
    "ti_machine",
    "ti_distro",
    "ti_image",
    "ti_manifest",
    "kas_container_image",
    "dl_dir",
    "sstate_dir",
    "sstate_mirrors",
    "sstate_mirror_url",
    "scheduler",
    "ccache_dir",
    "sccache_scheduler_url",
}
_BOOL_FIELDS = {
    "show_doctor_report",
    "show_hashes",
    "show_sstate_summary",
    "hashserv",
    "ccache_shared",
    "psi_autocalibrate",
    "sccache_dist",
}
_INT_FIELDS: set[str] = {
    "stall_abort_secs",
    "host_inotify_instances",
    "host_inotify_watches",
    "host_swappiness_max",
    "host_nofile_soft",
}
_PSI_FIELDS = {"pressure_max_cpu", "pressure_max_io", "pressure_max_memory"}
# The five [host] threshold fields; all require a strictly positive value.
_HOST_FIELDS = {
    "host_inotify_instances",
    "host_inotify_watches",
    "host_swappiness_max",
    "host_nofile_soft",
    "host_mem_min_gb",
}


@dataclass
class UserConfig:
    # [defaults.nxp]
    nxp_machine: str | None = None
    nxp_distro: str | None = None
    nxp_image: str | None = None
    nxp_manifest: str | None = None
    nxp_repo_url: str | None = None
    # [defaults.ti]
    ti_machine: str | None = None
    ti_distro: str | None = None
    ti_image: str | None = None
    ti_manifest: str | None = None
    # [build]
    kas_container_image: str | None = None
    show_doctor_report: bool = True
    dl_dir: str | None = None
    sstate_dir: str | None = None
    sstate_mirrors: str | None = None
    sstate_mirror_url: str | None = None
    scheduler: str | None = None
    sccache_dist: bool = False
    sccache_scheduler_url: str | None = None
    pressure_max_cpu: float | None = None
    pressure_max_io: float | None = None
    pressure_max_memory: float | None = None
    disk_free_threshold_gb: float = 50.0
    # Abort the build when every running task's log has been silent this many
    # seconds (a wedged task, e.g. a deadlocked final link). 0 disables the guard.
    stall_abort_secs: int = 2700
    hashserv: bool = False
    ccache_shared: bool = False
    ccache_dir: str | None = None
    psi_autocalibrate: bool = False
    # [layers]
    show_hashes: bool = False
    show_sstate_summary: bool = False
    # [host] doctor thresholds; defaults equal today's hardcoded literals in
    # diagnostics.py so verdicts are byte-identical until a value is written.
    host_inotify_instances: int = 4096
    host_inotify_watches: int = 524288
    host_swappiness_max: int = 20
    host_nofile_soft: int = 8192
    host_mem_min_gb: float = 16.0
    # Schema version of the on-disk config.toml this object was loaded from.
    config_version: int = CURRENT_CONFIG_VERSION


# Maps a (section, key) pair onto a UserConfig field name. The nxp_/ti_ prefixes
# keep the dataclass flat (one field per TOML key) so config.resolve()'s pick()
# calls map one-to-one without restructuring.
_NXP_KEYS = {
    "machine": "nxp_machine",
    "distro": "nxp_distro",
    "image": "nxp_image",
    "manifest": "nxp_manifest",
    "repo_url": "nxp_repo_url",
}
_TI_KEYS = {
    "machine": "ti_machine",
    "distro": "ti_distro",
    "image": "ti_image",
    "manifest": "ti_manifest",
}
_BUILD_KEYS = {
    "kas_container_image": "kas_container_image",
    "show_doctor_report": "show_doctor_report",
    "dl_dir": "dl_dir",
    "sstate_dir": "sstate_dir",
    "sstate_mirrors": "sstate_mirrors",
    "sstate_mirror_url": "sstate_mirror_url",
    "scheduler": "scheduler",
    "sccache_dist": "sccache_dist",
    "sccache_scheduler_url": "sccache_scheduler_url",
    "pressure_max_cpu": "pressure_max_cpu",
    "pressure_max_io": "pressure_max_io",
    "pressure_max_memory": "pressure_max_memory",
    "disk_free_threshold_gb": "disk_free_threshold_gb",
    "stall_abort_secs": "stall_abort_secs",
    "hashserv": "hashserv",
    "ccache_shared": "ccache_shared",
    "ccache_dir": "ccache_dir",
    "psi_autocalibrate": "psi_autocalibrate",
}
_LAYERS_KEYS = {
    "show_hashes": "show_hashes",
    "show_sstate_summary": "show_sstate_summary",
}
# Top-level [host] table -> host_* fields. Unlike [defaults.<family>] this is
# not family-scoped, so it parses from the document root, not under [defaults].
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
    # bool is a subclass of int; reject ints that are not bools explicitly.
    if field in _BOOL_FIELDS and not isinstance(value, bool):
        raise ValueError(f"{path}: '{field}' must be a boolean, got {type(value).__name__}")
    # bool is a subclass of int; test isinstance(value, bool) first to reject it.
    if field in _INT_FIELDS and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValueError(f"{path}: '{field}' must be an integer, got {type(value).__name__}")
    if field == "stall_abort_secs" and isinstance(value, int) and not isinstance(value, bool) and value < 0:
        raise ValueError(f"{path}: '{field}' must be >= 0 (0 disables), got {value}")
    if field in _PSI_FIELDS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path}: '{field}' must be a number, got {type(value).__name__}")
        if value < 1:
            raise ValueError(f"{path}: '{field}' must be >= 1 (bitbake minimum), got {value}")
    if field == "disk_free_threshold_gb":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{path}: '{field}' must be > 0, got {value}")
    if field in _HOST_FIELDS:
        # All five host thresholds must be a positive number. The four int
        # fields already passed the _INT_FIELDS type check above, so this number
        # guard only bites for host_mem_min_gb; the positivity check is shared.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path}: '{field}' must be a number, got {type(value).__name__}")
        if value <= 0:
            raise ValueError(f"{path}: '{field}' must be > 0, got {value}")


def _migrate_config(raw: dict[str, object], from_version: int) -> dict[str, object]:
    """Apply incremental forward migrations to a raw config dict.

    Walks one version at a time from ``from_version`` up to
    :data:`CURRENT_CONFIG_VERSION`, mutating ``raw`` in place per step, and
    stamps the resulting ``config_version`` on the returned dict. Each future
    schema bump adds one ``if migrated < N:`` block here.

    Version 0 -> 1 is the baseline: no field reshaping is needed, the migration
    only records the version, so configs predating the version field load
    cleanly.
    """
    migrated = from_version
    # Version 0 -> 1: no structural change; the field is simply stamped below.
    if migrated < 1:
        migrated = 1
    # Version 1 -> 2: the legacy [build] doctor toggle (skip doctor entirely) is
    # replaced by show_doctor_report (always run, hide the report). A user who had
    # doctor=false wanted a quiet pre-flight, which now maps to show_doctor_report=false.
    if migrated < 2:
        build = raw.get("build")
        if isinstance(build, dict) and "doctor" in build:
            if build.pop("doctor") is False:
                build["show_doctor_report"] = False
        migrated = 2
    # Version 2 -> 3: [build] container_image renamed to kas_container_image to match
    # the KAS_CONTAINER_IMAGE env var it mirrors.
    if migrated < 3:
        build = raw.get("build")
        if isinstance(build, dict) and "container_image" in build:
            if "kas_container_image" not in build:
                build["kas_container_image"] = build.pop("container_image")
            else:
                build.pop("container_image")
        migrated = 3
    raw["config_version"] = migrated
    return raw


def load_user_config(path: Path | None = None) -> UserConfig:
    """Load ``~/.config/bakar/config.toml`` into a :class:`UserConfig`.

    Returns an all-defaults ``UserConfig()`` when the file is absent. Raises
    ``ValueError`` (with the config path in the message) on a TOML parse error
    or a type mismatch (e.g. a string field given a non-string value).

    A config without a ``config_version`` field is treated as version 0 and
    migrated forward to :data:`CURRENT_CONFIG_VERSION`, persisting the migrated
    form back to disk. A ``config_version`` greater than
    :data:`CURRENT_CONFIG_VERSION` raises ``ValueError`` naming the unsupported
    version. A config already at the current version is loaded unchanged.
    """
    if path is None:
        path = Path.home() / ".config" / "bakar" / "config.toml"

    if not path.exists():
        return UserConfig()

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: invalid TOML: {exc}") from exc

    raw_version = data.get("config_version", 0)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise ValueError(f"{path}: 'config_version' must be an integer, got {type(raw_version).__name__}")  # noqa: TRY004
    if raw_version > CURRENT_CONFIG_VERSION:
        raise ValueError(
            f"{path}: config_version {raw_version} is newer than this bakar supports "
            f"(max {CURRENT_CONFIG_VERSION}); upgrade bakar to load it"
        )
    if raw_version < CURRENT_CONFIG_VERSION:
        data = _migrate_config(data, raw_version)
        _dump_raw(path, data)

    values: dict[str, object] = {}

    defaults = data.get("defaults", {})
    if isinstance(defaults, dict):
        for section, mapping in (("nxp", _NXP_KEYS), ("ti", _TI_KEYS)):
            section_data = defaults.get(section, {})
            if not isinstance(section_data, dict):
                continue
            for key, field in mapping.items():
                if key in section_data:
                    _check_type(field, section_data[key], path)
                    values[field] = section_data[key]

    for section, mapping in (("build", _BUILD_KEYS), ("layers", _LAYERS_KEYS), ("host", _HOST_KEYS)):
        section_data = data.get(section, {})
        if not isinstance(section_data, dict):
            continue
        for key, field in mapping.items():
            if key in section_data:
                _check_type(field, section_data[key], path)
                values[field] = section_data[key]

    values["config_version"] = CURRENT_CONFIG_VERSION
    return UserConfig(**values)


@dataclass(frozen=True)
class _SettingSpec:
    """Where a dotted setting key lives in the TOML tree and its declared type.

    ``section`` is the table path (e.g. ``("defaults", "nxp")`` or ``("build",)``)
    and ``key`` is the leaf key within that table. ``is_bool``, ``is_int``, and
    ``is_float`` are derived from the field-type sets so the dotted-key registry
    shares one source of truth with :func:`load_user_config`.
    """

    section: tuple[str, ...]
    key: str
    is_bool: bool
    is_int: bool
    is_float: bool = False


def _build_settings_schema() -> dict[str, _SettingSpec]:
    """Derive the dotted-key registry from the existing key mappings.

    Each ``(section, mapping)`` pair yields one dotted key per TOML key; the
    type is looked up from the mapped dataclass field's membership in
    ``_BOOL_FIELDS``. Keeping this derivation here means a new key added to a
    mapping automatically gains a dotted setting with no second edit.
    """
    schema: dict[str, _SettingSpec] = {}
    table_specs = (
        (("defaults", "nxp"), _NXP_KEYS),
        (("defaults", "ti"), _TI_KEYS),
        (("build",), _BUILD_KEYS),
        (("layers",), _LAYERS_KEYS),
        (("host",), _HOST_KEYS),
    )
    for section, mapping in table_specs:
        for key, field in mapping.items():
            dotted = ".".join((*section, key))
            schema[dotted] = _SettingSpec(
                section=section,
                key=key,
                is_bool=field in _BOOL_FIELDS,
                is_int=field in _INT_FIELDS,
                is_float=field in _PSI_FIELDS or field in {"disk_free_threshold_gb", "host_mem_min_gb"},
            )
    return schema


SETTINGS_SCHEMA: dict[str, _SettingSpec] = _build_settings_schema()

_TRUE_LITERALS = {"true", "1"}
_FALSE_LITERALS = {"false", "0"}


def _config_path(path: Path | None) -> Path:
    if path is None:
        return Path.home() / ".config" / "bakar" / "config.toml"
    return path


def _require_known(key: str) -> _SettingSpec:
    spec = SETTINGS_SCHEMA.get(key)
    if spec is None:
        raise ValueError(f"unrecognized setting key: {key!r}")
    return spec


def _coerce(spec: _SettingSpec, raw_value: str) -> str | bool | int | float:
    if spec.is_bool:
        lowered = raw_value.strip().lower()
        if lowered in _TRUE_LITERALS:
            return True
        if lowered in _FALSE_LITERALS:
            return False
        raise ValueError(f"value for boolean key must be one of true/false/1/0, got {raw_value!r}")
    if spec.is_int:
        try:
            v = int(raw_value)
        except ValueError:
            raise ValueError(f"value for integer key {spec.key!r} must be a valid integer, got {raw_value!r}") from None
        if spec.key == "stall_abort_secs" and v < 0:
            raise ValueError(f"value for {spec.key!r} must be >= 0 (0 disables), got {v}")
        if spec.key in _HOST_KEYS and v <= 0:
            raise ValueError(f"value for {spec.key!r} must be > 0, got {v}")
        return v
    if spec.is_float:
        try:
            v = float(raw_value)
        except ValueError:
            raise ValueError(f"value for {spec.key!r} must be a number, got {raw_value!r}") from None
        if spec.key in {"disk_free_threshold_gb", "mem_min_gb"}:
            if v <= 0:
                raise ValueError(f"value for {spec.key!r} must be > 0, got {v}")
        elif v < 1:
            raise ValueError(f"value for {spec.key!r} must be >= 1 (bitbake minimum), got {v}")
        return v
    return raw_value


def _load_raw(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _dump_raw(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write (tmp + replace): a crash mid-dump would otherwise leave a
    # truncated config.toml that breaks every command on the next load. Mirrors
    # the tmp+replace pattern used for config writes in kas.py.
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as f:
        tomli_w.dump(data, f)
    tmp.replace(path)


def get_setting(key: str, path: Path | None = None) -> str | bool | None:
    """Return the current value of a recognized dotted ``key``.

    Returns ``None`` when the key is recognized but absent from the config file
    (or the file does not exist). Raises ``ValueError`` for an unrecognized key.
    """
    spec = _require_known(key)
    data = _load_raw(_config_path(path))
    table: object = data
    for part in spec.section:
        if not isinstance(table, dict):
            return None
        table = table.get(part, {})
    if not isinstance(table, dict):
        return None
    return table.get(spec.key)


def set_setting(key: str, raw_value: str, path: Path | None = None) -> None:
    """Coerce and write a recognized dotted ``key`` to the config file.

    Rejects an unrecognized key with ``ValueError`` before touching the file.
    Boolean keys accept ``"true"``/``"false"``/``"1"``/``"0"`` (any other value
    raises ``ValueError``). Creates the file and parent directory if absent and
    preserves every other key already in the file.
    """
    spec = _require_known(key)
    value = _coerce(spec, raw_value)
    config_path = _config_path(path)
    data = _load_raw(config_path)
    table: dict[str, object] = data
    for part in spec.section:
        existing = table.get(part)
        if not isinstance(existing, dict):
            existing = {}
            table[part] = existing
        table = existing
    table[spec.key] = value
    _dump_raw(config_path, data)


def unset_setting(key: str, path: Path | None = None) -> None:
    """Remove a recognized dotted ``key`` from the config file.

    Prunes any table left empty by the removal. Rejects an unrecognized key with
    ``ValueError``. A no-op (no write) when the key or its containing tables are
    already absent.
    """
    spec = _require_known(key)
    config_path = _config_path(path)
    if not config_path.exists():
        return
    data = _load_raw(config_path)

    # Walk to the leaf table, recording the chain so emptied tables can be
    # pruned bottom-up after the removal.
    chain: list[tuple[dict[str, object], str]] = []
    table: object = data
    for part in spec.section:
        if not isinstance(table, dict) or not isinstance(table.get(part), dict):
            return
        chain.append((table, part))
        table = table[part]

    if not isinstance(table, dict) or spec.key not in table:
        return
    del table[spec.key]

    for parent, part in reversed(chain):
        child = parent.get(part)
        if isinstance(child, dict) and not child:
            del parent[part]

    _dump_raw(config_path, data)


def list_settings(path: Path | None = None) -> dict[str, str | bool | None]:
    """Return every recognized key mapped to its current value or ``None``.

    Keys absent from the file (or when no file exists) map to ``None``. Order
    follows :data:`SETTINGS_SCHEMA` insertion order.
    """
    return {key: get_setting(key, path) for key in SETTINGS_SCHEMA}
