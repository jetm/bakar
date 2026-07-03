"""Unit tests for the decoupled [build] parallelism knobs.

Three optional [build] keys size build parallelism independently of the
single NPROC coupling: ``nproc`` (the NPROC base, auto-detected when unset),
``parallel_make`` (compile -j, exported as BAKAR_PARALLEL_MAKE), and
``bb_number_threads`` (recipe concurrency, exported as
BAKAR_BB_NUMBER_THREADS). All three default to None; when unset, bakar derives
them (topology- and RAM-aware) via bakar.tuning.derive_parallelism.
"""

from __future__ import annotations

import re
import textwrap
from typing import TYPE_CHECKING

import pytest

from bakar.config import BuildConfig
from bakar.steps.kas_build import (
    _build_env,
    _inject_literal_parallelism,
    _resolve_parallelism,
    materialize_overlay,
)
from bakar.user_config import SETTINGS_SCHEMA, load_user_config, set_setting

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

_PARALLELISM_KEYS = ("nproc", "parallel_make", "bb_number_threads")


# ---------------------------------------------------------------------------
# config.toml parsing
# ---------------------------------------------------------------------------


def test_parallelism_keys_valid_positive_ints_populate(tmp_path: Path) -> None:
    toml_content = textwrap.dedent("""\
        [build]
        nproc             = 96
        parallel_make     = 256
        bb_number_threads = 24
    """)
    config_file = tmp_path / "config.toml"
    config_file.write_text(toml_content)

    cfg = load_user_config(config_file)

    assert cfg.nproc == 96
    assert isinstance(cfg.nproc, int) and not isinstance(cfg.nproc, bool)
    assert cfg.parallel_make == 256
    assert isinstance(cfg.parallel_make, int) and not isinstance(cfg.parallel_make, bool)
    assert cfg.bb_number_threads == 24
    assert isinstance(cfg.bb_number_threads, int) and not isinstance(cfg.bb_number_threads, bool)


def test_parallelism_keys_absent_yield_none(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[build]\ndoctor = true\n")

    cfg = load_user_config(config_file)

    assert cfg.nproc is None
    assert cfg.parallel_make is None
    assert cfg.bb_number_threads is None


@pytest.mark.parametrize("field", _PARALLELISM_KEYS)
def test_parallelism_key_zero_raises_naming_field(tmp_path: Path, field: str) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(f"[build]\n{field} = 0\n")

    with pytest.raises(ValueError, match=field):
        load_user_config(config_file)


@pytest.mark.parametrize("field", _PARALLELISM_KEYS)
def test_parallelism_key_negative_raises_naming_field(tmp_path: Path, field: str) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(f"[build]\n{field} = -4\n")

    with pytest.raises(ValueError, match=field):
        load_user_config(config_file)


@pytest.mark.parametrize("field", _PARALLELISM_KEYS)
def test_parallelism_key_bool_raises_with_path(tmp_path: Path, field: str) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(f"[build]\n{field} = true\n")

    with pytest.raises(ValueError, match=re.escape(str(config_file))):
        load_user_config(config_file)


@pytest.mark.parametrize("field", _PARALLELISM_KEYS)
def test_parallelism_key_float_raises_with_path(tmp_path: Path, field: str) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(f"[build]\n{field} = 8.5\n")

    with pytest.raises(ValueError, match=re.escape(str(config_file))):
        load_user_config(config_file)


# ---------------------------------------------------------------------------
# dotted-settings round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", _PARALLELISM_KEYS)
def test_parallelism_keys_present_in_settings_schema(field: str) -> None:
    assert f"build.{field}" in SETTINGS_SCHEMA
    assert SETTINGS_SCHEMA[f"build.{field}"].is_int is True


def test_set_setting_build_nproc_round_trip(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    set_setting("build.nproc", "64", path=config_file)

    cfg = load_user_config(config_file)

    assert cfg.nproc == 64
    assert isinstance(cfg.nproc, int) and not isinstance(cfg.nproc, bool)


def test_set_setting_build_parallel_make_round_trip(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    set_setting("build.parallel_make", "256", path=config_file)

    cfg = load_user_config(config_file)

    assert cfg.parallel_make == 256


def test_set_setting_build_bb_number_threads_round_trip(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    set_setting("build.bb_number_threads", "24", path=config_file)

    cfg = load_user_config(config_file)

    assert cfg.bb_number_threads == 24


@pytest.mark.parametrize("field", _PARALLELISM_KEYS)
def test_set_parallelism_key_non_positive_rejected(tmp_path: Path, field: str) -> None:
    config_file = tmp_path / "config.toml"
    with pytest.raises(ValueError, match=field):
        set_setting(f"build.{field}", "0", path=config_file)
    assert not config_file.exists()


# ---------------------------------------------------------------------------
# Backward compatibility: an old config without the keys loads cleanly
# ---------------------------------------------------------------------------


def test_old_config_without_parallelism_keys_loads(tmp_path: Path) -> None:
    """A pre-existing config that predates the keys loads with them at None."""
    toml_content = textwrap.dedent("""\
        config_version = 3

        [defaults.nxp]
        machine = "imx8mp-var-dart"

        [build]
        kas_container_image = "jetm/kas-build-env:latest"
        scheduler = "completion"
    """)
    config_file = tmp_path / "config.toml"
    config_file.write_text(toml_content)

    cfg = load_user_config(config_file)

    assert cfg.nxp_machine == "imx8mp-var-dart"
    assert cfg.scheduler == "completion"
    assert cfg.nproc is None
    assert cfg.parallel_make is None
    assert cfg.bb_number_threads is None


# ---------------------------------------------------------------------------
# BuildConfig.resolve() carries the values through
# ---------------------------------------------------------------------------


def test_resolve_carries_parallelism_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bakar import config as config_mod

    toml_content = textwrap.dedent("""\
        [build]
        nproc             = 96
        parallel_make     = 256
        bb_number_threads = 24
    """)
    config_file = tmp_path / "config.toml"
    config_file.write_text(toml_content)
    user_config = load_user_config(config_file)

    for var in ("BAKAR_BSP_FAMILY", "BAKAR_MACHINE", "BAKAR_DISTRO", "BAKAR_IMAGE"):
        monkeypatch.delenv(var, raising=False)

    cfg = config_mod.resolve(
        workspace=tmp_path,
        bsp_family="nxp",
        user_config=user_config,
    )

    assert cfg.nproc == 96
    assert cfg.parallel_make == 256
    assert cfg.bb_number_threads == 24


def test_resolve_defaults_parallelism_to_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bakar import config as config_mod

    for var in ("BAKAR_BSP_FAMILY", "BAKAR_MACHINE", "BAKAR_DISTRO", "BAKAR_IMAGE"):
        monkeypatch.delenv(var, raising=False)

    cfg = config_mod.resolve(
        workspace=tmp_path,
        bsp_family="nxp",
        user_config=load_user_config(tmp_path / "missing.toml"),
    )

    assert cfg.nproc is None
    assert cfg.parallel_make is None
    assert cfg.bb_number_threads is None


# ---------------------------------------------------------------------------
# _build_env emission
# ---------------------------------------------------------------------------


def _make_cfg(
    workspace: Path,
    *,
    nproc: int | None = None,
    parallel_make: int | None = None,
    bb_number_threads: int | None = None,
    sccache_dist: bool = False,
    sccache_scheduler_url: str | None = None,
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
        nproc=nproc,
        parallel_make=parallel_make,
        bb_number_threads=bb_number_threads,
        sccache_dist=sccache_dist,
        sccache_scheduler_url=sccache_scheduler_url,
    )


def _clear_parallelism_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NPROC", raising=False)
    monkeypatch.delenv("BAKAR_PARALLEL_MAKE", raising=False)
    monkeypatch.delenv("BAKAR_BB_NUMBER_THREADS", raising=False)


def test_build_env_emits_all_parallelism_vars_when_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_parallelism_env(monkeypatch)
    cfg = _make_cfg(tmp_path, nproc=96, parallel_make=256, bb_number_threads=24)

    env = _build_env(cfg)

    assert env["NPROC"] == "96"
    assert env["BAKAR_PARALLEL_MAKE"] == "256"
    assert env["BAKAR_BB_NUMBER_THREADS"] == "24"


def test_build_env_nproc_falls_back_to_cpu_count_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """NPROC defaults to os.cpu_count(); both BAKAR_* vars are now derived (not absent)."""
    _clear_parallelism_env(monkeypatch)
    monkeypatch.setattr("bakar.steps.kas_build.os.cpu_count", lambda: 12)
    monkeypatch.setattr("bakar.tuning.host_ram_gb", lambda: 96.0)
    cfg = _make_cfg(tmp_path)

    env = _build_env(cfg)

    assert env["NPROC"] == "12"
    # ccache (the cfg default) routes locally, so PARALLEL_MAKE = nproc; threads
    # are RAM-bound to min(12, floor(96/4)) = 12.
    assert env["BAKAR_PARALLEL_MAKE"] == "12"
    assert env["BAKAR_BB_NUMBER_THREADS"] == "12"


def test_build_env_emits_only_set_bakar_vars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """parallel_make set passes through; bb_number_threads unset is now derived."""
    _clear_parallelism_env(monkeypatch)
    monkeypatch.setattr("bakar.steps.kas_build.os.cpu_count", lambda: 12)
    monkeypatch.setattr("bakar.tuning.host_ram_gb", lambda: 96.0)
    cfg = _make_cfg(tmp_path, parallel_make=128)

    env = _build_env(cfg)

    assert env["NPROC"] == "12"
    assert env["BAKAR_PARALLEL_MAKE"] == "128"
    assert env["BAKAR_BB_NUMBER_THREADS"] == "12"


def test_build_env_env_nproc_beats_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-empty live NPROC env var wins over cfg.nproc."""
    monkeypatch.delenv("BAKAR_PARALLEL_MAKE", raising=False)
    monkeypatch.delenv("BAKAR_BB_NUMBER_THREADS", raising=False)
    monkeypatch.setenv("NPROC", "7")
    cfg = _make_cfg(tmp_path, nproc=96)

    env = _build_env(cfg)

    assert env["NPROC"] == "7"


def test_build_env_empty_nproc_treated_as_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An exported-but-empty NPROC ("") is treated as unset and falls through to
    cfg.nproc, so the overlay never expands BB_NUMBER_THREADS to "" or PARALLEL_MAKE
    to "-j ". This matches check_nproc's truthiness semantics, so the doctor and the
    build agree."""
    monkeypatch.delenv("BAKAR_PARALLEL_MAKE", raising=False)
    monkeypatch.delenv("BAKAR_BB_NUMBER_THREADS", raising=False)
    monkeypatch.setenv("NPROC", "")
    cfg = _make_cfg(tmp_path, nproc=8)

    env = _build_env(cfg)

    assert env["NPROC"] == "8"


def test_build_env_empty_nproc_falls_back_to_cpu_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty NPROC with no cfg.nproc falls all the way through to os.cpu_count()."""
    monkeypatch.delenv("BAKAR_PARALLEL_MAKE", raising=False)
    monkeypatch.delenv("BAKAR_BB_NUMBER_THREADS", raising=False)
    monkeypatch.setenv("NPROC", "")
    monkeypatch.setattr("bakar.steps.kas_build.os.cpu_count", lambda: 12)
    cfg = _make_cfg(tmp_path)

    env = _build_env(cfg)

    assert env["NPROC"] == "12"


def test_build_env_and_check_nproc_agree_on_empty_nproc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """check_nproc and _build_env resolve the same NPROC base for an empty env var."""
    from bakar.diagnostics import check_nproc

    monkeypatch.delenv("BAKAR_PARALLEL_MAKE", raising=False)
    monkeypatch.delenv("BAKAR_BB_NUMBER_THREADS", raising=False)
    monkeypatch.setenv("NPROC", "")
    cfg = _make_cfg(tmp_path, nproc=8)

    env = _build_env(cfg)
    result = check_nproc(cfg)

    assert env["NPROC"] == "8"
    assert "NPROC=8 (from config.toml)" in result.message


# ---------------------------------------------------------------------------
# Literal parallelism injection into the materialized overlay
# ---------------------------------------------------------------------------

_OVERLAY_PARALLELISM = textwrap.dedent(
    """\
    local_conf_header:
      bakar-tuning: |
        CCACHE_DIR = "/work/ccache"
        BB_NUMBER_THREADS = "${@os.environ.get('BAKAR_BB_NUMBER_THREADS') or os.environ.get('NPROC', '16')}"
        PARALLEL_MAKE = "-j ${@os.environ.get('BAKAR_PARALLEL_MAKE') or os.environ.get('NPROC', '16')}"
        BB_NUMBER_PARSE_THREADS = "${@os.environ.get('BAKAR_BB_NUMBER_THREADS') or os.environ.get('NPROC', '16')}"
    """
)


def test_resolve_parallelism_uses_cfg_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_parallelism_env(monkeypatch)
    cfg = _make_cfg(tmp_path, nproc=48, parallel_make=64, bb_number_threads=16)

    assert _resolve_parallelism(cfg) == (64, 16)


def test_resolve_parallelism_falls_back_to_nproc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset + no dist launcher: PARALLEL_MAKE = nproc base, BB_NUMBER_THREADS =
    min(nproc, ram/4GB). RAM is pinned so the cap is host-independent."""
    _clear_parallelism_env(monkeypatch)
    monkeypatch.setattr("bakar.tuning.host_ram_gb", lambda: 96.0)
    cfg = _make_cfg(tmp_path, nproc=20)

    assert _resolve_parallelism(cfg) == (20, 20)


def test_resolve_parallelism_falls_back_to_cpu_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_parallelism_env(monkeypatch)
    monkeypatch.setattr("bakar.tuning.host_ram_gb", lambda: 96.0)
    monkeypatch.setattr("bakar.steps.kas_build.os.cpu_count", lambda: 12)
    cfg = _make_cfg(tmp_path)

    assert _resolve_parallelism(cfg) == (12, 12)


def test_resolve_parallelism_live_nproc_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-empty numeric live NPROC env wins over cfg.nproc, matching _build_env."""
    monkeypatch.delenv("BAKAR_PARALLEL_MAKE", raising=False)
    monkeypatch.delenv("BAKAR_BB_NUMBER_THREADS", raising=False)
    monkeypatch.setattr("bakar.tuning.host_ram_gb", lambda: 96.0)
    monkeypatch.setenv("NPROC", "7")
    cfg = _make_cfg(tmp_path, nproc=96)

    assert _resolve_parallelism(cfg) == (7, 7)


def test_resolve_parallelism_bb_threads_capped_by_low_ram(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BB_NUMBER_THREADS is RAM-bound: on a low-RAM host it caps below nproc."""
    _clear_parallelism_env(monkeypatch)
    monkeypatch.setattr("bakar.tuning.host_ram_gb", lambda: 32.0)
    cfg = _make_cfg(tmp_path, nproc=32)

    # PARALLEL_MAKE = nproc (no dist); BB_NUMBER_THREADS = min(32, floor(32/2.5)) = 12.
    assert _resolve_parallelism(cfg) == (32, 12)


def test_resolve_parallelism_sccache_dist_uses_cluster_cpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under sccache-dist with the knobs unset, PARALLEL_MAKE is sized to the live
    cluster cpu count (the container literal feeds the whole cluster), not nproc."""
    import types

    from bakar import diagnostics
    from bakar.steps import kas_build

    _clear_parallelism_env(monkeypatch)
    monkeypatch.setattr("bakar.tuning.host_ram_gb", lambda: 96.0)
    monkeypatch.setattr(
        kas_build,
        "probe_cluster",
        lambda url: types.SimpleNamespace(
            reachable=True,
            capacity=diagnostics.ClusterCapacity(num_servers=2, num_cpus=64, in_progress=0),
            error=None,
        ),
    )
    cfg = _make_cfg(tmp_path, nproc=32, sccache_dist=True, sccache_scheduler_url="http://localhost:10600")

    # PARALLEL_MAKE = cluster 64; compile offloaded, so BB_NUMBER_THREADS =
    # floor(96/0.95) = 101 (offloaded divisor, nproc cap dropped).
    assert _resolve_parallelism(cfg) == (64, 101)


def test_inject_literal_parallelism_bakes_cluster_sized_pm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The materialized container overlay bakes the cluster-sized -j and the
    RAM-bound BB_NUMBER_THREADS when the knobs are unset under sccache-dist - the
    container path that the BAKAR_* env var cannot reach (kas scrubs it)."""
    import types

    from bakar import diagnostics
    from bakar.steps import kas_build

    _clear_parallelism_env(monkeypatch)
    monkeypatch.setattr("bakar.tuning.host_ram_gb", lambda: 96.0)
    monkeypatch.setattr(
        kas_build,
        "probe_cluster",
        lambda url: types.SimpleNamespace(
            reachable=True,
            capacity=diagnostics.ClusterCapacity(num_servers=2, num_cpus=64, in_progress=0),
            error=None,
        ),
    )
    cfg = _make_cfg(tmp_path, nproc=32, sccache_dist=True, sccache_scheduler_url="http://localhost:10600")

    out = _inject_literal_parallelism(cfg, _OVERLAY_PARALLELISM)

    assert 'PARALLEL_MAKE = "-j 64"' in out
    assert 'BB_NUMBER_THREADS = "101"' in out
    assert "os.environ.get" not in out


def test_inject_literal_parallelism_substitutes_resolved_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_parallelism_env(monkeypatch)
    cfg = _make_cfg(tmp_path, nproc=48, parallel_make=64, bb_number_threads=16)

    out = _inject_literal_parallelism(cfg, _OVERLAY_PARALLELISM)

    assert 'PARALLEL_MAKE = "-j 64"' in out
    assert 'BB_NUMBER_THREADS = "16"' in out
    assert 'BB_NUMBER_PARSE_THREADS = "16"' in out
    # No env lookup remains: the materialized value cannot be scrubbed by
    # bitbake's clean_environment.
    assert "os.environ.get" not in out


def test_inject_literal_parallelism_leaves_unrelated_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_parallelism_env(monkeypatch)
    cfg = _make_cfg(tmp_path, parallel_make=64, bb_number_threads=16)

    out = _inject_literal_parallelism(cfg, _OVERLAY_PARALLELISM)

    assert 'CCACHE_DIR = "/work/ccache"' in out


def test_materialize_overlay_writes_literal_parallelism(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The materialized bakar-tuning overlay carries a literal -j N, not the
    os.environ.get expression that bitbake's clean_environment can scrub."""
    _clear_parallelism_env(monkeypatch)
    src = tmp_path / "bakar-tuning-generic.yml"
    src.write_text(_OVERLAY_PARALLELISM, encoding="utf-8")
    cfg = _make_cfg(tmp_path, parallel_make=64, bb_number_threads=16)

    rel = materialize_overlay(cfg, src)
    written = (cfg.bsp_root / rel).read_text(encoding="utf-8")

    assert 'PARALLEL_MAKE = "-j 64"' in written
    assert "os.environ.get" not in written


def test_materialize_overlay_skips_non_tuning_overlays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A user extra overlay that happens to use os.environ.get is copied verbatim."""
    _clear_parallelism_env(monkeypatch)
    src = tmp_path / "my-extra.yml"
    src.write_text(_OVERLAY_PARALLELISM, encoding="utf-8")
    cfg = _make_cfg(tmp_path, parallel_make=64)

    rel = materialize_overlay(cfg, src)
    written = (cfg.bsp_root / rel).read_text(encoding="utf-8")

    assert "os.environ.get('BAKAR_PARALLEL_MAKE')" in written
