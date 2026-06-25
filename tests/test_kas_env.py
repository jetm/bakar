"""Unit tests for bakar.steps.kas_build._ccache_args.

Verifies the workspace-root ccache bind-mount that replaced the dangling
per-BSP ``ccache`` symlinks.  The mount is injected via the ``--runtime-args``
CLI flag rather than ``KAS_RUNTIME_ARGS`` env-var, because ``kas-container``
unconditionally overwrites that variable before its option-parsing loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.config import BuildConfig
from bakar.steps.kas_build import _build_env, _ccache_args

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _make_cfg(workspace: Path, bsp_family: str = "nxp", *, host_mode: bool = False) -> BuildConfig:
    return BuildConfig(
        workspace=workspace,
        bsp_family=bsp_family,  # type: ignore[arg-type]
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        repo_url="https://example.invalid/repo.git",
        repo_branch="imx-6.6.52-2.2.2",
        kas_container_image="jetm/kas-build-env:5.2-f40",
        host_mode=host_mode,
    )


def test_ccache_args_container_mode_returns_flag(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    args = _ccache_args(cfg)
    expected_mount = f"-v {tmp_path / 'ccache'}:/work/ccache:rw"
    assert args == ["--runtime-args", expected_mount]


def test_ccache_args_creates_dir(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    assert not (tmp_path / "ccache").exists()
    _ccache_args(cfg)
    assert (tmp_path / "ccache").is_dir()


def test_ccache_args_host_mode_returns_empty(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path, host_mode=True)
    assert _ccache_args(cfg) == []


def test_ccache_args_shared_for_nxp_and_ti(tmp_path: Path) -> None:
    """NXP and TI get identical mount args pointing at the workspace-root cache."""
    cfg_nxp = _make_cfg(tmp_path, bsp_family="nxp")
    cfg_ti = _make_cfg(tmp_path, bsp_family="ti")
    assert _ccache_args(cfg_nxp) == _ccache_args(cfg_ti)


def test_build_env_kas_work_dir_per_bsp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """KAS_WORK_DIR must scope to the BSP subtree, not the workspace root."""
    cfg_ti = _make_cfg(tmp_path, bsp_family="ti")
    cfg_nxp = _make_cfg(tmp_path, bsp_family="nxp")

    env_ti = _build_env(cfg_ti)
    env_nxp = _build_env(cfg_nxp)

    assert env_ti["KAS_WORK_DIR"].endswith("/ti")
    assert env_nxp["KAS_WORK_DIR"].endswith("/nxp")


# ---------------------------------------------------------------------------
# PSI pressure throttle and scheduler emission tests
# ---------------------------------------------------------------------------


def _make_tuning_cfg(
    workspace: Path,
    *,
    pressure_max_cpu: int | None = None,
    pressure_max_io: int | None = None,
    pressure_max_memory: int | None = None,
    scheduler: str | None = None,
    sstate_dir: str | None = None,
    dl_dir: str | None = None,
) -> BuildConfig:
    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        repo_url="https://example.invalid/repo.git",
        repo_branch="scarthgap",
        kas_container_image="jetm/kas-build-env:latest",
        pressure_max_cpu=pressure_max_cpu,
        pressure_max_io=pressure_max_io,
        pressure_max_memory=pressure_max_memory,
        scheduler=scheduler,
        sstate_dir=sstate_dir,
        dl_dir=dl_dir,
    )


def test_all_pressure_keys_set_emits_all_three(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BB_PRESSURE_MAX_* are emitted converted from avg10 percent to bitbake us/s."""
    monkeypatch.delenv("BB_PRESSURE_MAX_CPU", raising=False)
    monkeypatch.delenv("BB_PRESSURE_MAX_IO", raising=False)
    monkeypatch.delenv("BB_PRESSURE_MAX_MEMORY", raising=False)
    cfg = _make_tuning_cfg(tmp_path, pressure_max_cpu=60, pressure_max_io=45, pressure_max_memory=20)

    env = _build_env(cfg)

    assert env["BB_PRESSURE_MAX_CPU"] == "600000"
    assert env["BB_PRESSURE_MAX_IO"] == "450000"
    assert env["BB_PRESSURE_MAX_MEMORY"] == "200000"


def test_partial_pressure_keys_omit_unset_dimensions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only BB_PRESSURE_MAX_CPU is emitted when io and memory are None."""
    monkeypatch.delenv("BB_PRESSURE_MAX_CPU", raising=False)
    monkeypatch.delenv("BB_PRESSURE_MAX_IO", raising=False)
    monkeypatch.delenv("BB_PRESSURE_MAX_MEMORY", raising=False)
    cfg = _make_tuning_cfg(tmp_path, pressure_max_cpu=50)

    env = _build_env(cfg)

    assert env["BB_PRESSURE_MAX_CPU"] == "500000"
    assert "BB_PRESSURE_MAX_IO" not in env
    assert "BB_PRESSURE_MAX_MEMORY" not in env


def test_fractional_pressure_percent_converts_to_integer_us(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fractional avg10 percent threshold converts to a whole us/s value."""
    monkeypatch.delenv("BB_PRESSURE_MAX_MEMORY", raising=False)
    cfg = _make_tuning_cfg(tmp_path, pressure_max_memory=20.5)

    env = _build_env(cfg)

    assert env["BB_PRESSURE_MAX_MEMORY"] == "205000"


def test_scheduler_emitted_when_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BB_SCHEDULER is emitted with the configured value."""
    monkeypatch.delenv("BB_SCHEDULER", raising=False)
    cfg = _make_tuning_cfg(tmp_path, scheduler="completion")

    env = _build_env(cfg)

    assert env["BB_SCHEDULER"] == "completion"


def test_scheduler_absent_when_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BB_SCHEDULER is not injected when cfg.scheduler is None and env is unset."""
    monkeypatch.delenv("BB_SCHEDULER", raising=False)
    cfg = _make_tuning_cfg(tmp_path)

    env = _build_env(cfg)

    assert "BB_SCHEDULER" not in env


def test_cfg_sstate_dir_used_when_env_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cfg.sstate_dir reaches SSTATE_DIR when the env var is unset."""
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    cfg = _make_tuning_cfg(tmp_path, sstate_dir="/data/sstate")

    env = _build_env(cfg)

    assert env["SSTATE_DIR"] == "/data/sstate"


def test_env_sstate_dir_beats_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-set SSTATE_DIR env var wins over cfg.sstate_dir."""
    monkeypatch.setenv("SSTATE_DIR", "/env/sstate")
    cfg = _make_tuning_cfg(tmp_path, sstate_dir="/cfg/sstate")

    env = _build_env(cfg)

    assert env["SSTATE_DIR"] == "/env/sstate"


# ---------------------------------------------------------------------------
# Persistent hashserv (BB_HASHSERVE) injection tests
# ---------------------------------------------------------------------------


def _hashequiv_cfg(
    workspace: Path,
    *,
    use_hashequiv: bool = True,
    host_mode: bool = False,
) -> BuildConfig:
    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        repo_url="https://example.invalid/repo.git",
        repo_branch="imx-6.6.52-2.2.2",
        kas_container_image="jetm/kas-build-env:5.2-f40",
        host_mode=host_mode,
        use_hashequiv=use_hashequiv,
    )


def test_build_env_omits_bb_hashserve_when_use_hashequiv_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When cfg.use_hashequiv is False, BB_HASHSERVE is not set (overlay falls back to auto)."""
    monkeypatch.delenv("BB_HASHSERVE", raising=False)
    cfg = _hashequiv_cfg(tmp_path, use_hashequiv=False)

    env = _build_env(cfg)

    assert "BB_HASHSERVE" not in env


def test_build_env_host_mode_keeps_localhost_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Host mode: ensure_running's localhost URL is set verbatim."""
    monkeypatch.delenv("BB_HASHSERVE", raising=False)
    monkeypatch.setattr(
        "bakar.steps.kas_build.hashserv.ensure_running",
        lambda _state_key, **_kw: "ws://localhost:50000",
    )
    cfg = _hashequiv_cfg(tmp_path, use_hashequiv=True, host_mode=True)

    env = _build_env(cfg)

    assert env["BB_HASHSERVE"] == "ws://localhost:50000"


def test_build_env_container_mode_rewrites_to_host_docker_internal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Container mode: localhost is rewritten to host.docker.internal so the container can reach it."""
    monkeypatch.delenv("BB_HASHSERVE", raising=False)
    monkeypatch.setattr(
        "bakar.steps.kas_build.hashserv.ensure_running",
        lambda _state_key, **_kw: "ws://localhost:50000",
    )
    cfg = _hashequiv_cfg(tmp_path, use_hashequiv=True, host_mode=False)

    env = _build_env(cfg)

    assert env["BB_HASHSERVE"] == "ws://host.docker.internal:50000"


def test_build_env_omits_bb_hashserve_when_ensure_running_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ensure_running returns None, BB_HASHSERVE is not set so the overlay falls back to auto."""
    monkeypatch.delenv("BB_HASHSERVE", raising=False)
    monkeypatch.setattr(
        "bakar.steps.kas_build.hashserv.ensure_running",
        lambda _state_key, **_kw: None,
    )
    cfg = _hashequiv_cfg(tmp_path, use_hashequiv=True, host_mode=False)

    env = _build_env(cfg)

    assert "BB_HASHSERVE" not in env


# ---------------------------------------------------------------------------
# _ccache_args runtime-args concatenation (host-gateway injection) tests
# ---------------------------------------------------------------------------


def test_runtime_args_host_mode_returns_empty(tmp_path: Path) -> None:
    """Host mode: no container runtime args at all."""
    cfg = _hashequiv_cfg(tmp_path, use_hashequiv=True, host_mode=True)
    assert _ccache_args(cfg) == []


def test_runtime_args_container_no_hashserv_returns_ccache_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """use_hashequiv False: single --runtime-args pair, ccache mount only, no --add-host."""
    cfg = _hashequiv_cfg(tmp_path, use_hashequiv=False, host_mode=False)
    result = _ccache_args(cfg)
    assert len(result) == 2
    assert result[0] == "--runtime-args"
    assert f"-v {tmp_path / 'ccache'}:/work/ccache:rw" in result[1]
    assert "host.docker.internal" not in result[1]


def test_runtime_args_container_with_hashserv_appends_add_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """use_hashequiv True + daemon running: both ccache mount and --add-host inside same string."""
    monkeypatch.setattr(
        "bakar.steps.kas_build.hashserv.is_running",
        lambda _root: True,
    )
    cfg = _hashequiv_cfg(tmp_path, use_hashequiv=True, host_mode=False)
    result = _ccache_args(cfg)
    # Pin the single-flag shape: must be exactly 2 elements, not 4. A two-pair
    # `[--runtime-args, X, --runtime-args, Y]` shape would let the second
    # occurrence overwrite the first inside kas-container.
    assert len(result) == 2
    assert result[0] == "--runtime-args"
    assert f"-v {tmp_path / 'ccache'}:/work/ccache:rw" in result[1]
    assert "--add-host=host.docker.internal:host-gateway" in result[1]


def test_runtime_args_container_hashserv_configured_but_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """use_hashequiv True always injects --add-host regardless of daemon state.

    _build_env() starts the daemon after _ccache_args() builds the command
    string, so we cannot gate on is_running() here: the flag would be absent
    on the first build. Add it unconditionally when use_hashequiv is True.
    """
    monkeypatch.setattr(
        "bakar.steps.kas_build.hashserv.is_running",
        lambda _root: False,
    )
    cfg = _hashequiv_cfg(tmp_path, use_hashequiv=True, host_mode=False)
    result = _ccache_args(cfg)
    assert len(result) == 2
    assert result[0] == "--runtime-args"
    assert f"-v {tmp_path / 'ccache'}:/work/ccache:rw" in result[1]
    assert "--add-host=host.docker.internal:host-gateway" in result[1]


def test_runtime_args_eventlog_path_appended_when_provided(tmp_path: Path) -> None:
    """eventlog_path appends -e BB_DEFAULT_EVENTLOG=<path> inside the single --runtime-args string.

    kas-container only forwards a fixed env-var allowlist into Docker, so
    BB_DEFAULT_EVENTLOG must travel via --runtime-args -e, not the subprocess env.
    """
    cfg = _hashequiv_cfg(tmp_path, use_hashequiv=False, host_mode=False)
    result = _ccache_args(cfg, eventlog_path="/work/build/runs/20260604/bitbake_eventlog.json")
    assert len(result) == 2
    assert result[0] == "--runtime-args"
    assert "-e BB_DEFAULT_EVENTLOG=/work/build/runs/20260604/bitbake_eventlog.json" in result[1]


def test_runtime_args_eventlog_path_absent_when_none(tmp_path: Path) -> None:
    """Without eventlog_path, BB_DEFAULT_EVENTLOG is not injected (dry-run callers pass nothing)."""
    cfg = _hashequiv_cfg(tmp_path, use_hashequiv=False, host_mode=False)
    result = _ccache_args(cfg)
    assert "BB_DEFAULT_EVENTLOG" not in result[1]


def test_runtime_args_eventlog_path_ignored_in_host_mode(tmp_path: Path) -> None:
    """Host mode: _ccache_args returns [] even when eventlog_path is supplied."""
    cfg = _hashequiv_cfg(tmp_path, use_hashequiv=False, host_mode=True)
    assert _ccache_args(cfg, eventlog_path="/some/path") == []


# ---------------------------------------------------------------------------
# _ccache_args run_id label injection (bakar.run_id container targeting) tests
# ---------------------------------------------------------------------------


def test_runtime_args_container_run_id_appends_single_label(tmp_path: Path) -> None:
    """Container mode + run_id: one --runtime-args pair carrying the bakar.run_id label.

    The label lets `bakar stop` resolve the exact container of a run via
    `docker ps -f label=bakar.run_id=<id>`; it is appended to the single
    concatenated runtime-args string, never as a second --runtime-args pair.
    """
    cfg = _hashequiv_cfg(tmp_path, use_hashequiv=False, host_mode=False)
    result = _ccache_args(cfg, run_id="20260101-000000")
    assert len(result) == 2
    assert result[0] == "--runtime-args"
    assert "--label bakar.run_id=20260101-000000" in result[1]
    # The ccache mount must still be present alongside the injected label.
    assert f"-v {tmp_path / 'ccache'}:/work/ccache:rw" in result[1]


def test_runtime_args_host_mode_ignores_run_id(tmp_path: Path) -> None:
    """Host mode short-circuits to [] regardless of run_id (no container to label)."""
    cfg = _hashequiv_cfg(tmp_path, use_hashequiv=True, host_mode=True)
    assert _ccache_args(cfg, run_id="20260101-000000") == []


def test_runtime_args_run_id_none_omits_label(tmp_path: Path) -> None:
    """run_id=None (dry-run/preview callers) injects no bakar.run_id label."""
    cfg = _hashequiv_cfg(tmp_path, use_hashequiv=False, host_mode=False)
    result = _ccache_args(cfg, run_id=None)
    assert "bakar.run_id" not in result[1]


def test_build_env_forwards_sdkmachine_when_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A host SDKMACHINE is forwarded into the kas-container env so SDK-target
    builds (`bakar build --target avocado-complete`) pick the SDK arch."""
    monkeypatch.setenv("SDKMACHINE", "x86_64")
    env = _build_env(_make_cfg(tmp_path))
    assert env["SDKMACHINE"] == "x86_64"


def test_build_env_omits_sdkmachine_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SDKMACHINE absent from the host env is not synthesized into the container env."""
    monkeypatch.delenv("SDKMACHINE", raising=False)
    env = _build_env(_make_cfg(tmp_path))
    assert "SDKMACHINE" not in env


def test_cfg_kas_container_image_exported_to_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cfg.kas_container_image reaches KAS_CONTAINER_IMAGE when the env var is absent."""
    monkeypatch.delenv("KAS_CONTAINER_IMAGE", raising=False)
    cfg = _make_cfg(tmp_path)

    env = _build_env(cfg)

    assert env["KAS_CONTAINER_IMAGE"] == "jetm/kas-build-env:5.2-f40"


def test_env_kas_container_image_beats_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-set KAS_CONTAINER_IMAGE env var wins over cfg.kas_container_image."""
    monkeypatch.setenv("KAS_CONTAINER_IMAGE", "override/image:1.0")
    cfg = _make_cfg(tmp_path)

    env = _build_env(cfg)

    assert env["KAS_CONTAINER_IMAGE"] == "override/image:1.0"


def test_host_mode_omits_kas_container_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In host mode, KAS_CONTAINER_IMAGE is not injected (kas runs directly, no container)."""
    monkeypatch.delenv("KAS_CONTAINER_IMAGE", raising=False)
    cfg = _make_cfg(tmp_path, host_mode=True)

    env = _build_env(cfg)

    assert "KAS_CONTAINER_IMAGE" not in env
