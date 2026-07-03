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
    monkeypatch.setattr(hashserv, "central_ensure_running", lambda **k: "ignored")
    monkeypatch.setattr(prserv, "central_ensure_running", lambda **k: "ignored")
    action = CentralTierAction(_cfg(tmp_path))
    action.apply(tmp_path / "config.toml")
    assert action.is_satisfied(None) is True


# --- apply(): start services + persist endpoints -----------------------------


def test_apply_writes_endpoints_to_build_config(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(hashserv, "central_ensure_running", lambda **k: "ignored")
    monkeypatch.setattr(prserv, "central_ensure_running", lambda **k: "ignored")
    config_path = tmp_path / "config.toml"
    CentralTierAction(_cfg(tmp_path)).apply(config_path)

    with config_path.open("rb") as f:
        data = tomllib.load(f)
    build = data["build"]
    assert build["bb_hashserve"] == "10.42.0.1:8686"
    assert build["prserv_host"] == "10.42.0.1:8585"


def test_apply_starts_both_services_with_db_urls(monkeypatch, tmp_path) -> None:
    """apply() launches each Rust service with its bind host and postgres URL."""
    calls: dict[str, dict] = {}
    monkeypatch.setattr(hashserv, "central_ensure_running", lambda **k: calls.setdefault("hashserv", k) and None)
    monkeypatch.setattr(prserv, "central_ensure_running", lambda **k: calls.setdefault("prserv", k) and None)
    CentralTierAction(_cfg(tmp_path)).apply(tmp_path / "config.toml")

    assert calls["hashserv"]["bind_host"] == _BIND
    assert calls["hashserv"]["database"].startswith("postgres://hashserv")
    assert calls["prserv"]["bind_host"] == _BIND
    assert calls["prserv"]["database"].startswith("postgres://prserv")


# --- operations(): idempotent database bootstrap -----------------------------


def test_operations_bootstrap_both_databases_unprivileged() -> None:
    ops = CentralTierAction(_cfg()).operations()
    assert len(ops) == 2
    assert all(isinstance(op, RunCommand) for op in ops)
    assert all(op.needs_root is False for op in ops)
    joined = " ".join(arg for op in ops for arg in op.argv)
    assert "createdb" in joined
    assert "hashserv" in joined
    assert "prserv" in joined
