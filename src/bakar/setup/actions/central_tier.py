"""Provision the central cross-node coordination tier for ``bakar setup``.

:class:`CentralTierAction` stands up the shared Rust/PostgreSQL hashserv +
prserv (see ``hashserv/docs/integration.md``) and records their endpoints in
the global ``[build]`` config so every node's build points ``BB_HASHSERVE`` /
``PRSERV_HOST`` at one hash-equivalence + PR service instead of the
per-workspace bitbake daemons.

Unlike the doctor-driven remediation actions, no host ``doctor`` check maps to
this one - it is an opt-in provisioning action invoked explicitly. It conforms
to the :class:`~bakar.setup.actions.base.Action` protocol so the setup runner
applies it uniformly. ``is_satisfied`` probes live state (postgres reachable,
both services listening, both endpoints already in config), so a host whose
tier is up yields no work. The one-shot per-dataset database bootstrap is the
action's ``operations()``; the service start and config persist happen in
:meth:`apply` (mirroring :class:`~bakar.setup.actions.config_write.ConfigWriteAction`),
which the runner calls last.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bakar import hashserv, prserv
from bakar.setup.actions.base import RunCommand
from bakar.user_config import get_setting, set_setting

if TYPE_CHECKING:
    from pathlib import Path

    from bakar.setup.actions.base import WriteFile
    from bakar.setup.profile import HostProfile

_DEFAULT_PG_HOST = "localhost"
_DEFAULT_PG_PORT = 5432


@dataclass(frozen=True)
class CentralTierConfig:
    """Endpoints and DB URLs for the central tier (owned by the plan builder).

    ``bind_host`` is the address both services bind so other cluster nodes can
    reach them (the node's cluster-reachable IP, e.g. ``10.42.0.1``).
    ``config_path`` defaults to ``None`` - the global ``~/.config/bakar/config.toml`` -
    and is overridden in tests to assert the persisted target.
    """

    bind_host: str
    pg_host: str = _DEFAULT_PG_HOST
    pg_port: int = _DEFAULT_PG_PORT
    hashserv_port: int = hashserv.CENTRAL_DEFAULT_PORT
    prserv_port: int = prserv.CENTRAL_DEFAULT_PORT
    hashserv_database: str = "postgres://hashserv:hashserv@localhost:5432/hashserv"
    prserv_database: str = "postgres://prserv:prserv@localhost:5432/prserv"
    hashserv_binary: str = "avocado-hashserv"
    prserv_binary: str = "avocado-prserv"
    config_path: Path | None = None


def _postgres_reachable(host: str, port: int, *, timeout: float = 0.5) -> bool:
    """Return True iff a TCP connection to the postgres instance succeeds.

    A failed connect is the "PostgreSQL absent" signal: the Rust services are
    postgres-backed (prserv has no sqlite path), so an unreachable instance
    means the tier cannot be satisfied no matter what else is up.
    """
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return False
    sock.close()
    return True


def _ensure_db_op(pg_host: str, pg_port: int, dbname: str) -> RunCommand:
    """An idempotent per-dataset database bootstrap command.

    ``createdb`` errors when the database exists, so each op first probes the
    catalog as the dataset's own role and only creates the database when the
    connect fails - re-running setup is then a no-op rather than a failure.
    """
    guard = (
        f"PGPASSWORD={dbname} psql -h {pg_host} -p {pg_port} -U {dbname} "
        f"-d {dbname} -tAc 'SELECT 1' >/dev/null 2>&1 "
        f"|| createdb -h {pg_host} -p {pg_port} -O {dbname} {dbname}"
    )
    return RunCommand(argv=["sh", "-c", guard], needs_root=False)


class CentralTierAction:
    """Provision the central Rust/PostgreSQL hashserv + prserv and record them."""

    check_name = "central-tier"
    needs_root = False

    def __init__(self, cfg: CentralTierConfig) -> None:
        self._cfg = cfg

    @property
    def _bb_hashserve(self) -> str:
        return hashserv.central_bb_hashserve(self._cfg.bind_host, self._cfg.hashserv_port)

    @property
    def _prserv_host(self) -> str:
        return prserv.central_prserv_host(self._cfg.bind_host, self._cfg.prserv_port)

    def describe(self) -> str:
        return (
            f"start central hashserv ({self._bb_hashserve}) and prserv "
            f"({self._prserv_host}) and record them in the global [build] config"
        )

    def is_satisfied(self, _profile: HostProfile) -> bool:
        """True only when postgres is reachable, both services listen, and the
        endpoints are already persisted.

        Returns False the moment postgres is unreachable or either service is
        not listening - so an absent PostgreSQL or a stopped hashserv is never
        reported as satisfied.
        """
        cfg = self._cfg
        return (
            _postgres_reachable(cfg.pg_host, cfg.pg_port)
            and hashserv.central_listening(cfg.bind_host, cfg.hashserv_port)
            and prserv.central_listening(cfg.bind_host, cfg.prserv_port)
            and get_setting("build.bb_hashserve", cfg.config_path) == self._bb_hashserve
            and get_setting("build.prserv_host", cfg.config_path) == self._prserv_host
        )

    def operations(self) -> list[RunCommand | WriteFile]:
        """The one-shot, idempotent per-dataset database bootstrap (unprivileged)."""
        cfg = self._cfg
        return [
            _ensure_db_op(cfg.pg_host, cfg.pg_port, "hashserv"),
            _ensure_db_op(cfg.pg_host, cfg.pg_port, "prserv"),
        ]

    def apply(self, path: Path | None = None) -> None:
        """Start both services, then persist their endpoints to ``[build]`` config.

        Mirrors :class:`ConfigWriteAction`: the runner calls this last. ``path``
        defaults to ``None`` (the global config), falling back to the config's
        ``config_path`` when the runner passes nothing.
        """
        cfg = self._cfg
        target = path if path is not None else cfg.config_path
        hashserv.central_ensure_running(
            binary=cfg.hashserv_binary,
            bind_host=cfg.bind_host,
            port=cfg.hashserv_port,
            database=cfg.hashserv_database,
        )
        prserv.central_ensure_running(
            binary=cfg.prserv_binary,
            bind_host=cfg.bind_host,
            port=cfg.prserv_port,
            database=cfg.prserv_database,
        )
        set_setting("build.bb_hashserve", self._bb_hashserve, target)
        set_setting("build.prserv_host", self._prserv_host, target)
