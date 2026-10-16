from __future__ import annotations

import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING

import tomli_w

from bakar import user_config

if TYPE_CHECKING:
    from pathlib import Path

    from bakar.user_config import SchemaTable


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
    # in config.resolve() (rm_work default False; ccache's "nothing set
    # anywhere" default follows the resolved sccache_dist value instead of a
    # flat False).
    ccache: bool | None = None
    rm_work: bool | None = None
    # Live build UI drift coloring, workspace tier. None = not set -> user config
    # -> default False in config.resolve().
    show_baseline_drift: bool | None = None
    # [build] container opt-in, workspace tier. None = not set -> user config
    # (default False) -> host (structural default) in config.resolve().
    container: bool | None = None
    # Retained back-compat alias (no-op; host is the default). None = not set.
    host_mode: bool | None = None
    # [host] - workspace-tier override; None means "not set" (falls back to
    # the user config then the built-in floor in config.resolve()).
    host_inotify_instances: int | None = None
    host_inotify_watches: int | None = None
    host_swappiness_max: int | None = None
    host_nofile_soft: int | None = None
    host_mem_min_gb: float | None = None


# The workspace tier overrides a subset of the user config's keys, under the same
# TOML names. The maps are derived from user_config's so a key renamed there
# cannot drift here; the nxp_/ti_/generic_ prefixes keep the dataclass flat (one
# field per TOML key) so config.resolve()'s pick() calls map one-to-one.
_NXP_KEYS = {k: v for k, v in user_config._NXP_KEYS.items() if k != "repo_url"}
_TI_KEYS = user_config._TI_KEYS
# [defaults.generic] has no user-config counterpart.
_GENERIC_KEYS = {
    "kas_yaml": "generic_kas_yaml",
    "machine": "generic_machine",
}
# Top-level [build] table -> build fields. Not under [defaults]; the container
# image is not family-scoped.
_WORKSPACE_BUILD_FIELDS = frozenset(
    {"kas_container_image", "ccache", "rm_work", "show_baseline_drift", "container", "host_mode"}
)
_BUILD_KEYS = {k: v for k, v in user_config._BUILD_KEYS.items() if v in _WORKSPACE_BUILD_FIELDS}

_WORKSPACE_TABLES: tuple[SchemaTable, ...] = (
    (("defaults",), {}, frozenset({"nxp", "ti", "generic"})),
    (("defaults", "nxp"), _NXP_KEYS, frozenset()),
    (("defaults", "ti"), _TI_KEYS, frozenset()),
    (("defaults", "generic"), _GENERIC_KEYS, frozenset()),
    (("build",), _BUILD_KEYS, frozenset()),
    (("host",), user_config._HOST_KEYS, frozenset()),
)


def _check_generic(values: dict[str, object], path: Path) -> None:
    # user_config's type checks know no generic_* field, so the walker passes
    # them through unchecked.
    for field in _GENERIC_KEYS.values():
        if field in values and not isinstance(values[field], str):
            raise ValueError(f"{path}: '{field}' must be a string, got {type(values[field]).__name__}")


def load_workspace_config(workspace: Path) -> WorkspaceConfig:
    """Load ``<workspace>/.bakar.toml`` into a :class:`WorkspaceConfig`.

    Returns an all-defaults ``WorkspaceConfig()`` when the file is absent or
    carries no recognized sections (e.g. a comment-only marker file). Raises
    ``ValueError`` (with the file path in the message) on a TOML parse error or
    a type mismatch. Reads ``[defaults.<family>]`` build targets, a top-level
    ``[build]`` table (``kas_container_image``), and a top-level ``[host]``
    table of numeric thresholds through :func:`bakar.user_config.walk_schema`,
    so the rules match ``config.toml``: an unrecognized key under a recognized
    section, or an unrecognized top-level section, emits a :class:`UserWarning`
    naming it and the recognized names, then is ignored. A recognized section
    (``[defaults]``, ``[defaults.<family>]``, ``[build]``, ``[host]``) that is
    present but not a table raises ``ValueError``.
    """
    path = workspace / ".bakar.toml"

    if not path.exists():
        return WorkspaceConfig()

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: invalid TOML: {exc}") from exc

    values = user_config.walk_schema(data, path, _WORKSPACE_TABLES)
    _check_generic(values, path)
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
