"""Config-value doctor checks: scheduler and central-endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar import diagnostics, hashserv, prserv
from bakar.config import BuildConfig
from bakar.diagnostics import (
    _CHECK_METADATA,
    _CLUSTER_CHECKS,
    CHECK_GROUPS,
    SHARED_CHECKS,
    Severity,
    Status,
    check_central_endpoints,
    check_scheduler_value,
    run_all,
)


def _cfg(**over: object) -> BuildConfig:
    base: dict[str, object] = {
        "workspace": Path("/tmp"),
        "bsp_family": "nxp",
        "machine": "m",
        "distro": "d",
        "image": "i",
        "manifest": "x.xml",
        "repo_url": "https://example.com",
        "repo_branch": "main",
        "kas_container_image": "img:latest",
    }
    base.update(over)
    return BuildConfig(**base)  # type: ignore[arg-type]


def _no_probe(*_a: object, **_k: object) -> bool:
    raise AssertionError("no endpoint probe may run here")


def _patch_probes(monkeypatch: pytest.MonkeyPatch, hash_up: object, pr_up: object) -> list[tuple[str, int]]:
    seen: list[tuple[str, int]] = []

    def make(up: object) -> object:
        if callable(up):
            return up

        def probe(host: str, port: int, **_k: object) -> bool:
            seen.append((host, port))
            return bool(up)

        return probe

    monkeypatch.setattr(hashserv, "central_listening", make(hash_up))
    monkeypatch.setattr(prserv, "central_listening", make(pr_up))
    return seen


# --- scheduler ---------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, "", "   "])
def test_scheduler_unset_skips(value: str | None) -> None:
    result = check_scheduler_value(_cfg(scheduler=value))
    assert result.name == "scheduler"
    assert result.status is Status.SKIP


@pytest.mark.unit
@pytest.mark.parametrize("value", ["basic", "speed", "completion"])
def test_scheduler_builtin_passes(value: str) -> None:
    result = check_scheduler_value(_cfg(scheduler=value))
    assert result.status is Status.PASS


@pytest.mark.unit
def test_scheduler_unknown_warns_naming_value_builtins_and_bb_schedulers() -> None:
    result = check_scheduler_value(_cfg(scheduler="fastest"))
    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    for needle in ("fastest", "basic", "speed", "completion", "BB_SCHEDULERS"):
        assert needle in result.message


# --- central-endpoints -------------------------------------------------------


@pytest.mark.unit
def test_central_endpoints_skip_in_cluster_mode_without_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probes(monkeypatch, _no_probe, _no_probe)
    result = check_central_endpoints(_cfg(cluster=True, bb_hashserve="hub:8686", prserv_host="hub:8585"))
    assert result.name == "central-endpoints"
    assert result.status is Status.SKIP


@pytest.mark.unit
def test_central_endpoints_skip_when_none_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probes(monkeypatch, _no_probe, _no_probe)
    result = check_central_endpoints(_cfg(bb_hashserve=None, prserv_host=""))
    assert result.status is Status.SKIP


@pytest.mark.unit
def test_central_endpoints_pass_when_all_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch_probes(monkeypatch, True, True)
    result = check_central_endpoints(_cfg(bb_hashserve="hub:8686", prserv_host="hub:8585"))
    assert result.status is Status.PASS
    assert sorted(seen) == [("hub", 8585), ("hub", 8686)]


@pytest.mark.unit
def test_central_endpoints_probe_only_the_set_endpoint_with_default_port(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch_probes(monkeypatch, _no_probe, True)
    result = check_central_endpoints(_cfg(prserv_host="hub"))
    assert result.status is Status.PASS
    assert seen == [("hub", prserv.CENTRAL_DEFAULT_PORT)]


@pytest.mark.unit
def test_central_endpoints_warn_naming_each_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probes(monkeypatch, False, False)
    result = check_central_endpoints(_cfg(bb_hashserve="hub:8686", prserv_host="pr:8585"))
    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert "hub:8686" in result.message
    assert "pr:8585" in result.message
    assert result.fix_hint == "bakar settings set build.cluster true"


@pytest.mark.unit
def test_central_endpoints_warn_names_only_the_unreachable_one(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probes(monkeypatch, True, False)
    result = check_central_endpoints(_cfg(bb_hashserve="hub:8686", prserv_host="pr:8585"))
    assert result.status is Status.FAIL
    assert "pr:8585" in result.message
    assert "hub:8686" not in result.message


# --- registration ------------------------------------------------------------


@pytest.mark.unit
def test_registration() -> None:
    assert check_scheduler_value in SHARED_CHECKS
    assert check_central_endpoints in SHARED_CHECKS
    assert check_central_endpoints not in _CLUSTER_CHECKS
    meta = {fn: (name, sev) for fn, name, sev in _CHECK_METADATA}
    assert meta[check_scheduler_value] == ("scheduler", Severity.WARN)
    assert meta[check_central_endpoints] == ("central-endpoints", Severity.WARN)
    groups = dict(CHECK_GROUPS)
    assert "scheduler" in groups["Workspace & build config"]
    assert "central-endpoints" in groups["Cluster"]


@pytest.mark.unit
def test_run_all_with_nothing_configured_skips_every_new_check(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probes(monkeypatch, _no_probe, _no_probe)
    monkeypatch.setattr(diagnostics.probes, "http_reachable", _no_probe)
    names = {"scheduler", "central-endpoints", "sstate-mirror-reachable", "dl-mirror-reachable"}
    cfg = _cfg(
        scheduler=None,
        bb_hashserve=None,
        prserv_host=None,
        sstate_mirror_url=None,
        dl_mirror_url=None,
    )
    results = run_all(cfg, only=names)
    assert {r.name for r in results} == names
    assert all(r.status is Status.SKIP for r in results), [(r.name, r.status) for r in results]
