"""Tests for sccache-dist distributed-compile support.

Task 1.1 covers config plumbing in ``bakar.user_config``: the ``sccache_dist``
bool and ``sccache_scheduler_url`` string keys parse from ``[build]``, default
to their unset values, and a non-bool ``sccache_dist`` raises a typed error
naming the field. The ``config`` keyword groups these tests for the task's
verify command.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

from bakar.user_config import (
    _BOOL_FIELDS,
    _BUILD_KEYS,
    _STR_FIELDS,
    SETTINGS_SCHEMA,
    UserConfig,
    load_user_config,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_config_sccache_fields_default_unset() -> None:
    """An all-defaults UserConfig has sccache disabled and no scheduler URL."""
    cfg = UserConfig()
    assert cfg.sccache_dist is False
    assert isinstance(cfg.sccache_dist, bool)
    assert cfg.sccache_scheduler_url is None


@pytest.mark.unit
def test_config_sccache_fields_absent_yield_defaults(tmp_path: Path) -> None:
    """A [build] table omitting both keys leaves the defaults in place."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("[build]\ndoctor = true\n")

    cfg = load_user_config(config_file)

    assert cfg.sccache_dist is False
    assert cfg.sccache_scheduler_url is None


@pytest.mark.unit
def test_config_sccache_dist_true_loads_as_bool(tmp_path: Path) -> None:
    """`[build] sccache_dist = true` loads as a real boolean True."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("[build]\nsccache_dist = true\n")

    cfg = load_user_config(config_file)

    assert cfg.sccache_dist is True
    assert isinstance(cfg.sccache_dist, bool)


@pytest.mark.unit
def test_config_sccache_scheduler_url_loads_as_str(tmp_path: Path) -> None:
    """A valid scheduler URL parses to a non-None string (falsifier guard)."""
    config_file = tmp_path / "config.toml"
    config_file.write_text('[build]\nsccache_dist = true\nsccache_scheduler_url = "http://localhost:10600"\n')

    cfg = load_user_config(config_file)

    assert cfg.sccache_scheduler_url == "http://localhost:10600"
    assert isinstance(cfg.sccache_scheduler_url, str)


@pytest.mark.unit
def test_config_sccache_dist_non_bool_raises_naming_field(tmp_path: Path) -> None:
    """A non-bool value for `sccache_dist` raises ValueError naming the field."""
    toml_content = textwrap.dedent("""\
        [build]
        sccache_dist = "yes"
    """)
    config_file = tmp_path / "config.toml"
    config_file.write_text(toml_content)

    with pytest.raises(ValueError, match="sccache_dist"):
        load_user_config(config_file)


@pytest.mark.unit
def test_config_sccache_scheduler_url_non_str_raises_naming_field(tmp_path: Path) -> None:
    """A non-string value for `sccache_scheduler_url` raises naming the field."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("[build]\nsccache_scheduler_url = 10600\n")

    with pytest.raises(ValueError, match="sccache_scheduler_url"):
        load_user_config(config_file)


@pytest.mark.unit
def test_config_sccache_fields_registered_in_type_sets() -> None:
    """The two fields belong to the correct type registries and the build map."""
    assert "sccache_dist" in _BOOL_FIELDS
    assert "sccache_scheduler_url" in _STR_FIELDS
    assert _BUILD_KEYS["sccache_dist"] == "sccache_dist"
    assert _BUILD_KEYS["sccache_scheduler_url"] == "sccache_scheduler_url"


@pytest.mark.unit
def test_config_sccache_keys_present_in_settings_schema() -> None:
    """Both dotted keys are recognized by the settings schema."""
    assert "build.sccache_dist" in SETTINGS_SCHEMA
    assert "build.sccache_scheduler_url" in SETTINGS_SCHEMA
    assert SETTINGS_SCHEMA["build.sccache_dist"].is_bool is True
    assert SETTINGS_SCHEMA["build.sccache_scheduler_url"].is_bool is False


# ---------------------------------------------------------------------------
# Task 1.2: BuildConfig fields, resolution, use_sccache_dist property, and
# the --sccache-dist / --sccache-scheduler CLI options. The ``resolve``
# keyword groups these tests for the task's verify command.
# ---------------------------------------------------------------------------


def _nxp_workspace(tmp_path: Path) -> Path:
    """Return a workspace path with the nxp subdir present (resolve() needs it)."""
    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.mark.unit
def test_resolve_use_sccache_dist_true_when_dist_set() -> None:
    """use_sccache_dist is True when sccache_dist is set (mirrors use_shared_cache)."""
    from pathlib import Path

    from bakar.config import BuildConfig

    cfg = BuildConfig(
        workspace=Path("/tmp"),
        bsp_family="nxp",
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        container_image="img:latest",
        sccache_dist=True,
    )
    assert cfg.use_sccache_dist is True


@pytest.mark.unit
def test_resolve_use_sccache_dist_false_by_default() -> None:
    """use_sccache_dist is False when sccache_dist defaults to False (falsifier)."""
    from pathlib import Path

    from bakar.config import BuildConfig

    cfg = BuildConfig(
        workspace=Path("/tmp"),
        bsp_family="nxp",
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        container_image="img:latest",
    )
    assert cfg.use_sccache_dist is False


@pytest.mark.unit
def test_resolve_threads_sccache_dist_from_user_config_true(tmp_path: Path) -> None:
    """UserConfig(sccache_dist=True) threads to cfg.sccache_dist is True."""
    from bakar.config import resolve

    uc = UserConfig(sccache_dist=True, sccache_scheduler_url="http://localhost:10600")

    cfg = resolve(workspace=_nxp_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    assert cfg.sccache_dist is True
    assert cfg.sccache_scheduler_url == "http://localhost:10600"
    assert cfg.use_sccache_dist is True


@pytest.mark.unit
def test_resolve_sccache_dist_default_false_without_user_config(tmp_path: Path) -> None:
    """Without a user_config, sccache_dist resolves to False and url to None."""
    from bakar.config import resolve

    cfg = resolve(workspace=_nxp_workspace(tmp_path), bsp_family="nxp")

    assert cfg.sccache_dist is False
    assert cfg.sccache_scheduler_url is None
    assert cfg.use_sccache_dist is False


@pytest.mark.unit
def test_resolve_cli_scheduler_overrides_config(tmp_path: Path) -> None:
    """The --sccache-scheduler CLI value overrides a config-set scheduler URL.

    Mirrors the replace(cfg, sccache_scheduler_url=...) sites in build.py: the
    CLI flag wins over the value resolved from UserConfig. This is the task's
    falsifier guard - a config-set scheduler URL must NOT survive a CLI flag.
    """
    from dataclasses import replace

    from bakar.config import resolve

    uc = UserConfig(sccache_dist=True, sccache_scheduler_url="http://config-host:10600")
    cfg = resolve(workspace=_nxp_workspace(tmp_path), bsp_family="nxp", user_config=uc)

    cli_scheduler = "http://cli-host:10600"
    cfg = replace(cfg, sccache_scheduler_url=cli_scheduler)

    assert cfg.sccache_scheduler_url == cli_scheduler


@pytest.mark.unit
def test_resolve_build_help_shows_sccache_options() -> None:
    """`bakar build --help` must expose --sccache-dist and --sccache-scheduler."""
    import re

    from typer.testing import CliRunner

    from bakar.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["build", "--help"])

    assert result.exit_code == 0, result.output
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "--sccache-dist" in plain
    assert "--sccache-scheduler" in plain


# ---------------------------------------------------------------------------
# Task 3.1: host-mode build-env passthrough. When cfg.use_sccache_dist, the
# scheduler URL is exported into the build env (BAKAR_SCCACHE_SCHEDULER_URL,
# mirroring BAKAR_SSTATE_MIRROR_URL) for the sccache overlay to consume; no key
# is emitted when disabled. This is the host-mode path only - no container
# mounts or host-gateway rewrite here. The ``host_env`` keyword groups these
# tests for the task's verify command.
# ---------------------------------------------------------------------------


def _sccache_build_cfg(
    workspace: Path,
    *,
    sccache_dist: bool = False,
    sccache_scheduler_url: str | None = None,
) -> object:
    """Return a host-mode BuildConfig with the sccache knobs set."""
    from bakar.config import BuildConfig

    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        repo_url="https://example.invalid/repo.git",
        repo_branch="imx-6.6.52-2.2.2",
        container_image="jetm/kas-build-env:5.2-f40",
        host_mode=True,
        sccache_dist=sccache_dist,
        sccache_scheduler_url=sccache_scheduler_url,
    )


@pytest.mark.unit
def test_host_env_carries_scheduler_when_enabled(tmp_path: Path) -> None:
    """When sccache_dist is enabled, the scheduler URL lands in the build env."""
    from bakar.steps.kas_build import _build_env

    cfg = _sccache_build_cfg(
        tmp_path,
        sccache_dist=True,
        sccache_scheduler_url="http://localhost:10600",
    )

    env = _build_env(cfg, ensure_hashserv=False)

    assert env["BAKAR_SCCACHE_SCHEDULER_URL"] == "http://localhost:10600"


@pytest.mark.unit
def test_host_env_omits_sccache_when_disabled(tmp_path: Path) -> None:
    """When sccache_dist is disabled, no sccache key is emitted (falsifier guard)."""
    from bakar.steps.kas_build import _build_env

    cfg = _sccache_build_cfg(
        tmp_path,
        sccache_dist=False,
        sccache_scheduler_url="http://localhost:10600",
    )

    env = _build_env(cfg, ensure_hashserv=False)

    assert "BAKAR_SCCACHE_SCHEDULER_URL" not in env


# ---------------------------------------------------------------------------
# Task 2.1: the sccache tuning overlay and its append helper. When
# cfg.use_sccache_dist, _sccache_extra_overlays() returns the
# bakar-tuning-sccache.yml path and _tuning_extra_overlays() includes it;
# both yield nothing when disabled. The overlay swaps the compiler launcher
# (CCACHE = "sccache ") and removes the mutually-exclusive ccache inherit
# (INHERIT:remove = "ccache"). The ``overlay`` keyword groups these tests for
# the task's verify command.
# ---------------------------------------------------------------------------


def _overlay_cfg(*, sccache_dist: bool = False) -> object:
    """Return a minimal BuildConfig for the overlay helper tests."""
    from pathlib import Path

    from bakar.config import BuildConfig

    return BuildConfig(
        workspace=Path("/tmp"),
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        container_image="img:latest",
        sccache_dist=sccache_dist,
    )


@pytest.mark.unit
def test_overlay_sccache_extra_overlays_returns_path_when_enabled() -> None:
    """When use_sccache_dist is True the helper returns the sccache overlay path."""
    from bakar.commands._helpers import _sccache_extra_overlays

    cfg = _overlay_cfg(sccache_dist=True)
    result = _sccache_extra_overlays(cfg)  # type: ignore[arg-type]

    assert len(result) == 1
    assert result[0].name == "bakar-tuning-sccache.yml"
    assert result[0].is_file(), "overlay file must exist in the installed overlays/ dir"


@pytest.mark.unit
def test_overlay_sccache_extra_overlays_returns_empty_when_disabled() -> None:
    """When use_sccache_dist is False the helper returns an empty list (falsifier)."""
    from bakar.commands._helpers import _sccache_extra_overlays

    cfg = _overlay_cfg(sccache_dist=False)
    result = _sccache_extra_overlays(cfg)  # type: ignore[arg-type]

    assert result == []


@pytest.mark.unit
def test_overlay_in_tuning_stack_when_enabled() -> None:
    """_tuning_extra_overlays includes the sccache overlay when enabled."""
    from bakar.commands._helpers import _tuning_extra_overlays

    cfg = _overlay_cfg(sccache_dist=True)
    names = [p.name for p in _tuning_extra_overlays(cfg)]  # type: ignore[arg-type]

    assert "bakar-tuning-sccache.yml" in names


@pytest.mark.unit
def test_overlay_absent_from_tuning_stack_when_disabled() -> None:
    """_tuning_extra_overlays omits the sccache overlay when disabled (falsifier)."""
    from bakar.commands._helpers import _tuning_extra_overlays

    cfg = _overlay_cfg(sccache_dist=False)
    names = [p.name for p in _tuning_extra_overlays(cfg)]  # type: ignore[arg-type]

    assert "bakar-tuning-sccache.yml" not in names


@pytest.mark.unit
def test_overlay_swaps_launcher_and_removes_ccache_inherit() -> None:
    """The overlay routes CC through sccache and drops the ccache inherit.

    ccache and sccache are mutually-exclusive launchers; chaining them
    double-wraps the compiler and breaks caching. This is the task's
    falsifier guard - the overlay MUST remove ccache when enabling sccache.
    """
    from bakar.commands._helpers import _sccache_extra_overlays

    cfg = _overlay_cfg(sccache_dist=True)
    overlay = _sccache_extra_overlays(cfg)[0]  # type: ignore[arg-type]
    text = overlay.read_text()

    assert 'CCACHE = "sccache "' in text
    assert 'INHERIT:remove = "ccache"' in text


@pytest.mark.unit
def test_overlay_exports_sccache_scheduler_env() -> None:
    """The overlay declares the scheduler-URL passthrough env var so kas whitelists it."""
    from bakar.commands._helpers import _sccache_extra_overlays

    cfg = _overlay_cfg(sccache_dist=True)
    overlay = _sccache_extra_overlays(cfg)[0]  # type: ignore[arg-type]
    text = overlay.read_text()

    assert "BAKAR_SCCACHE_SCHEDULER_URL" in text


# ---------------------------------------------------------------------------
# Task 3.2: the doctor/preflight gate. When cfg.use_sccache_dist, the gate
# fails with an actionable message if the `sccache` binary is absent from PATH
# or the configured scheduler URL does not respond, and passes when both are
# present. SKIP when sccache_dist is disabled. The ``doctor`` keyword groups
# these tests for the task's verify command.
# ---------------------------------------------------------------------------


def _doctor_cfg(
    *,
    sccache_dist: bool = False,
    sccache_scheduler_url: str | None = None,
) -> object:
    """Return a host-mode BuildConfig for the sccache doctor-check tests."""
    from pathlib import Path

    from bakar.config import BuildConfig

    return BuildConfig(
        workspace=Path("/tmp"),
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        container_image="img:latest",
        host_mode=True,
        sccache_dist=sccache_dist,
        sccache_scheduler_url=sccache_scheduler_url,
    )


@pytest.mark.unit
def test_doctor_sccache_skips_when_disabled() -> None:
    """The check SKIPs (does not fail) when sccache_dist is disabled."""
    from bakar.diagnostics import Status, check_sccache_dist

    cfg = _doctor_cfg(sccache_dist=False)
    result = check_sccache_dist(cfg)  # type: ignore[arg-type]

    assert result.status is Status.SKIP


@pytest.mark.unit
def test_doctor_sccache_in_shared_checks() -> None:
    """The sccache check is wired into the shared check list so the gate runs it."""
    from bakar.diagnostics import SHARED_CHECKS, check_sccache_dist

    assert check_sccache_dist in SHARED_CHECKS


@pytest.mark.unit
def test_doctor_sccache_fails_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the sccache binary is absent from PATH the check FAILs and BLOCKs.

    This is the task's falsifier guard - a missing prerequisite must NOT
    silently fall through to a local-only compile.
    """
    import shutil

    from bakar.diagnostics import Severity, Status, check_sccache_dist

    monkeypatch.setattr(shutil, "which", lambda name: None)

    cfg = _doctor_cfg(sccache_dist=True, sccache_scheduler_url="http://localhost:10600")
    result = check_sccache_dist(cfg)  # type: ignore[arg-type]

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "sccache" in result.message


@pytest.mark.unit
def test_doctor_sccache_fails_when_scheduler_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the scheduler URL does not respond the check FAILs and BLOCKs.

    Falsifier guard: the gate must NOT pass when the scheduler is down.
    """
    import shutil
    import socket

    from bakar.diagnostics import Severity, Status, check_sccache_dist

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/sccache")

    def _refuse(*args: object, **kwargs: object) -> object:
        raise OSError("connection refused")

    monkeypatch.setattr(socket, "create_connection", _refuse)

    cfg = _doctor_cfg(sccache_dist=True, sccache_scheduler_url="http://localhost:10600")
    result = check_sccache_dist(cfg)  # type: ignore[arg-type]

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK


@pytest.mark.unit
def test_doctor_sccache_passes_when_binary_and_scheduler_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check PASSes when binary is on PATH, scheduler responds, and client dist is enabled."""
    import shutil
    import socket
    import subprocess
    import types

    from bakar.diagnostics import Status, check_sccache_dist

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/sccache")

    class _FakeSock:
        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: _FakeSock())
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(
            stdout='{"SchedulerStatus":["http://localhost:10600/",{"num_servers":1}]}',
            stderr="",
            returncode=0,
        ),
    )

    cfg = _doctor_cfg(sccache_dist=True, sccache_scheduler_url="http://localhost:10600")
    result = check_sccache_dist(cfg)  # type: ignore[arg-type]

    assert result.status is Status.PASS


@pytest.mark.unit
def test_doctor_sccache_fails_when_client_dist_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable scheduler but a Disabled client must BLOCK, not pass to local-only.

    Falsifier guard for the silent-degradation the check exists to prevent: when
    ``~/.config/sccache/config`` lacks the dist auth token the running client
    reports ``{"Disabled":"disabled"}`` and every compile runs local-only, yet a
    bare TCP probe to the scheduler still succeeds. ``sccache --dist-status`` is
    the only signal that reflects the client's real runtime state.
    """
    import shutil
    import socket
    import subprocess
    import types

    from bakar.diagnostics import Severity, Status, check_sccache_dist

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/sccache")

    class _FakeSock:
        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: _FakeSock())
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(stdout='{"Disabled":"disabled"}', stderr="", returncode=0),
    )

    cfg = _doctor_cfg(sccache_dist=True, sccache_scheduler_url="http://localhost:10600")
    result = check_sccache_dist(cfg)  # type: ignore[arg-type]

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "disabled" in result.message.lower()


@pytest.mark.unit
def test_doctor_sccache_fails_when_scheduler_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """sccache enabled but no scheduler URL configured FAILs (nothing to reach)."""
    import shutil

    from bakar.diagnostics import Severity, Status, check_sccache_dist

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/sccache")

    cfg = _doctor_cfg(sccache_dist=True, sccache_scheduler_url=None)
    result = check_sccache_dist(cfg)  # type: ignore[arg-type]

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
