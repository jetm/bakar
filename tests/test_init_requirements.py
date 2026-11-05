"""Unit tests for bakar.commands._init_requirements."""

from __future__ import annotations

import dataclasses
import os
from typing import TYPE_CHECKING, Any, Literal

import pytest

from bakar import config, diagnostics
from bakar.commands._init_requirements import (
    Evaluation,
    checks_line,
    evaluate,
    proposed_config,
)
from bakar.diagnostics import CheckResult, Severity, Status
from bakar.setup.features import CATALOG, Feature, Requirement, feature_by_id

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping
    from pathlib import Path

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.startswith("BAKAR_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


class FakeRunner:
    """Stands in for diagnostics.run_all, returning canned results by name."""

    def __init__(self, results: dict[str, CheckResult], *, cluster_gate: bool = False) -> None:
        self.results = results
        self.cluster_gate = cluster_gate
        self.calls: list[frozenset[str]] = []

    def __call__(
        self,
        cfg: config.BuildConfig,
        bsp: object,
        *,
        only: Collection[str] | None = None,
        deadline: float | None = None,
    ) -> list[CheckResult]:
        assert bsp is None
        assert only is not None
        self.calls.append(frozenset(only))
        out = []
        for name in only:
            if (
                self.cluster_gate
                and name in {"shared-mounts", "central-hashserv", "central-prserv"}
                and not cfg.cluster
            ):
                continue
            if name in self.results:
                out.append(self.results[name])
        return out


def _result(name: str, status: Status, message: str = "msg", fix_hint: str | None = None) -> CheckResult:
    return CheckResult(name=name, severity=Severity.BLOCK, status=status, message=message, fix_hint=fix_hint)


def _cfg(tmp_path: Path, **overrides: object) -> config.BuildConfig:
    cfg = config.resolve(config.ResolveRequest(workspace=tmp_path))
    return dataclasses.replace(cfg, **overrides)


def _feature(*reqs: Requirement) -> Feature:
    return Feature(id="probe-feature", tier=1, title="T", summary="S", guide="g", requirements=reqs)


def _check_req(target: str, **flags: bool) -> Requirement:
    return Requirement("check", target, hard=True, fix="fix it", **flags)


def _eval(  # noqa: PLR0913 - mirrors evaluate()'s keyword options
    features: list[Feature],
    cfg: config.BuildConfig,
    runner: FakeRunner,
    *,
    phase: Literal["pre", "values"] = "pre",
    values: Mapping[str, str] | None = None,
    inputs: Mapping[str, str] | None = None,
    probes: Mapping[str, Any] | None = None,
    explain: bool = False,
) -> Evaluation:
    return evaluate(
        features,
        cfg,
        values=values or {},
        inputs=inputs or {},
        phase=phase,
        probes=probes or {},
        run_checks=runner,
        explain=explain,
    )


def test_pass_is_met(tmp_path: Path) -> None:
    runner = FakeRunner({"psi_support": _result("psi_support", Status.PASS)})
    ev = _eval([_feature(_check_req("psi_support"))], _cfg(tmp_path), runner)
    [res] = ev.results_for("probe-feature")
    assert res.met is True
    assert ev.hard_unmet("probe-feature") == []
    assert checks_line(ev) == "checks: 1/1 run"
    assert len(runner.calls) == 1


def test_skip_met_only_with_skip_ok(tmp_path: Path) -> None:
    runner = FakeRunner({"shared-mounts": _result("shared-mounts", Status.SKIP, "nothing configured")})
    cfg = _cfg(tmp_path)
    without = _eval([_feature(_check_req("shared-mounts"))], cfg, runner)
    assert without.results_for("probe-feature")[0].met is False
    assert len(without.hard_unmet("probe-feature")) == 1

    with_ok = _eval([_feature(_check_req("shared-mounts", skip_ok=True))], cfg, runner)
    assert with_ok.results_for("probe-feature")[0].met is True


def test_missing_result_is_unmet_and_counted(tmp_path: Path) -> None:
    runner = FakeRunner({"psi_support": _result("psi_support", Status.PASS)})
    feature = _feature(_check_req("psi_support"), _check_req("systemd-scope"))
    ev = _eval([feature], _cfg(tmp_path), runner)
    assert ev.scheduled == 2
    assert ev.ran == 1
    assert checks_line(ev) == "checks: 1/2 run"
    missing = ev.results_for("probe-feature")[1]
    assert missing.met is False
    assert missing.reason == "not verified: the check did not run"
    assert runner.calls == [frozenset({"psi_support", "systemd-scope"})]


def test_timeout_fail_is_unmet(tmp_path: Path) -> None:
    runner = FakeRunner(
        {"cache-mounts": _result("cache-mounts", Status.FAIL, "did not finish within 30s", fix_hint="check the mount")}
    )
    ev = _eval([_feature(_check_req("cache-mounts"))], _cfg(tmp_path), runner)
    [res] = ev.results_for("probe-feature")
    assert res.met is False
    assert "did not finish within 30s" in res.reason
    assert "check the mount" in res.reason


def test_host_only_not_applicable_in_container_mode(tmp_path: Path) -> None:
    runner = FakeRunner({})
    cfg = _cfg(tmp_path, host_mode=False)
    uninative = feature_by_id("uninative")
    ev = _eval([uninative], cfg, runner)
    path_res = next(r for r in ev.results_for("uninative") if r.requirement.kind == "path")
    assert path_res.applicable is False
    assert path_res.met is True
    assert "host mode" in ev.unavailable["uninative"]
    [refusal] = ev.hard_unmet("uninative")
    assert "host mode" in refusal.reason
    assert runner.calls == []


def test_proposed_config_leaves_real_config_byte_identical(tmp_path: Path) -> None:
    real = tmp_path / "config.toml"
    real.write_text("[build]\nccache = false\n# keep this comment\n")
    before = real.read_bytes()
    cfg, warnings = proposed_config(
        tmp_path,
        {"build.ccache": "true", "build.sstate_mirror_url": "http://mirror/sstate"},
        ["build.ccache_dir"],
        user_config_path=real,
    )
    assert real.read_bytes() == before
    assert cfg.ccache is True
    assert cfg.sstate_mirror_url == "http://mirror/sstate"
    assert warnings == []


def test_proposed_config_default_path_never_created(tmp_path: Path) -> None:
    proposed_config(tmp_path, {"build.ccache": "true"}, [])
    assert not (tmp_path / "home" / ".config" / "bakar" / "config.toml").exists()


def test_proposed_config_warns_naming_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_SCCACHE_DIST", "0")
    real = tmp_path / "config.toml"
    _cfg_out, warnings = proposed_config(tmp_path, {"build.sccache_dist": "true"}, [], user_config_path=real)
    assert len(warnings) == 1
    assert "BAKAR_SCCACHE_DIST" in warnings[0]
    assert not real.exists()


def test_central_node_values_phase_gets_shared_mounts_result(tmp_path: Path) -> None:
    node = feature_by_id("central-node")
    sets = {"build.bb_hashserve": "ws://10.0.0.1:8686", "build.prserv_host": "10.0.0.1:8585", **node.fixed}
    cfg, _ = proposed_config(tmp_path, sets, [], user_config_path=tmp_path / "config.toml")
    assert cfg.cluster is True
    runner = FakeRunner(
        {"shared-mounts": _result("shared-mounts", Status.SKIP, "cluster mode with no shared cache directories")},
        cluster_gate=True,
    )
    probes = {"central-postgres": lambda _c, _v, _i: (True, "ok")}
    ev = _eval(
        [node],
        cfg,
        runner,
        phase="values",
        values=sets,
        inputs={"bind_host": "10.0.0.1"},
        probes=probes,
    )
    mounts = next(r for r in ev.results_for("central-node") if r.requirement.target == "shared-mounts")
    assert mounts.met is True
    assert mounts.reason != "not verified: the check did not run"
    assert checks_line(ev) == "checks: 1/1 run"


def test_explain_sstate_mirror_without_url_not_checked(tmp_path: Path) -> None:
    runner = FakeRunner({})
    ev = _eval([feature_by_id("sstate-mirror")], _cfg(tmp_path), runner, phase="values", explain=True)
    results = ev.results_for("sstate-mirror")
    assert results
    assert all(r.met is None for r in results)
    assert all("build.sstate_mirror_url" in r.reason for r in results)
    assert all(r.reason.startswith("not checked: set") for r in results)
    assert runner.calls == []
    assert ev.hard_unmet("sstate-mirror") == []


def test_explain_runs_when_value_is_configured(tmp_path: Path) -> None:
    runner = FakeRunner({"sstate-mirror-reachable": _result("sstate-mirror-reachable", Status.FAIL, "unreachable")})
    cfg = _cfg(tmp_path, sstate_mirror_url="http://127.0.0.1:1")
    ev = _eval([feature_by_id("sstate-mirror")], cfg, runner, phase="values", explain=True)
    reach = ev.results_for("sstate-mirror")[0]
    assert reach.met is False
    assert runner.calls == [frozenset({"sstate-mirror-reachable", "sstate-mirror-shared-mount"})]


def test_binary_path_setting_and_probe_kinds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bakar.commands._init_requirements.shutil.which", lambda name: None)
    present = tmp_path / "present"
    present.touch()
    feature = _feature(
        Requirement("binary", "nope", hard=True, fix="f"),
        Requirement("path", str(present), hard=True, fix="f"),
        Requirement("path", str(tmp_path / "absent"), hard=False, fix="f"),
        Requirement("setting", "build.dl_dir", hard=True, fix="f"),
        Requirement("probe", "known", hard=True, fix="f"),
        Requirement("probe", "mystery", hard=True, fix="f"),
    )
    probes = {"known": lambda _c, _v, inputs: (inputs.get("host") == "pc2", "probe said so")}
    ev = _eval([feature], _cfg(tmp_path), FakeRunner({}), values={}, inputs={"host": "pc2"}, probes=probes)
    met = [r.met for r in ev.results_for("probe-feature")]
    assert met == [False, True, False, False, True, False]
    assert ev.results_for("probe-feature")[5].reason.startswith("not verified: unknown probe")
    assert len(ev.soft_unmet("probe-feature")) == 1
    assert checks_line(ev) == "checks: 0/0 run"

    ev2 = _eval([feature], _cfg(tmp_path), FakeRunner({}), values={"build.dl_dir": "/srv/dl"}, probes=probes)
    assert ev2.results_for("probe-feature")[3].met is True


def test_phase_split(tmp_path: Path) -> None:
    feature = _feature(_check_req("psi_support"), _check_req("cache-mounts", needs_values=True))
    runner = FakeRunner({})
    _eval([feature], _cfg(tmp_path), runner, phase="pre")
    _eval([feature], _cfg(tmp_path), runner, phase="values")
    assert runner.calls == [frozenset({"psi_support"}), frozenset({"cache-mounts"})]


def test_default_runner_looked_up_at_call_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = FakeRunner({"psi_support": _result("psi_support", Status.PASS)})
    monkeypatch.setattr(diagnostics, "run_all", runner)
    ev = evaluate([_feature(_check_req("psi_support"))], _cfg(tmp_path), values={}, inputs={}, phase="pre", probes={})
    assert ev.results_for("probe-feature")[0].met is True


def test_catalog_check_targets_registered() -> None:
    registered = set(diagnostics._CHECK_NAME.values())
    targets = {req.target for feature in CATALOG for req in feature.requirements if req.kind == "check"}
    assert targets, "catalog has no check-kind requirements"
    assert sorted(targets - registered) == []


def test_gate_skip_is_unmet_even_with_skip_ok(tmp_path: Path) -> None:
    gated = _result("shared-mounts", Status.SKIP, "not run: sstate unusable (see cache-mounts)")
    runner = FakeRunner({"shared-mounts": gated})
    ev = _eval([_feature(_check_req("shared-mounts", skip_ok=True))], _cfg(tmp_path), runner)
    [res] = ev.results_for("probe-feature")
    assert res.met is False
    assert len(ev.hard_unmet("probe-feature")) == 1
