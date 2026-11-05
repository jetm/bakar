"""Tests for the `bakar init` requirement probes (commands/_init_probes.py).

Every call that would start ssh, systemctl, bakar or a TCP connect is patched;
no test reaches a real host.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar import feed_preflight
from bakar.commands import _init_probes
from bakar.commands._init_probes import PROBES
from bakar.diagnostics import CheckResult, Severity, Status
from bakar.setup import features
from bakar.setup.actions import central_tier
from bakar.steps import remote_dispatch

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

CFG = object()


def _forbid(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("must not be called")


def _result(name: str, status: Status, message: str = "msg", hint: str | None = None) -> CheckResult:
    return CheckResult(name=name, severity=Severity.BLOCK, status=status, message=message, fix_hint=hint)


def test_every_catalog_probe_target_is_registered() -> None:
    targets = {r.target for f in features.CATALOG for r in f.requirements if r.kind == "probe"}
    assert targets
    assert targets <= set(PROBES)
    assert set(PROBES) == {
        "remote-host",
        "remote-release-workspace",
        "local-release-workspace",
        "feed-host-tools",
        "central-units",
        "central-postgres",
        "psi-kernel",
    }


# --- remote-host -------------------------------------------------------------


@pytest.mark.parametrize("host", ["-oProxyCommand=x", "", "a host", "pc3\t"])
def test_remote_host_refuses_bad_alias_before_ssh(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setattr(remote_dispatch, "preflight_remote", _forbid)
    monkeypatch.setattr(remote_dispatch, "_local_bakar_version", _forbid)
    met, reason = PROBES["remote-host"](CFG, {}, {"host": host})
    assert met is False
    assert reason


def test_remote_host_dash_alias_named_as_ssh_option(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_dispatch, "preflight_remote", _forbid)
    met, reason = PROBES["remote-host"](CFG, {}, {"host": "-oProxyCommand=x"})
    assert met is False
    assert "-oProxyCommand=x" in reason
    assert "ssh option" in reason


def test_remote_host_version_mismatch_names_both(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_dispatch, "preflight_remote", lambda _h: (True, "bakar 0.40.0 (aaaa)"))
    monkeypatch.setattr(remote_dispatch, "_local_bakar_version", lambda: "bakar 0.40.0 (bbbb)")
    met, reason = PROBES["remote-host"](CFG, {}, {"host": "pc3"})
    assert met is False
    assert "bakar 0.40.0 (aaaa)" in reason
    assert "bakar 0.40.0 (bbbb)" in reason


def test_remote_host_exact_match_is_met(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_dispatch, "preflight_remote", lambda _h: (True, "bakar 0.40.0 (aaaa)"))
    monkeypatch.setattr(remote_dispatch, "_local_bakar_version", lambda: "bakar 0.40.0 (aaaa)")
    met, _reason = PROBES["remote-host"](CFG, {}, {"host": "pc3"})
    assert met is True


def test_remote_host_preflight_error_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_dispatch, "preflight_remote", lambda _h: (False, "Permission denied (publickey)"))
    monkeypatch.setattr(remote_dispatch, "_local_bakar_version", _forbid)
    assert PROBES["remote-host"](CFG, {}, {"host": "pc3"}) == (False, "Permission denied (publickey)")


def test_remote_host_preflight_without_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_dispatch, "preflight_remote", lambda _h: (False, None))
    assert PROBES["remote-host"](CFG, {}, {"host": "pc3"}) == (False, "ssh to pc3 failed (no detail)")


def test_remote_host_unknown_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_dispatch, "preflight_remote", lambda _h: (True, "bakar 1"))
    monkeypatch.setattr(remote_dispatch, "_local_bakar_version", lambda: None)
    assert PROBES["remote-host"](CFG, {}, {"host": "pc3"}) == (False, "local bakar version unknown")

    monkeypatch.setattr(remote_dispatch, "preflight_remote", lambda _h: (True, None))
    monkeypatch.setattr(remote_dispatch, "_local_bakar_version", lambda: "bakar 1")
    assert PROBES["remote-host"](CFG, {}, {"host": "pc3"}) == (False, "remote bakar version unknown")


# --- remote-release-workspace -------------------------------------------------


def test_remote_release_workspace_without_release_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_dispatch, "check_remote_workspace", _forbid)
    assert PROBES["remote-release-workspace"](CFG, {}, {"host": "pc3"}) == (True, "not checked")


def test_remote_release_workspace_dispatch_error_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, str]] = []
    text = "the release workspace /w/scarthgap does not exist on pc3. Create it once on pc3:\n  mkdir -p /w/scarthgap"

    def fake(host: str, workspace: str, release: str) -> None:
        calls.append((host, workspace, release))
        raise remote_dispatch.DispatchError(text)

    monkeypatch.setattr(remote_dispatch, "check_remote_workspace", fake)
    met, reason = PROBES["remote-release-workspace"](
        CFG, {"workspaces.template": "/w/{release}"}, {"host": "pc3", "release": "scarthgap"}
    )
    assert (met, reason) == (False, text)
    assert calls == [("pc3", "/w/scarthgap", "scarthgap")]


def test_remote_release_workspace_met(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_dispatch, "check_remote_workspace", lambda *_a: None)
    met, _ = PROBES["remote-release-workspace"](
        CFG, {"workspaces.template": "/w/{release}"}, {"host": "pc3", "release": "scarthgap"}
    )
    assert met is True


def test_remote_release_workspace_falls_back_to_config(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        _init_probes.user_config,
        "load_user_config",
        lambda: _init_probes.user_config.UserConfig(workspaces_template="/cfg/{release}"),
    )
    monkeypatch.setattr(remote_dispatch, "check_remote_workspace", lambda _h, ws, _r: calls.append(ws))
    PROBES["remote-release-workspace"](CFG, {}, {"host": "pc3", "release": "walnascar"})
    assert calls == ["/cfg/walnascar"]


def test_remote_release_workspace_refuses_bad_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_dispatch, "check_remote_workspace", _forbid)
    met, _ = PROBES["remote-release-workspace"](
        CFG, {"workspaces.template": "/w/{release}"}, {"host": "-oProxyCommand=x", "release": "scarthgap"}
    )
    assert met is False


# --- local-release-workspace --------------------------------------------------


def test_local_release_workspace(tmp_path: Path) -> None:
    probe = PROBES["local-release-workspace"]
    values = {"workspaces.template": str(tmp_path / "{release}")}
    assert probe(CFG, values, {}) == (True, "not checked")

    met, reason = probe(CFG, values, {"local_release": "scarthgap"})
    assert met is False
    assert "local routed builds" in reason
    assert "--on" in reason

    (tmp_path / "scarthgap").mkdir()
    met, _ = probe(CFG, values, {"local_release": "scarthgap"})
    assert met is True


def test_local_release_workspace_bad_template() -> None:
    met, reason = PROBES["local-release-workspace"](
        CFG, {"workspaces.template": "/w/{nope}"}, {"local_release": "scarthgap"}
    )
    assert met is False
    assert "workspaces.template" in reason


# --- feed-host-tools ----------------------------------------------------------


def test_feed_host_tools_names_missing_createrepo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feed_preflight, "check_interpreter", lambda: _result("python", Status.PASS))
    monkeypatch.setattr(
        feed_preflight, "check_createrepo", lambda: _result("createrepo_c", Status.FAIL, "not on PATH", "dnf it")
    )
    monkeypatch.setattr(
        feed_preflight,
        "check_shell_tools",
        lambda: [_result("bash", Status.PASS), _result("tar", Status.FAIL, "tar missing")],
    )
    met, reason = PROBES["feed-host-tools"](CFG, {}, {})
    assert met is False
    assert "createrepo_c: not on PATH" in reason
    assert "dnf it" in reason
    assert "tar: tar missing" in reason
    assert "bash" not in reason


def test_feed_host_tools_all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feed_preflight, "check_interpreter", lambda: _result("python", Status.PASS))
    monkeypatch.setattr(feed_preflight, "check_createrepo", lambda: _result("createrepo_c", Status.PASS))
    monkeypatch.setattr(
        feed_preflight, "check_shell_tools", lambda: [_result("bash", Status.PASS), _result("tar", Status.PASS)]
    )
    met, _ = PROBES["feed-host-tools"](CFG, {}, {})
    assert met is True


# --- central-units / central-postgres -------------------------------------------


def _patch_preflight(monkeypatch: pytest.MonkeyPatch, reasons: list[str]) -> list[central_tier.CentralTierConfig]:
    seen: list[central_tier.CentralTierConfig] = []

    def fake(self: central_tier.CentralTierAction) -> list[str]:
        seen.append(self._cfg)
        return list(reasons)

    monkeypatch.setattr(central_tier.CentralTierAction, "preflight", fake)
    return seen


def test_central_units_reports_only_unit_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch_preflight(
        monkeypatch,
        ["systemd unit avocado-hashserv.service is not installed", "PostgreSQL at localhost:5432 is not reachable"],
    )
    met, reason = PROBES["central-units"](CFG, {}, {"bind_host": "10.0.0.1"})
    assert met is False
    assert "avocado-hashserv.service" in reason
    assert "PostgreSQL" not in reason
    assert seen[0].bind_host == "10.0.0.1"


def test_central_units_met_when_only_postgres_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_preflight(monkeypatch, ["PostgreSQL at localhost:5432 is not reachable"])
    met, _ = PROBES["central-units"](CFG, {}, {})
    assert met is True


def test_central_postgres_uses_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch_preflight(
        monkeypatch,
        ["systemd unit avocado-prserv.service is not installed", "PostgreSQL at db:5433 is not reachable"],
    )
    met, reason = PROBES["central-postgres"](CFG, {}, {"bind_host": "10.0.0.1", "pg_host": "db", "pg_port": "5433"})
    assert (met, reason) == (False, "PostgreSQL at db:5433 is not reachable")
    assert (seen[0].pg_host, seen[0].pg_port) == ("db", 5433)


def test_central_postgres_non_integer_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(central_tier.CentralTierAction, "preflight", _forbid)
    met, reason = PROBES["central-postgres"](CFG, {}, {"pg_port": "fifty"})
    assert met is False
    assert "fifty" in reason


def test_central_postgres_met(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_preflight(monkeypatch, ["systemd unit avocado-prserv.service is not installed"])
    met, _ = PROBES["central-postgres"](CFG, {}, {})
    assert met is True


@pytest.mark.unit
def test_psi_kernel_probe_follows_the_proc_pressure_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from bakar import diagnostics

    monkeypatch.setattr(diagnostics, "_read_psi_avg10", lambda _resource: 0.5)
    met, reason = PROBES["psi-kernel"](None, {}, {})  # type: ignore[arg-type]
    assert met is True
    monkeypatch.setattr(diagnostics, "_read_psi_avg10", lambda _resource: None)
    met, reason = PROBES["psi-kernel"](None, {}, {})  # type: ignore[arg-type]
    assert met is False
    assert "/proc/pressure/cpu" in reason


@pytest.mark.unit
def test_psi_requirement_is_the_probe_not_the_skipping_check() -> None:
    psi = features.feature_by_id("psi")
    assert [(r.kind, r.target) for r in psi.requirements] == [("probe", "psi-kernel")]
