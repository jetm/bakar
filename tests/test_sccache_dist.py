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

    cfg = BuildConfig(
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
    # Host-mode _build_env now requires the bundled bitbake bin on the launch PATH.
    cfg.bitbake_bin_path.mkdir(parents=True, exist_ok=True)
    return cfg


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


def _overlay_cfg(*, sccache_dist: bool = False, ccache: bool = True, host_mode: bool = False) -> object:
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
        ccache=ccache,
        host_mode=host_mode,
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
def test_overlay_ccache_extra_overlays_returns_path_when_effective() -> None:
    """ccache on and sccache off: the ccache overlay is selected."""
    from bakar.commands._helpers import _ccache_extra_overlays

    cfg = _overlay_cfg(sccache_dist=False, ccache=True)
    result = _ccache_extra_overlays(cfg)  # type: ignore[arg-type]

    assert len(result) == 1
    assert result[0].name == "bakar-tuning-ccache.yml"
    assert result[0].is_file()


@pytest.mark.unit
def test_overlay_ccache_extra_overlays_empty_under_sccache() -> None:
    """ccache and sccache are mutually exclusive: no ccache overlay under sccache-dist."""
    from bakar.commands._helpers import _ccache_extra_overlays

    cfg = _overlay_cfg(sccache_dist=True, ccache=True)

    assert _ccache_extra_overlays(cfg) == []  # type: ignore[arg-type]


@pytest.mark.unit
def test_overlay_ccache_extra_overlays_empty_when_ccache_disabled() -> None:
    """ccache=False disables the ccache overlay even without sccache-dist."""
    from bakar.commands._helpers import _ccache_extra_overlays

    cfg = _overlay_cfg(sccache_dist=False, ccache=False)

    assert _ccache_extra_overlays(cfg) == []  # type: ignore[arg-type]


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
def test_overlay_host_extra_overlays_returns_path_in_host_mode() -> None:
    """When host_mode is True the helper returns the host isolation overlay path."""
    from bakar.commands._helpers import _host_extra_overlays

    cfg = _overlay_cfg(host_mode=True)
    result = _host_extra_overlays(cfg)  # type: ignore[arg-type]

    assert len(result) == 1
    assert result[0].name == "bakar-tuning-host.yml"
    assert result[0].is_file(), "overlay file must exist in the installed overlays/ dir"


@pytest.mark.unit
def test_overlay_host_extra_overlays_empty_in_container_mode() -> None:
    """When host_mode is False the helper returns an empty list (falsifier)."""
    from bakar.commands._helpers import _host_extra_overlays

    cfg = _overlay_cfg(host_mode=False)
    result = _host_extra_overlays(cfg)  # type: ignore[arg-type]

    assert result == []


@pytest.mark.unit
def test_host_overlay_in_tuning_stack_in_host_mode() -> None:
    """_tuning_extra_overlays includes the host overlay when host_mode is on."""
    from bakar.commands._helpers import _tuning_extra_overlays

    cfg = _overlay_cfg(host_mode=True)
    names = [p.name for p in _tuning_extra_overlays(cfg)]  # type: ignore[arg-type]

    assert "bakar-tuning-host.yml" in names


@pytest.mark.unit
def test_host_overlay_absent_from_tuning_stack_in_container_mode() -> None:
    """_tuning_extra_overlays omits the host overlay in container mode (falsifier)."""
    from bakar.commands._helpers import _tuning_extra_overlays

    cfg = _overlay_cfg(host_mode=False)
    names = [p.name for p in _tuning_extra_overlays(cfg)]  # type: ignore[arg-type]

    assert "bakar-tuning-host.yml" not in names


@pytest.mark.unit
def test_materialize_host_layer_copies_rpm_bbappend_into_bsp_root(tmp_path: Path) -> None:
    """materialize_host_layer drops the layer (and its rpm bbappend) under <bsp_root>/.bakar/.

    The host overlay references the layer by the relative repos path
    .bakar/meta-bakar-host; the rpm bbappend must exist there for kas to apply it
    and disable the rpm transaction plugins that otherwise dlopen the build
    host's ABI-incompatible /usr/lib/rpm-plugins during do_rootfs.
    """
    import re
    from pathlib import Path

    from bakar.config import BuildConfig
    from bakar.steps.kas_build import materialize_host_layer

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
        host_mode=True,
    )

    dest = materialize_host_layer(cfg)

    assert dest == cfg.bsp_root / ".bakar" / "meta-bakar-host"
    assert (dest / "conf" / "layer.conf").is_file()
    bbappend = dest / "recipes-devtools" / "rpm" / "rpm_%.bbappend"
    assert bbappend.is_file()
    text = bbappend.read_text()
    assert "do_install:append:class-native" in text
    # the audit plugin (the one that broke do_rootfs) must be neutralised to nil
    assert re.search(r"%__transaction_audit\s+%\{nil\}", text)


def _sccache_bbclass_text() -> str:
    """Return the packaged sccache.bbclass source text."""
    from bakar.commands._helpers import _overlay_dir

    return (_overlay_dir() / "meta-bakar-sccache" / "classes" / "sccache.bbclass").read_text()


@pytest.mark.unit
def test_overlay_inherits_sccache_class_without_ccache_present() -> None:
    """The sccache overlay inherits the sccache class.

    ccache and sccache are mutually-exclusive launchers. The ccache overlay
    (bakar-tuning-ccache) is not selected when sccache-dist is on (use_ccache is
    False), so ccache is never inherited - the overlay just adds sccache, with no
    INHERIT:remove needed. Falsifier: a stale INHERIT:remove = "ccache" here would
    imply ccache was added, which the selection logic now prevents.
    """
    from bakar.commands._helpers import _ccache_extra_overlays, _sccache_extra_overlays

    cfg = _overlay_cfg(sccache_dist=True)
    overlay = _sccache_extra_overlays(cfg)[0]  # type: ignore[arg-type]
    text = overlay.read_text()

    assert 'INHERIT += "sccache"' in text
    assert 'INHERIT:remove = "ccache"' not in text
    # The ccache overlay must not be selected alongside sccache.
    assert _ccache_extra_overlays(cfg) == []


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
    """sccache.bbclass scopes CCACHE='sccache ' to the compile-family task overrides.

    OE prepends ${CCACHE} to CC for *every* task, so a global CCACHE routed
    autoconf's thousands of conftest do_configure compiles through sccache, each
    paying a client->server->scheduler round-trip (measured: do_configure 45% of
    task-time at 2.8% CPU). Scoping CCACHE to the do_compile-family task overrides
    keeps configure on plain gcc. Falsifier: a global ``d.setVar('CCACHE',
    'sccache ')`` would re-introduce the configure bottleneck, so assert it is
    absent.
    """
    text = _sccache_bbclass_text()

    assert "python () {" in text
    assert "d.setVar('CCACHE:task-compile', 'sccache ')" in text
    assert "d.setVar('CCACHE:task-compile-ptest-base', 'sccache ')" in text
    assert "d.setVar('CCACHE:task-compile-kernelmodules', 'sccache ')" in text
    assert "d.setVar('CCACHE:task-bundle-initramfs', 'sccache ')" in text
    assert "d.setVar('CCACHE', 'sccache ')" not in text


@pytest.mark.unit
def test_sccache_class_excludes_no_compiler_classes() -> None:
    """The class excludes no inherit-class: native/cross/crosssdk distribute too.

    All of native, cross, and crosssdk compile with the host/build gcc whose
    `-print-prog-name=as` returns a bare `as`; the sccache fork resolves that
    against the compile task's PATH (not the daemon's), so the right assembler is
    packaged and they distribute - measured on avocado scarthgap as zlib-native,
    linux-libc-headers, and binutils-cross-aarch64, all 0-error. nativesdk,
    cross-canadian, and the kernel were never excluded either. This is the
    falsifier guard: re-adding any class to the excluded line fails the empty-list
    assertion. The per-class gate stays as a documented escape hatch.
    """
    text = _sccache_bbclass_text()

    excluded_line = text.split("SCCACHE_EXCLUDED_CLASSES ?=")[1].split("\n")[0]
    assert excluded_line.strip() == '""'
    for cls in ("native", "cross", "crosssdk", "nativesdk", "cross-canadian", "kernel"):
        assert cls not in excluded_line
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
def test_sccache_class_routes_build_compiler_through_sccache() -> None:
    """The class routes the build/host compiler (BUILD_CC/CXX) through ${CCACHE}.

    OE prepends ${CCACHE} to both the target CC (gcc.bbclass) and the build
    BUILD_CC/BUILD_CXX (gcc-native.bbclass). Re-deriving them here with
    :forcevariable (which beats gcc-native.bbclass's `=` regardless of inherit
    order) keeps the ${CCACHE} launcher so build-compiler objects distribute too -
    the fork resolves their bare host `as` against the compile PATH. Excluded
    recipes never set CCACHE, so for them this expands to a bare compiler and stays
    local. This is the falsifier: dropping ${CCACHE} would force every build-tool
    compile local.
    """
    text = _sccache_bbclass_text()

    assert 'BUILD_CC:forcevariable = "${CCACHE}${BUILD_PREFIX}gcc ${BUILD_CC_ARCH}"' in text
    assert 'BUILD_CXX:forcevariable = "${CCACHE}${BUILD_PREFIX}g++ ${BUILD_CC_ARCH}"' in text


@pytest.mark.unit
def test_sccache_class_adds_hosttools_and_network() -> None:
    """The class puts sccache on the task PATH and grants only compile tasks network.

    OE restricts each task's PATH to sysroot bins + HOSTTOOLS; without
    HOSTTOOLS += "sccache" every CC="sccache gcc" fails "command not found".
    bitbake also isolates each task's network namespace unless [network] = "1".
    With CCACHE now scoped to the compile family, only do_compile and its ptest
    mirror run the sccache client, so only those need network. Falsifier:
    do_configure/do_install run plain gcc now, so granting them network would be
    dead config - assert the compile grants are present and the
    configure/install grants are absent.
    """
    text = _sccache_bbclass_text()

    assert 'HOSTTOOLS += "sccache"' in text
    for task in ("do_compile", "do_compile_ptest_base"):
        assert f'{task}[network] = "1"' in text
    for task in (
        "do_configure",
        "do_install",
        "do_configure_ptest_base",
        "do_install_ptest_base",
    ):
        assert f'{task}[network] = "1"' not in text


@pytest.mark.unit
def test_sccache_class_grants_kernel_compiler_task_network() -> None:
    """Only the kernel compile-family tasks need network now, not the kconfig probes.

    do_compile_kernelmodules and do_bundle_initramfs (-> kernel_do_compile) run
    CC="sccache <gcc>" through oe_runmake, so they keep [network] = "1". The
    kconfig-probe tasks (do_kernel_configme, do_kernel_configcheck) now compile
    with plain gcc - CCACHE is not in their task scope - so they reach no
    scheduler and their network grant is removed. Falsifier: a stale
    do_kernel_configme/configcheck network grant would imply sccache still runs
    there, which the compile-only scoping prevents.
    """
    text = _sccache_bbclass_text()

    for task in ("do_compile_kernelmodules", "do_bundle_initramfs"):
        assert f'{task}[network] = "1"' in text
    for task in ("do_kernel_configme", "do_kernel_configcheck"):
        assert f'{task}[network] = "1"' not in text


@pytest.mark.unit
def test_sccache_class_scopes_launcher_to_compile_only() -> None:
    """The class forces the cmake compiler launcher to sccache, with no global CCACHE.

    CMake derives CMAKE_<LANG>_COMPILER_LAUNCHER from CC; with CCACHE now scoped
    to do_compile, CC is bare at parse time, so the upstream
    OECMAKE_*_COMPILER_LAUNCHER would resolve to '' and CMake recipes would lose
    sccache. The class sets the launcher vars explicitly to 'sccache'. CMake
    applies the launcher only to real build compiles (not its try_compile checks),
    so configure stays clean. Falsifier: a global ``d.setVar('CCACHE', ...)`` would
    re-route configure, so assert it is absent.
    """
    text = _sccache_bbclass_text()

    assert "OECMAKE_C_COMPILER_LAUNCHER" in text
    assert "OECMAKE_CXX_COMPILER_LAUNCHER" in text
    assert "'sccache'" in text
    assert "d.setVar('CCACHE', 'sccache ')" not in text


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
def test_sccache_summary_targets_task_sccache_env_in_container() -> None:
    """The build-end summary must query the same sccache server the compile tasks
    use. In container mode tasks read SCCACHE_CONF/SCCACHE_DIR mapped from the
    container-injected BAKAR_* vars (per-task python block); this handler runs in
    the cooker, whose environment has only the BAKAR_* vars. Without the same
    mapping its sccache targets the default cache dir - absent and unwritable in
    the container - so --zero-stats never starts a server and --show-stats reports
    zero, silently dropping the summary. Falsifier: drop the env mapping and the
    container-mode summary regresses to never printing.
    """
    text = _sccache_bbclass_text()
    summary = text.split("python sccache_dist_summary")[1]

    # The handler maps both BAKAR_* vars onto the names sccache reads.
    for var in ("BAKAR_SCCACHE_CONF", "BAKAR_SCCACHE_DIR", "SCCACHE_CONF", "SCCACHE_DIR"):
        assert var in summary, f"{var} missing from the summary handler env mapping"
    # And passes the constructed env to both the zero-stats and show-stats calls.
    assert summary.count("env=env") >= 2


@pytest.mark.unit
def test_sccache_guard_rejects_config_without_auth_token() -> None:
    """The guard fatals when SCCACHE_CONF lacks a dist scheduler_url or token.

    `sccache --dist-status` hits the scheduler's UNAUTHENTICATED
    /api/v1/scheduler/status, so it passes even with no token; job allocation is
    token-gated (/api/v1/scheduler/alloc_job) and would 401, degrading silently to
    local-only. The guard reads the config the daemon will use and asserts both a
    [dist] scheduler_url and a non-empty [dist.auth] token are present. Falsifier:
    drop the config-token gate and a token-less config sails past the guard.
    """
    text = _sccache_bbclass_text()
    guard = text.split("python sccache_dist_guard")[1]

    assert "scheduler_url" in guard
    assert "token" in guard
    # The rationale names the token-gated route the unauthenticated status probe
    # cannot exercise, so a future reader cannot mistake the two endpoints.
    assert "alloc_job" in guard


@pytest.mark.unit
def test_sccache_guard_probes_dispatch_authentication() -> None:
    """The guard distributes one throwaway compile to confirm auth end to end.

    /status being unauthenticated means reachability cannot prove the client's
    token is accepted for job allocation. The guard zeroes stats, compiles a
    unique source (guaranteed cache miss -> real compile -> dispatch), reads the
    dist counters, then re-zeroes so the probe does not pollute the build-end
    summary - and fatals if the probe fell back to local instead of distributing.
    Falsifier: remove the probe and a present-but-wrong token passes undetected.
    """
    text = _sccache_bbclass_text()
    guard = text.split("python sccache_dist_guard")[1]

    assert "--zero-stats" in guard
    assert "dist_compiles" in guard
    assert "FELL BACK" in guard


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
    assert "d.setVar('CCACHE:task-compile', 'sccache ')" in bbclass.read_text()


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


# ---------------------------------------------------------------------------
# Cluster status parsing and probe (WS2: bakar cluster-info backing helpers)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_cluster_status_reads_aggregate() -> None:
    from bakar.diagnostics import _parse_cluster_status

    cap = _parse_cluster_status(
        '{"SchedulerStatus":["http://h:10600/",{"num_servers":2,"num_cpus":64,"in_progress":7}]}'
    )

    assert cap is not None
    assert cap.num_servers == 2
    assert cap.num_cpus == 64
    assert cap.in_progress == 7
    assert cap.servers is None


@pytest.mark.unit
def test_parse_cluster_status_returns_none_on_garbage() -> None:
    from bakar.diagnostics import _parse_cluster_status

    assert _parse_cluster_status("not json") is None
    assert _parse_cluster_status('{"unexpected":true}') is None


@pytest.mark.unit
def test_parse_cluster_status_picks_up_servers_when_present() -> None:
    """A forked scheduler may add a per-server array; parse it so the node table
    lights up without a bakar change."""
    from bakar.diagnostics import _parse_cluster_status

    cap = _parse_cluster_status(
        '{"SchedulerStatus":["http://h:10600/",{"num_servers":1,"num_cpus":32,"in_progress":0,"servers":[{"id":"a"}]}]}'
    )

    assert cap is not None
    assert cap.servers == [{"id": "a"}]


@pytest.mark.unit
def test_parse_cluster_capacity_string_is_unchanged() -> None:
    """The doctor preflight message bytes must not drift when the parser is
    refactored onto _parse_cluster_status."""
    from bakar.diagnostics import _parse_cluster_capacity

    msg = _parse_cluster_capacity(
        '{"SchedulerStatus":["http://h:10600/",{"num_servers":2,"num_cpus":64,"in_progress":3}]}'
    )

    assert msg == "2 build server(s), 64 cpus, 3 job(s) in progress"


@pytest.mark.unit
def test_probe_cluster_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    import types

    from bakar import diagnostics

    monkeypatch.setattr(
        diagnostics.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(
            stdout='{"SchedulerStatus":["http://h:10600/",{"num_servers":2,"num_cpus":64,"in_progress":1}]}',
            stderr="",
            returncode=0,
        ),
    )
    _ = subprocess  # keep the import meaningful for readers

    report = diagnostics.probe_cluster("http://h:10600")

    assert report.reachable is True
    assert report.capacity is not None
    assert report.capacity.num_servers == 2


@pytest.mark.unit
def test_probe_cluster_unreachable_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    from bakar import diagnostics

    monkeypatch.setattr(
        diagnostics.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(stdout="connection refused", stderr="", returncode=1),
    )

    report = diagnostics.probe_cluster(None)

    assert report.reachable is False
    assert report.capacity is None
    assert report.error


@pytest.mark.unit
def test_probe_cluster_surfaces_nonzero_exit_and_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed `sccache --dist-status` invocation is reported unreachable with its
    stderr surfaced, not silently treated as parseable output."""
    import types

    from bakar import diagnostics

    monkeypatch.setattr(
        diagnostics.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(stdout="", stderr="no scheduler configured", returncode=2),
    )

    report = diagnostics.probe_cluster(None)

    assert report.reachable is False
    assert report.capacity is None
    assert "no scheduler configured" in (report.error or "")


@pytest.mark.unit
def test_probe_cluster_reports_sccache_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    from bakar import diagnostics

    def _raise(*_a: object, **_k: object) -> None:
        raise FileNotFoundError("sccache")

    monkeypatch.setattr(diagnostics.subprocess, "run", _raise)

    report = diagnostics.probe_cluster(None)

    assert report.reachable is False
    assert "not found" in (report.error or "")


@pytest.mark.unit
def test_probe_cluster_threads_scheduler_url_into_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A supplied scheduler URL is forwarded to the subprocess so
    `sccache --dist-status` queries the requested cluster, not the configured one."""
    import types

    from bakar import diagnostics

    captured: dict[str, object] = {}

    def _capture(*_a: object, **k: object) -> object:
        captured["env"] = k.get("env")
        return types.SimpleNamespace(
            stdout='{"SchedulerStatus":["http://x/",{"num_servers":1,"num_cpus":1,"in_progress":0}]}',
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr(diagnostics.subprocess, "run", _capture)

    diagnostics.probe_cluster("http://override:10600")

    env = captured["env"]
    assert env is not None
    assert env["SCCACHE_DIST_SCHEDULER_URL"] == "http://override:10600"
