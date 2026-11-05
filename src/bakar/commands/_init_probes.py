"""Requirement probes `bakar init` runs for the feature catalog's ``probe`` kind.

The catalog (``bakar.setup.features``) names a probe by id only, because the
host-provisioning group may not import the ssh preflight (steps) or the feed
preflight (feed). The command layer may import both, so the probes live here and
the requirement evaluator looks them up in :data:`PROBES`.

Each probe takes the resolved build config, the proposed setting values (dotted
keys) and the feature inputs (bare names, never written), and returns
``(met, reason)``. A probe never raises for a condition it can describe: an
unmet requirement is a result, not an exception.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from bakar import diagnostics, feed_preflight, user_config
from bakar.diagnostics import Status
from bakar.setup.actions.central_tier import CentralTierAction, CentralTierConfig
from bakar.steps import remote_dispatch

if TYPE_CHECKING:
    from bakar.config import BuildConfig

type ProbeFunc = Callable[[BuildConfig, Mapping[str, str], Mapping[str, str]], tuple[bool, str]]

_NOT_CHECKED = "not checked"
_TEMPLATE_KEY = "workspaces.template"
_POSTGRES_PREFIX = "PostgreSQL"


def _invalid_host(host: str) -> str | None:
    """Why ``host`` cannot be handed to ssh, or None when it can.

    Checked before any process starts: an alias beginning with ``-`` would parse
    as an ssh option (``-oProxyCommand=...`` runs a local command), and
    whitespace would split it into several arguments.
    """
    if not host:
        return "no remote host given"
    if host.startswith("-"):
        return f"invalid host {host!r}: must not begin with '-' (it would parse as an ssh option)"
    if any(ch.isspace() for ch in host):
        return f"invalid host {host!r}: must not contain whitespace"
    return None


def _remote_host(_cfg: BuildConfig, _values: Mapping[str, str], inputs: Mapping[str, str]) -> tuple[bool, str]:
    host = inputs.get("host") or ""
    invalid = _invalid_host(host)
    if invalid is not None:
        return False, invalid
    ok, detail = remote_dispatch.preflight_remote(host)
    if not ok:
        return False, detail or f"ssh to {host} failed (no detail)"
    local = remote_dispatch._local_bakar_version()
    if local is None:
        return False, "local bakar version unknown"
    if detail is None:
        return False, "remote bakar version unknown"
    if detail != local:
        return False, f"bakar version mismatch: {host} has {detail!r}, this host has {local!r}"
    return True, f"{host} reachable over ssh with {detail}"


def _template(values: Mapping[str, str]) -> str | None:
    """The release workspace template: the proposed value, else the global config's.

    ``BuildConfig`` does not carry ``[workspaces]``, so the fallback reads the
    user config directly.
    """
    proposed = values.get(_TEMPLATE_KEY)
    if proposed:
        return proposed
    try:
        return user_config.load_user_config().workspaces_template
    except ValueError, OSError:
        return None


def _format_template(template: str, release: str) -> tuple[str | None, str]:
    try:
        return template.format(release=release), ""
    except (KeyError, IndexError, ValueError) as exc:
        return None, f"{_TEMPLATE_KEY} {template!r} cannot be formatted with release {release!r}: {exc}"


def _remote_release_workspace(
    _cfg: BuildConfig, values: Mapping[str, str], inputs: Mapping[str, str]
) -> tuple[bool, str]:
    release = inputs.get("release") or ""
    if not release:
        return True, _NOT_CHECKED
    host = inputs.get("host") or ""
    invalid = _invalid_host(host)
    if invalid is not None:
        return False, invalid
    template = _template(values)
    if not template:
        return False, f"no {_TEMPLATE_KEY} configured, so the remote release workspace cannot be located"
    workspace, error = _format_template(template, release)
    if workspace is None:
        return False, error
    try:
        remote_dispatch.check_remote_workspace(host, workspace, release)
    except remote_dispatch.DispatchError as exc:
        return False, str(exc)
    return True, f"{workspace} on {host} is a {release} workspace"


def _local_release_workspace(
    _cfg: BuildConfig, values: Mapping[str, str], inputs: Mapping[str, str]
) -> tuple[bool, str]:
    release = inputs.get("local_release") or ""
    if not release:
        return True, _NOT_CHECKED
    template = _template(values)
    if not template:
        return False, f"no {_TEMPLATE_KEY} configured, so the local release workspace cannot be located"
    workspace, error = _format_template(template, release)
    if workspace is None:
        return False, error
    if not Path(workspace).expanduser().is_dir():
        return False, (
            f"{workspace} is not a directory; local routed builds of {release} need it, "
            "a setup that only dispatches with --on can ignore this"
        )
    return True, f"{workspace} exists"


def _feed_host_tools(_cfg: BuildConfig, _values: Mapping[str, str], _inputs: Mapping[str, str]) -> tuple[bool, str]:
    results = [
        feed_preflight.check_interpreter(),
        feed_preflight.check_createrepo(),
        *feed_preflight.check_shell_tools(),
    ]
    failed = [r for r in results if r.status is not Status.PASS]
    if failed:
        return False, "; ".join(f"{r.name}: {r.message}" + (f" ({r.fix_hint})" if r.fix_hint else "") for r in failed)
    return True, "found " + ", ".join(r.name for r in results)


def _central_units(_cfg: BuildConfig, _values: Mapping[str, str], inputs: Mapping[str, str]) -> tuple[bool, str]:
    action = CentralTierAction(CentralTierConfig.from_inputs(inputs.get("bind_host") or "localhost"))
    reasons = [r for r in action.preflight() if not r.startswith(_POSTGRES_PREFIX)]
    if reasons:
        return False, "; ".join(reasons)
    return True, "avocado-hashserv.service and avocado-prserv.service are installed"


def _central_postgres(_cfg: BuildConfig, _values: Mapping[str, str], inputs: Mapping[str, str]) -> tuple[bool, str]:
    pg_host = inputs.get("pg_host") or "localhost"
    raw_port = inputs.get("pg_port") or "5432"
    try:
        pg_port = int(raw_port)
    except ValueError:
        return False, f"pg_port {raw_port!r} is not an integer"
    cfg = CentralTierConfig.from_inputs(inputs.get("bind_host") or "localhost", pg_host, pg_port)
    reasons = [r for r in CentralTierAction(cfg).preflight() if r.startswith(_POSTGRES_PREFIX)]
    if reasons:
        return False, "; ".join(reasons)
    return True, f"PostgreSQL at {pg_host}:{pg_port} is reachable"


def _psi_kernel(_cfg: BuildConfig, _values: Mapping[str, str], _inputs: Mapping[str, str]) -> tuple[bool, str]:
    """Whether the kernel exposes PSI, read the way the ``psi_support`` check reads it."""
    if diagnostics._read_psi_avg10("cpu") is None:
        return False, "the kernel does not expose /proc/pressure/cpu"
    return True, "the kernel exposes PSI"


PROBES: Mapping[str, ProbeFunc] = MappingProxyType(
    {
        "psi-kernel": _psi_kernel,
        "remote-host": _remote_host,
        "remote-release-workspace": _remote_release_workspace,
        "local-release-workspace": _local_release_workspace,
        "feed-host-tools": _feed_host_tools,
        "central-units": _central_units,
        "central-postgres": _central_postgres,
    }
)
