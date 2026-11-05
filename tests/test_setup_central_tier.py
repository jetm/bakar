"""Tests for the central cross-node tier provisioning of ``bakar setup``.

Covers :class:`CentralTierAction` and the ``central_*`` lifecycle helpers on
:mod:`bakar.hashserv` / :mod:`bakar.prserv`. The action stands up the shared
Rust/PostgreSQL hashserv + prserv and records ``BB_HASHSERVE`` / ``PRSERV_HOST``
in the global ``[build]`` config. Every probe is monkeypatched so the suite
needs no live postgres or services.
"""

from __future__ import annotations

import tomllib

import pytest

from bakar import central_service, hashserv, prserv
from bakar.setup.actions import central_tier
from bakar.setup.actions.base import Action, RunCommand
from bakar.setup.actions.central_tier import CentralTierAction, CentralTierConfig
from tests.conftest import run_commands

_BIND = "10.42.0.1"


def _cfg(tmp_path=None) -> CentralTierConfig:
    return CentralTierConfig(bind_host=_BIND, config_path=(tmp_path / "config.toml") if tmp_path else None)


def _all_probes_up(monkeypatch) -> None:
    """Make postgres reachable and both services listening."""
    monkeypatch.setattr(central_tier, "_postgres_reachable", lambda *a, **k: True)
    monkeypatch.setattr(hashserv, "central_listening", lambda *a, **k: True)
    monkeypatch.setattr(prserv, "central_listening", lambda *a, **k: True)


# --- helper format / probe contracts ----------------------------------------


def test_central_endpoints_are_host_colon_port() -> None:
    assert hashserv.central_bb_hashserve("10.42.0.1") == "10.42.0.1:8686"
    assert prserv.central_prserv_host("10.42.0.1") == "10.42.0.1:8585"


def test_central_listening_false_on_closed_port() -> None:
    """A port with nothing bound is reported not-listening (negative assertion)."""
    assert hashserv.central_listening("127.0.0.1", 1) is False
    assert prserv.central_listening("127.0.0.1", 1) is False


def test_central_ensure_running_returns_none_when_binary_missing(tmp_path) -> None:
    """No executable -> None, never a fabricated endpoint (negative assertion)."""
    missing = str(tmp_path / "definitely-not-on-path-xyz")
    assert (
        hashserv.central_ensure_running(binary=missing, bind_host="127.0.0.1", database="postgres://x", port=1) is None
    )
    assert prserv.central_ensure_running(binary=missing, bind_host="127.0.0.1", database="postgres://x", port=1) is None


class _NeverListensProc:
    """A spawn that stays alive but never opens its port."""

    def __init__(self) -> None:
        self.terminated = False

    def poll(self):
        return None  # still running

    def terminate(self) -> None:
        self.terminated = True


@pytest.mark.parametrize("module", [hashserv, prserv])
def test_central_ensure_running_terminates_spawn_on_startup_timeout(module, monkeypatch) -> None:
    """A spawn that never starts listening is terminated, not left orphaned.

    Both modules' central_ensure_running delegate to bakar.central_service, where
    the spawn/probe/terminate loop lives, so the spawn is patched there.
    """
    fake = _NeverListensProc()
    monkeypatch.setattr(central_service.shutil, "which", lambda _b: "/usr/bin/svc")
    monkeypatch.setattr(central_service.subprocess, "Popen", lambda *a, **k: fake)

    result = module.central_ensure_running(
        binary="svc",
        bind_host="127.0.0.1",
        database="postgres://x",
        port=1,  # nothing listening -> probe never succeeds
        startup_deadline_seconds=0.05,
    )

    assert result is None
    assert fake.terminated is True


# --- Action protocol conformance --------------------------------------------


def test_central_tier_is_an_action() -> None:
    action = CentralTierAction(_cfg())
    assert isinstance(action, Action)
    assert action.check_name == "central-tier"
    assert action.needs_root is False


# --- is_satisfied gating -----------------------------------------------------


def test_is_satisfied_false_when_postgres_unreachable(monkeypatch, tmp_path) -> None:
    """Absent PostgreSQL is detected even when everything else is up."""
    monkeypatch.setattr(central_tier, "_postgres_reachable", lambda *a, **k: False)
    monkeypatch.setattr(hashserv, "central_listening", lambda *a, **k: True)
    monkeypatch.setattr(prserv, "central_listening", lambda *a, **k: True)
    action = CentralTierAction(_cfg(tmp_path))
    action.apply(tmp_path / "config.toml")  # endpoints persisted, so only pg gates
    assert action.is_satisfied(None) is False


def test_is_satisfied_false_when_hashserv_not_listening(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(central_tier, "_postgres_reachable", lambda *a, **k: True)
    monkeypatch.setattr(hashserv, "central_listening", lambda *a, **k: False)
    monkeypatch.setattr(prserv, "central_listening", lambda *a, **k: True)
    action = CentralTierAction(_cfg(tmp_path))
    action.apply(tmp_path / "config.toml")
    assert action.is_satisfied(None) is False


def test_is_satisfied_false_when_prserv_not_listening(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(central_tier, "_postgres_reachable", lambda *a, **k: True)
    monkeypatch.setattr(hashserv, "central_listening", lambda *a, **k: True)
    monkeypatch.setattr(prserv, "central_listening", lambda *a, **k: False)
    action = CentralTierAction(_cfg(tmp_path))
    action.apply(tmp_path / "config.toml")
    assert action.is_satisfied(None) is False


def test_is_satisfied_false_when_config_missing_endpoint(monkeypatch, tmp_path) -> None:
    """Services up but no persisted endpoint -> not satisfied (config not written)."""
    _all_probes_up(monkeypatch)
    action = CentralTierAction(_cfg(tmp_path))  # config never written
    assert action.is_satisfied(None) is False


def test_is_satisfied_true_when_all_up_and_persisted(monkeypatch, tmp_path) -> None:
    _all_probes_up(monkeypatch)
    action = CentralTierAction(_cfg(tmp_path))
    action.apply(tmp_path / "config.toml")
    assert action.is_satisfied(None) is True


# --- apply(): start services + persist endpoints -----------------------------


def test_apply_writes_endpoints_to_build_config(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    CentralTierAction(_cfg(tmp_path)).apply(config_path)

    with config_path.open("rb") as f:
        data = tomllib.load(f)
    build = data["build"]
    assert build["bb_hashserve"] == "10.42.0.1:8686"
    assert build["prserv_host"] == "10.42.0.1:8585"
    assert build["cluster"] is True


def test_apply_writes_all_three_settings_in_one_call(monkeypatch, tmp_path) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(central_tier, "apply_settings", lambda sets, unsets=(), path=None: calls.append(dict(sets)))
    CentralTierAction(_cfg(tmp_path)).apply()
    assert calls == [
        {"build.bb_hashserve": "10.42.0.1:8686", "build.prserv_host": "10.42.0.1:8585", "build.cluster": "true"}
    ]


def test_apply_persist_false_writes_nothing(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    CentralTierAction(_cfg(tmp_path), persist=False).apply(config_path)
    assert not config_path.exists()


def test_is_satisfied_false_when_cluster_unset(monkeypatch, tmp_path) -> None:
    _all_probes_up(monkeypatch)
    config_path = tmp_path / "config.toml"
    config_path.write_text('[build]\nbb_hashserve = "10.42.0.1:8686"\nprserv_host = "10.42.0.1:8585"\n')
    assert CentralTierAction(_cfg(tmp_path)).is_satisfied(None) is False


def test_from_inputs_threads_pg_port_into_database_urls() -> None:
    cfg = CentralTierConfig.from_inputs("10.0.0.5", pg_host="db", pg_port=5433)
    assert cfg.bind_host == "10.0.0.5"
    assert cfg.hashserv_database == "postgres://hashserv:hashserv@db:5433/hashserv"
    assert cfg.prserv_database == "postgres://prserv:prserv@db:5433/prserv"
    assert cfg.hashserv_port == hashserv.CENTRAL_DEFAULT_PORT


def test_preflight_reports_missing_units_and_postgres(monkeypatch) -> None:
    class _Result:
        returncode = 1

    monkeypatch.setattr(central_tier.subprocess, "run", lambda *a, **k: _Result())
    monkeypatch.setattr(central_tier, "_postgres_reachable", lambda *a, **k: False)
    reasons = CentralTierAction(_cfg()).preflight()
    assert any("avocado-hashserv.service" in r for r in reasons)
    assert any("avocado-prserv.service" in r for r in reasons)
    assert "PostgreSQL at localhost:5432 is not reachable" in reasons


def test_preflight_empty_when_ready(monkeypatch) -> None:
    class _Result:
        returncode = 0

    monkeypatch.setattr(central_tier.subprocess, "run", lambda *a, **k: _Result())
    monkeypatch.setattr(central_tier, "_postgres_reachable", lambda *a, **k: True)
    assert CentralTierAction(_cfg()).preflight() == []


def test_preflight_missing_systemctl_is_a_reason(monkeypatch) -> None:
    def _boom(*a, **k):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(central_tier.subprocess, "run", _boom)
    monkeypatch.setattr(central_tier, "_postgres_reachable", lambda *a, **k: True)
    assert len(CentralTierAction(_cfg()).preflight()) == 2


def test_operations_enables_both_systemd_units(tmp_path) -> None:
    """operations() enables the packaged units instead of spawning the daemons."""
    ops = CentralTierAction(_cfg(tmp_path)).operations()
    enable = [op for op in ops if isinstance(op, RunCommand) and "enable" in op.argv]
    assert len(enable) == 1
    op = enable[0]
    assert op.needs_root is True
    assert "avocado-hashserv.service" in op.argv
    assert "avocado-prserv.service" in op.argv
    assert "--now" in op.argv


# --- operations(): idempotent database bootstrap -----------------------------


def test_operations_bootstrap_both_databases_unprivileged() -> None:
    ops = run_commands(CentralTierAction(_cfg()).operations())
    assert len(ops) == 3
    createdb_ops = [op for op in ops if "createdb" in " ".join(op.argv)]
    assert len(createdb_ops) == 2
    assert all(op.needs_root is False for op in createdb_ops)
    joined = " ".join(arg for op in createdb_ops for arg in op.argv)
    assert "hashserv" in joined
    assert "prserv" in joined


def test_database_bootstrap_runs_before_the_privileged_unit_start() -> None:
    ops = run_commands(CentralTierAction(_cfg()).operations())
    createdb_ops = [op for op in ops if "createdb" in " ".join(op.argv)]
    unit_ops = [op for op in ops if op.needs_root]
    assert len(createdb_ops) == 2
    assert all(op.before_privileged for op in createdb_ops)
    assert unit_ops
    assert not any(op.before_privileged for op in unit_ops)
