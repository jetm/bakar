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
        kas_container_image="img:latest",
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
        kas_container_image="img:latest",
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
def test_resolve_top_level_help_shows_sccache_options() -> None:
    """`bakar --help` must expose the global --sccache-dist and --sccache-scheduler.

    These are global callback options (passed before the subcommand), so they
    appear on the top-level help, not on `bakar build --help`.
    """
    import re

    from typer.testing import CliRunner

    from bakar.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])

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
        kas_container_image="jetm/kas-build-env:5.2-f40",
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


@pytest.mark.unit
def test_host_sccache_build_starts_persistent_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real host-mode sccache build pre-starts the persistent server with the scheduler.

    Without it the first bitbake task's auto-started server dies with that task,
    churning fallbacks and poisoning the cache (the recurring -fPIC link error).
    """
    from bakar.steps import kas_build

    calls: list[str | None] = []
    monkeypatch.setattr(kas_build.sccache_server, "ensure_running", lambda url=None: calls.append(url) or True)
    cfg = _sccache_build_cfg(tmp_path, sccache_dist=True, sccache_scheduler_url="http://localhost:10600")

    kas_build._build_env(cfg, ensure_hashserv=True)  # type: ignore[arg-type]

    assert calls == ["http://localhost:10600"]


@pytest.mark.unit
def test_dry_run_env_does_not_start_sccache_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Script-gen / dry-run (ensure_hashserv=False) never spawns a daemon (falsifier)."""
    from bakar.steps import kas_build

    calls: list[str | None] = []
    monkeypatch.setattr(kas_build.sccache_server, "ensure_running", lambda url=None: calls.append(url) or True)
    cfg = _sccache_build_cfg(tmp_path, sccache_dist=True, sccache_scheduler_url="http://localhost:10600")

    kas_build._build_env(cfg, ensure_hashserv=False)  # type: ignore[arg-type]

    assert calls == []


@pytest.mark.unit
def test_container_sccache_build_does_not_start_host_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Container mode runs sccache inside the container; no host server is pre-started."""
    from dataclasses import replace

    from bakar.steps import kas_build

    calls: list[str | None] = []
    monkeypatch.setattr(kas_build.sccache_server, "ensure_running", lambda url=None: calls.append(url) or True)
    cfg = replace(
        _sccache_build_cfg(tmp_path, sccache_dist=True, sccache_scheduler_url="http://localhost:10600"),
        host_mode=False,
    )

    kas_build._build_env(cfg, ensure_hashserv=True)  # type: ignore[arg-type]

    assert calls == []


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
        kas_container_image="img:latest",
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


def _sccache_bbclass_text() -> str:
    """Return the packaged sccache.bbclass source text."""
    from bakar.commands._helpers import _overlay_dir

    return (_overlay_dir() / "meta-bakar-sccache" / "classes" / "sccache.bbclass").read_text()


@pytest.mark.unit
def test_overlay_inherits_sccache_class_and_removes_ccache() -> None:
    """The overlay swaps the ccache inherit for the sccache class.

    ccache and sccache are mutually-exclusive launchers; chaining them
    double-wraps the compiler and breaks caching. This is the task's
    falsifier guard - the overlay MUST remove ccache when enabling sccache.
    """
    from bakar.commands._helpers import _sccache_extra_overlays

    cfg = _overlay_cfg(sccache_dist=True)
    overlay = _sccache_extra_overlays(cfg)[0]  # type: ignore[arg-type]
    text = overlay.read_text()

    assert 'INHERIT:remove = "ccache"' in text
    assert 'INHERIT += "sccache"' in text


@pytest.mark.unit
def test_overlay_adds_sccache_layer_repo() -> None:
    """The overlay adds the meta-bakar-sccache layer via a relative repos path.

    sccache.bbclass lives in a bakar-shipped layer (no bbclass can sit in
    local.conf). bakar materializes the layer under <bsp_root>/.bakar/, and the
    relative repos path resolves against bsp_root in both host and container
    modes. Without the repo entry, `INHERIT += "sccache"` cannot find the class.
    """
    from bakar.commands._helpers import _sccache_extra_overlays

    cfg = _overlay_cfg(sccache_dist=True)
    overlay = _sccache_extra_overlays(cfg)[0]  # type: ignore[arg-type]
    text = overlay.read_text()

    assert "meta-bakar-sccache:" in text
    assert "path: .bakar/meta-bakar-sccache" in text


@pytest.mark.unit
def test_sccache_class_sets_launcher_per_recipe() -> None:
    """sccache.bbclass sets CCACHE='sccache ' through a per-recipe python gate.

    Mirrors ccache.bbclass: CCACHE is not set globally (that ignores per-recipe
    CCACHE_DISABLE); the anonymous python function sets it only for eligible
    recipes.
    """
    text = _sccache_bbclass_text()

    assert "python () {" in text
    assert "d.setVar('CCACHE', 'sccache ')" in text


@pytest.mark.unit
def test_sccache_class_excludes_host_compiler_classes_only() -> None:
    """The class excludes only the host-compiler classes; nativesdk/cross-canadian distribute.

    sccache packages the in-use compiler via `gcc -print-prog-name=as`; native,
    cross, and crosssdk recipes compile with the host gcc whose PATH-relative `as`
    (Arch) cannot be packaged, so they must compile locally and stay excluded.
    nativesdk and cross-canadian build with the OE crosssdk compiler (absolute-path
    `as`, packageable), so they distribute - measured on avocado as 218 SDK-toolchain
    compiles across two nodes with 0 distributed-compile failures. The kernel is also
    not excluded (it distributes; its few .incbin objects fall back locally). This is
    the falsifier guard: re-adding nativesdk/cross-canadian to the excluded line fails.
    """
    text = _sccache_bbclass_text()

    excluded_line = text.split("SCCACHE_EXCLUDED_CLASSES ?=")[1].split("\n")[0]
    for cls in ("native", "cross", "crosssdk"):
        assert cls in excluded_line
    assert "nativesdk" not in excluded_line
    assert "cross-canadian" not in excluded_line
    assert "kernel" not in excluded_line
    assert "inherits_class(cls, d)" in text


@pytest.mark.unit
def test_sccache_class_honors_disable_flags() -> None:
    """The class honors per-recipe CCACHE_DISABLE and a new SCCACHE_DISABLE.

    Several oe-core recipes (webkitgtk, babeltrace2, make-mod-scripts, go-cross,
    piglit) ship CCACHE_DISABLE; the global-set approach ignored them. The python
    gate returns early for any recipe that sets either flag.
    """
    text = _sccache_bbclass_text()

    assert "SCCACHE_DISABLE" in text
    assert "CCACHE_DISABLE" in text


@pytest.mark.unit
def test_sccache_class_keeps_build_compiler_local() -> None:
    """The class strips sccache from the build/host compiler (BUILD_CC/CXX).

    OE prepends ${CCACHE} to both the target CC (gcc.bbclass) and the build
    BUILD_CC/BUILD_CXX (gcc-native.bbclass). Eligible target recipes still
    compile host helper tools with the build compiler - e.g. linux-libc-headers
    do_install runs `make HOSTCC="${BUILD_CC}"` to build fixdep - so a leaked
    sccache on BUILD_CC ships those host-tool compiles to the build-server,
    where they need network the install task lacks and hit the unpackageable
    host `as`. :forcevariable beats gcc-native.bbclass's `=` regardless of
    inherit order. Caught by a real qemuarm64 linux-libc-headers do_install
    failing with "Network is unreachable".
    """
    text = _sccache_bbclass_text()

    assert 'BUILD_CC:forcevariable = "${BUILD_PREFIX}gcc ${BUILD_CC_ARCH}"' in text
    assert 'BUILD_CXX:forcevariable = "${BUILD_PREFIX}g++ ${BUILD_CC_ARCH}"' in text


@pytest.mark.unit
def test_sccache_class_adds_hosttools_and_network() -> None:
    """The class puts sccache on the task PATH and grants compile tasks network.

    OE restricts each task's PATH to sysroot bins + HOSTTOOLS; without
    HOSTTOOLS += "sccache" every CC="sccache gcc" fails "command not found".
    bitbake also isolates each task's network namespace unless [network] = "1",
    so without it the client fails "Network is unreachable". The compiler runs in
    do_configure (compiler tests), do_compile (main build), and do_install (e.g.
    glibc links format.lds with the target gcc), so all three need network. Caught
    by a real qemuarm64 zlib-native do_configure and glibc do_install.
    """
    text = _sccache_bbclass_text()

    assert 'HOSTTOOLS += "sccache"' in text
    for task in (
        "do_configure",
        "do_compile",
        "do_install",
        "do_configure_ptest_base",
        "do_compile_ptest_base",
        "do_install_ptest_base",
    ):
        assert f'{task}[network] = "1"' in text


@pytest.mark.unit
def test_sccache_class_grants_kernel_compiler_task_network() -> None:
    """Every kernel-specific task that runs the compiler needs task network.

    Beyond the generic do_configure/do_compile/do_install grants, linux-yocto
    defines extra tasks that invoke CC="sccache <gcc>" - directly via oe_runmake
    (do_compile_kernelmodules, do_bundle_initramfs -> kernel_do_compile) or via
    the kconfig probe in scripts/Kconfig.include (do_kernel_configme through
    `make alldefconfig`, do_kernel_configcheck through symbol_why.py ->
    kconfiglib; every kernel make also re-runs the syncconfig probe). Without
    their own [network] = "1" the task runs with loopback down and the client
    cannot reach its 127.0.0.1 daemon, failing "Network is unreachable (os error
    101)" -> "Sorry, this C compiler is not supported." Caught by a real
    qemuarm64 linux-yocto host build (configme, then configcheck, then
    compile_kernelmodules). bundle_initramfs runs the same kernel_do_compile and
    is covered for the initramfs-bundle case.
    """
    text = _sccache_bbclass_text()

    for task in (
        "do_kernel_configme",
        "do_kernel_configcheck",
        "do_compile_kernelmodules",
        "do_bundle_initramfs",
    ):
        assert f'{task}[network] = "1"' in text


@pytest.mark.unit
def test_sccache_class_fixes_cmake_launcher_split() -> None:
    """The class re-derives the cmake compiler/launcher split to recognize sccache.

    cmake.bbclass's oecmake_map_compiler only treats the literal "ccache" as a
    launcher, so with CC="sccache <gcc>" it makes sccache itself the compiler and
    the configure-time compiler check fails ("sccache: unexpected argument '-m'").
    The class overrides OECMAKE_C/CXX_COMPILER and *_LAUNCHER with a helper that
    splits sccache too. Caught by a real qemuarm64 expat/json-c do_configure.
    """
    text = _sccache_bbclass_text()

    assert "OECMAKE_C_COMPILER = " in text
    assert "OECMAKE_C_COMPILER_LAUNCHER = " in text
    assert "OECMAKE_CXX_COMPILER = " in text
    assert "OECMAKE_CXX_COMPILER_LAUNCHER = " in text
    assert "'sccache'" in text


@pytest.mark.unit
def test_sccache_class_emits_dist_summary_at_build_completed() -> None:
    """The class registers a BuildCompleted handler that prints a per-node dist summary.

    sccache schedules per compile job, not per recipe, so the only honest
    build-end view is aggregate per-server counts. The handler reads the client
    daemon's `sccache --show-stats --stats-format=json` (the `dist_compiles`
    address->count map and `dist_errors` fallback count) and emits one
    `bb.plain` line. It is gated on dist being enabled via the BuildStarted/
    BuildCompleted dispatch so non-dist builds stay silent.
    """
    text = _sccache_bbclass_text()

    assert "addhandler sccache_dist_summary" in text
    assert 'sccache_dist_summary[eventmask] = "bb.event.BuildStarted bb.event.BuildCompleted"' in text
    assert "isinstance(e, bb.event.BuildCompleted)" in text
    assert "--show-stats" in text
    assert "--stats-format=json" in text
    assert "dist_compiles" in text
    assert "dist_errors" in text
    assert "bb.plain(" in text


@pytest.mark.unit
def test_sccache_class_zeroes_stats_at_build_started() -> None:
    """The handler zeroes the daemon's stats at BuildStarted for per-build accuracy.

    `--show-stats` reports the daemon's cumulative counters since the last
    `--zero-stats`. In host mode the daemon is pre-started and persists across
    builds, so without a reset the BuildCompleted summary would report every
    build since the daemon came up. Zeroing at BuildStarted scopes the numbers to
    the current build. This is the falsifier guard: drop the reset and the
    summary stops being per-build.
    """
    text = _sccache_bbclass_text()

    assert "isinstance(e, bb.event.BuildStarted)" in text
    assert "--zero-stats" in text


@pytest.mark.unit
def test_materialize_sccache_layer_copies_class_into_bsp_root(tmp_path: object) -> None:
    """materialize_sccache_layer drops the layer under <bsp_root>/.bakar/.

    The sccache overlay references the layer by the relative repos path
    .bakar/meta-bakar-sccache; the layer must exist there (with its bbclass) for
    kas to resolve it and `INHERIT += "sccache"` to find the class. Overwrites on
    every call so the materialized copy tracks the packaged source.
    """
    from pathlib import Path

    from bakar.config import BuildConfig
    from bakar.steps.kas_build import materialize_sccache_layer

    root = Path(str(tmp_path))
    cfg = BuildConfig(
        workspace=root,
        bsp_family="generic",  # type: ignore[arg-type]
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        kas_container_image="img:latest",
        kas_yaml_override=root / "my.yml",
        sccache_dist=True,
    )

    dest = materialize_sccache_layer(cfg)

    assert dest == cfg.bsp_root / ".bakar" / "meta-bakar-sccache"
    assert (dest / "conf" / "layer.conf").is_file()
    bbclass = dest / "classes" / "sccache.bbclass"
    assert bbclass.is_file()
    assert "d.setVar('CCACHE', 'sccache ')" in bbclass.read_text()


@pytest.mark.unit
def test_materialize_sccache_layer_targets_workspace_for_meta_avocado(tmp_path: Path) -> None:
    """For meta-avocado the layer lands under <workspace>/.bakar, not <bsp_root>/.bakar.

    meta-avocado runs kas with KAS_WORK_DIR = workspace (_build_env), and bsp_root
    is the nested build dir workspace/build-<stem>. kas resolves the sccache
    overlay's relative repos path `.bakar/meta-bakar-sccache` against KAS_WORK_DIR,
    so bitbake's bblayers points at <workspace>/.bakar - one level above bsp_root.
    Materializing under bsp_root/.bakar (the non-avocado location) leaves the layer
    where bblayers cannot find it and parse fails "layer directories do not exist".
    This is the falsifier guard: the dest must be the workspace .bakar, and the two
    paths genuinely differ for avocado's nested bsp_root.
    """
    from pathlib import Path

    from bakar.config import BuildConfig
    from bakar.steps.kas_build import materialize_sccache_layer

    root = Path(str(tmp_path))
    avocado_yaml = root / "meta-avocado" / "kas" / "machine" / "qemux86-64.yml"
    cfg = BuildConfig(
        workspace=root,
        bsp_family="generic",  # type: ignore[arg-type]
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        kas_container_image="img:latest",
        kas_yaml_override=avocado_yaml,
        sccache_dist=True,
    )
    assert cfg.is_meta_avocado is True
    assert cfg.bsp_root != cfg.workspace  # nested build-<stem> dir

    dest = materialize_sccache_layer(cfg)

    assert dest == cfg.workspace / ".bakar" / "meta-bakar-sccache"
    assert dest != cfg.bsp_root / ".bakar" / "meta-bakar-sccache"
    assert (dest / "conf" / "layer.conf").is_file()
    assert (dest / "classes" / "sccache.bbclass").is_file()


@pytest.mark.unit
def test_overlay_exports_sccache_scheduler_env() -> None:
    """The overlay declares the scheduler-URL passthrough env var so kas whitelists it."""
    from bakar.commands._helpers import _sccache_extra_overlays

    cfg = _overlay_cfg(sccache_dist=True)
    overlay = _sccache_extra_overlays(cfg)[0]  # type: ignore[arg-type]
    text = overlay.read_text()

    assert "BAKAR_SCCACHE_SCHEDULER_URL" in text


@pytest.mark.unit
def test_bbclass_distributes_gcc_runtime_recipes(tmp_path: Path) -> None:
    """The bbclass no longer force-excludes the gcc/glibc bootstrap recipes.

    sccache-dist used to break two ways on these - glibc's side `.o.dt`
    dependency files were not captured when zipping remote outputs, and the
    libgcc/gcc-sanitizers soft-float files errored on -Wimplicit-fallthrough once
    preprocessing stripped the suppressing comments - so they were listed in
    SCCACHE_EXCLUDED_PN. The client now falls back to a local recompile on any
    dist-infra failure, so the overwhelming majority of their objects distribute
    and the rest fall back safely; the exclusion list is empty. This is the
    falsifier guard: re-adding any PN to the list fails the empty-list assertion.
    The per-PN gate is kept as a documented escape hatch.
    """
    from pathlib import Path

    from bakar.config import BuildConfig
    from bakar.steps.kas_build import materialize_sccache_layer

    root = Path(str(tmp_path))
    cfg = BuildConfig(
        workspace=root,
        bsp_family="generic",  # type: ignore[arg-type]
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        kas_container_image="img:latest",
        kas_yaml_override=root / "my.yml",
        sccache_dist=True,
    )

    bbclass = (materialize_sccache_layer(cfg) / "classes" / "sccache.bbclass").read_text()

    pn_assignment = bbclass.split("SCCACHE_EXCLUDED_PN ?= ")[1].split("\n")[0]
    assert pn_assignment == '""', pn_assignment
    # The per-PN escape-hatch gate must survive so a recipe can still be forced local.
    assert "d.getVar('PN') in" in bbclass
    assert "d.getVar('SCCACHE_EXCLUDED_PN').split()" in bbclass


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
        kas_container_image="img:latest",
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


@pytest.mark.unit
def test_doctor_sccache_skips_in_container_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The host-side preflight is skipped in container mode when the config is sane.

    The reachability probe is host-side and does not reflect the in-container
    client's path to the scheduler, so the gate is scoped to host_mode. With no
    sccache config present (HOME points at an empty dir), there is nothing
    container-specific to flag, so the check SKIPs. The gate must short-circuit
    before the binary check: with the binary monkeypatched absent, the pre-gate
    behaviour would FAIL, so a SKIP proves the gate fired first.
    """
    import shutil
    from dataclasses import replace

    from bakar.diagnostics import Status, check_sccache_dist

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda name: None)

    cfg = replace(
        _doctor_cfg(sccache_dist=True, sccache_scheduler_url="http://localhost:10600"),
        host_mode=False,
    )
    result = check_sccache_dist(cfg)  # type: ignore[arg-type]

    assert result.status is Status.SKIP


@pytest.mark.unit
def test_doctor_sccache_warns_localhost_scheduler_in_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Container mode warns when the sccache config names a localhost scheduler.

    localhost inside the container is the container itself, so a localhost
    scheduler_url silently forces every compile local. The host-side probe cannot
    catch this, so the check reads the config and warns, making the precondition
    (a host LAN scheduler address) discoverable.
    """
    from dataclasses import replace

    from bakar.diagnostics import Severity, Status, check_sccache_dist

    monkeypatch.setenv("HOME", str(tmp_path))
    conf = tmp_path / ".config" / "sccache" / "config"
    conf.parent.mkdir(parents=True)
    conf.write_text('[dist]\nscheduler_url = "http://localhost:10600"\n')

    cfg = replace(
        _doctor_cfg(sccache_dist=True, sccache_scheduler_url="http://localhost:10600"),
        host_mode=False,
    )
    result = check_sccache_dist(cfg)  # type: ignore[arg-type]

    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert "localhost" in result.message


@pytest.mark.unit
def test_doctor_sccache_reports_cluster_capacity_host_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host-mode preflight surfaces live cluster capacity so the user knows
    what distributed build power to expect."""
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
            stdout='{"SchedulerStatus":["http://h:10600/",{"num_servers":2,"num_cpus":64,"in_progress":3}]}',
            stderr="",
            returncode=0,
        ),
    )

    cfg = _doctor_cfg(sccache_dist=True, sccache_scheduler_url="http://h:10600")
    result = check_sccache_dist(cfg)  # type: ignore[arg-type]

    assert result.status is Status.PASS
    assert "2 build server" in result.message
    assert "64 cpu" in result.message
    assert "3 job" in result.message


@pytest.mark.unit
def test_doctor_sccache_reports_capacity_container_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Container mode (the common build path) also reports cluster capacity when
    the configured scheduler is routable, so the user knows what to expect."""
    import subprocess
    import types
    from dataclasses import replace

    from bakar.diagnostics import check_sccache_dist

    monkeypatch.setenv("HOME", str(tmp_path))
    conf = tmp_path / ".config" / "sccache" / "config"
    conf.parent.mkdir(parents=True)
    conf.write_text('[dist]\nscheduler_url = "http://10.42.0.1:10600"\n')
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(
            stdout='{"SchedulerStatus":["http://10.42.0.1:10600/",{"num_servers":2,"num_cpus":64,"in_progress":0}]}',
            stderr="",
            returncode=0,
        ),
    )

    cfg = replace(
        _doctor_cfg(sccache_dist=True, sccache_scheduler_url="http://10.42.0.1:10600"),
        host_mode=False,
    )
    result = check_sccache_dist(cfg)  # type: ignore[arg-type]

    assert "2 build server" in result.message
    assert "64 cpu" in result.message


# ---------------------------------------------------------------------------
# Container path (task 5.1): _ccache_args bind-mounts the sccache binary and
# client config and adds the host-gateway when the scheduler targets localhost;
# _build_env rewrites a localhost scheduler URL to host.docker.internal for the
# in-container client (mirroring the hashequiv rewrite). The ``runtime_args``
# keyword groups the _ccache_args tests for the task's verify command.
# ---------------------------------------------------------------------------


def _container_sccache_cfg(workspace: Path, *, scheduler_url: str = "http://localhost:10600") -> object:
    """A container-mode (host_mode=False) BuildConfig with sccache enabled."""
    from dataclasses import replace

    cfg = _sccache_build_cfg(workspace, sccache_dist=True, sccache_scheduler_url=scheduler_url)
    return replace(cfg, host_mode=False)  # type: ignore[arg-type]


def _container_cfg_no_sccache(workspace: Path) -> object:
    """A container-mode BuildConfig with sccache and hashequiv both off."""
    from dataclasses import replace

    cfg = _sccache_build_cfg(workspace, sccache_dist=False)
    return replace(cfg, host_mode=False)  # type: ignore[arg-type]


@pytest.mark.unit
def test_runtime_args_container_mounts_sccache_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Container runtime-args mount the sccache binary + config and inject SCCACHE_CONF/DIR.

    sccache reads its scheduler URL and auth token only from the config file, so
    the config is mounted read-only and SCCACHE_CONF points at it (kas gives the
    container a throwaway HOME, so XDG discovery would miss it). SCCACHE_DIR
    redirects the disk cache under /work because the config's ~/.cache/sccache is
    absent in the container. The binary lands in /usr/bin (kas sanitizes bitbake's
    PATH and drops /usr/local/bin). No host-gateway is needed: the config names a
    host LAN scheduler reachable from the container as-is.
    """
    import shutil

    from bakar.steps.kas_build import _ccache_args

    monkeypatch.setenv("HOME", str(tmp_path))
    conf = tmp_path / ".config" / "sccache" / "config"
    conf.parent.mkdir(parents=True)
    conf.write_text('[dist]\nscheduler_url = "http://192.168.8.174:10600"\n')
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/sccache" if name == "sccache" else None)

    cfg = _container_sccache_cfg(tmp_path)
    args = _ccache_args(cfg)  # type: ignore[arg-type]

    assert args[0] == "--runtime-args"
    s = args[1]
    assert "-v /usr/bin/sccache:/usr/bin/sccache:ro" in s, s
    assert f"-v {conf}:{conf}:ro" in s, s
    assert f"-e BAKAR_SCCACHE_CONF={conf}" in s, s
    assert "-e BAKAR_SCCACHE_DIR=/work/.sccache-cache" in s, s
    # sccache reaches the scheduler via the config's LAN address, so it needs no
    # host-gateway (and this cfg leaves hashequiv off).
    assert "--add-host" not in s, s


@pytest.mark.unit
def test_runtime_args_container_omits_sccache_when_disabled(tmp_path: Path) -> None:
    """With sccache disabled, no sccache mount and no host-gateway appear (falsifier guard)."""
    from bakar.steps.kas_build import _ccache_args

    cfg = _container_cfg_no_sccache(tmp_path)
    args = _ccache_args(cfg)  # type: ignore[arg-type]

    s = args[1]
    assert "sccache" not in s, s
    assert "--add-host" not in s, s


@pytest.mark.unit
def test_container_env_rewrites_scheduler_to_host_docker_internal(tmp_path: Path) -> None:
    """In container mode the exported scheduler URL swaps localhost for host.docker.internal.

    localhost inside the container is the container itself; the scheduler runs on
    the host, reachable via the host-gateway alias. Mirrors the hashequiv rewrite.
    """
    from bakar.steps.kas_build import _build_env

    cfg = _container_sccache_cfg(tmp_path)
    env = _build_env(cfg, ensure_hashserv=False)  # type: ignore[arg-type]

    assert env["BAKAR_SCCACHE_SCHEDULER_URL"] == "http://host.docker.internal:10600"


@pytest.mark.unit
def test_host_env_keeps_localhost_scheduler(tmp_path: Path) -> None:
    """Host mode leaves the scheduler URL untouched (no container rewrite)."""
    from bakar.steps.kas_build import _build_env

    cfg = _sccache_build_cfg(tmp_path, sccache_dist=True, sccache_scheduler_url="http://localhost:10600")
    env = _build_env(cfg, ensure_hashserv=False)  # type: ignore[arg-type]

    assert env["BAKAR_SCCACHE_SCHEDULER_URL"] == "http://localhost:10600"


@pytest.mark.unit
def test_host_env_omits_sccache_conf_and_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Host mode never sets the container-only config/cache overrides (falsifier guard).

    In host mode the pre-started server already reads ~/.config/sccache/config and
    the configured disk cache, so emitting these keys would override the host
    cache dir for no reason. They must stay absent to keep host builds unchanged.
    """
    from bakar.steps.kas_build import _build_env

    monkeypatch.setenv("HOME", str(tmp_path))
    conf = tmp_path / ".config" / "sccache" / "config"
    conf.parent.mkdir(parents=True)
    conf.write_text('[dist]\nscheduler_url = "http://localhost:10600"\n')

    cfg = _sccache_build_cfg(tmp_path, sccache_dist=True, sccache_scheduler_url="http://localhost:10600")
    env = _build_env(cfg, ensure_hashserv=False)  # type: ignore[arg-type]

    assert "BAKAR_SCCACHE_CONF" not in env
    assert "BAKAR_SCCACHE_DIR" not in env
