"""Sanity checks on the static tuning overlay YAMLs.

The overlays carry every optimization that used to live in the
generator's ``local_conf_header`` block. These tests parse the shipped
files and assert each load-bearing line is present, so a regression
that drops e.g. the renderdoc fix or the BB_FETCH_TIMEOUT bump fails
in CI before it lands in a build.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_OVERLAY_DIR = Path(str(importlib.resources.files("bakar") / "overlays"))
NXP_OVERLAY = _OVERLAY_DIR / "bakar-tuning-nxp.yml"
TI_OVERLAY = _OVERLAY_DIR / "bakar-tuning-ti.yml"
GENERIC_OVERLAY = _OVERLAY_DIR / "bakar-tuning-generic.yml"
HASHEQUIV_OVERLAY = _OVERLAY_DIR / "bakar-tuning-hashequiv.yml"

_SHARED_LINES = (
    "BB_NUMBER_THREADS",
    "PARALLEL_MAKE",
    "IMAGE_FEATURES:remove",
    'BB_FETCH_TIMEOUT = "600"',
    "MIRRORS = ",
    "PREMIRRORS:prepend = ",
)

# Tokens added by the performance-optimizations change; present in all three
# BSP-specific overlays (NXP, TI, generic).
_TUNING_PERF_LINES = (
    "BB_NUMBER_PARSE_THREADS",
    "BB_DISKMON_DIRS",
    "BB_PRESSURE_MAX_CPU",
    "BB_PRESSURE_MAX_IO",
    "BB_PRESSURE_MAX_MEMORY",
    "BB_SCHEDULER",
)

_NXP_ONLY_LINES = (
    'ACCEPT_FSL_EULA = "1"',
    'CMAKE_CXX_COMPILER_LAUNCHER:pn-renderdoc = ""',
    'CMAKE_C_COMPILER_LAUNCHER:pn-renderdoc = ""',
    "varigit/linux-imx",
    "/work/forks/linux-imx",
)


def _load(path: Path) -> dict:
    assert path.is_file(), f"overlay missing: {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture
def nxp_overlay() -> dict:
    return _load(NXP_OVERLAY)


@pytest.fixture
def ti_overlay() -> dict:
    return _load(TI_OVERLAY)


@pytest.fixture
def generic_overlay() -> dict:
    return _load(GENERIC_OVERLAY)


def test_nxp_overlay_has_kas_header(nxp_overlay: dict) -> None:
    assert nxp_overlay.get("header") == {"version": 21}


def test_ti_overlay_has_kas_header(ti_overlay: dict) -> None:
    assert ti_overlay.get("header") == {"version": 21}


def test_nxp_overlay_carries_shared_tuning(nxp_overlay: dict) -> None:
    body = nxp_overlay["local_conf_header"]["zz-bakar-10-base"]
    for needle in _SHARED_LINES:
        assert needle in body, f"NXP overlay missing: {needle!r}"


def test_nxp_overlay_carries_nxp_only_tuning(nxp_overlay: dict) -> None:
    body = nxp_overlay["local_conf_header"]["zz-bakar-10-base"]
    for needle in _NXP_ONLY_LINES:
        assert needle in body, f"NXP overlay missing: {needle!r}"


def test_ti_overlay_carries_shared_tuning(ti_overlay: dict) -> None:
    body = ti_overlay["local_conf_header"]["zz-bakar-10-base"]
    for needle in _SHARED_LINES:
        assert needle in body, f"TI overlay missing: {needle!r}"


def test_ti_overlay_omits_nxp_specific_knobs(ti_overlay: dict) -> None:
    """ACCEPT_FSL_EULA and renderdoc are NXP-specific."""
    body = ti_overlay["local_conf_header"]["zz-bakar-10-base"]
    assert "ACCEPT_FSL_EULA" not in body
    assert "renderdoc" not in body


def test_ti_overlay_carries_ti_fork_premirrors(ti_overlay: dict) -> None:
    body = ti_overlay["local_conf_header"]["zz-bakar-10-base"]
    assert "/work/forks/ti-linux-kernel" in body
    assert "/work/forks/ti-u-boot" in body


def test_nxp_overlay_carries_meta_varis_overrides(nxp_overlay: dict) -> None:
    """The override layer ships with the NXP overlay, not the generator output."""
    repos = nxp_overlay.get("repos") or {}
    assert "meta-varis-overrides" in repos
    assert repos["meta-varis-overrides"]["path"] == "meta-varis-overrides"


def test_ti_overlay_carries_meta_varis_overrides_ti(ti_overlay: dict) -> None:
    repos = ti_overlay.get("repos") or {}
    assert "meta-varis-overrides-ti" in repos
    assert repos["meta-varis-overrides-ti"]["path"] == "meta-varis-overrides-ti"


def test_generic_overlay_has_kas_header(generic_overlay: dict) -> None:
    assert generic_overlay.get("header") == {"version": 21}


def test_generic_overlay_carries_shared_tuning(generic_overlay: dict) -> None:
    body = generic_overlay["local_conf_header"]["zz-bakar-10-base"]
    for needle in _SHARED_LINES:
        assert needle in body, f"generic overlay missing: {needle!r}"


def test_generic_overlay_omits_nxp_specific_knobs(generic_overlay: dict) -> None:
    """The generic overlay must not pull in NXP-only knobs."""
    body = generic_overlay["local_conf_header"]["zz-bakar-10-base"]
    assert "ACCEPT_FSL_EULA" not in body
    assert "renderdoc" not in body
    assert "linux-imx" not in body


def test_generic_overlay_omits_ti_specific_knobs(generic_overlay: dict) -> None:
    body = generic_overlay["local_conf_header"]["zz-bakar-10-base"]
    assert "ti-linux-kernel" not in body
    assert "ti-u-boot" not in body


def test_generic_overlay_omits_meta_varis_overrides(generic_overlay: dict) -> None:
    """The vendor carry layer is irrelevant for non-NXP/TI builds."""
    repos = generic_overlay.get("repos") or {}
    assert "meta-varis-overrides" not in repos
    assert "meta-varis-overrides-ti" not in repos


def test_generic_overlay_declares_pythonmalloc_env(generic_overlay: dict) -> None:
    """PYTHONMALLOC=malloc is BSP-agnostic; the parser fork race fires on every BSP."""
    assert generic_overlay.get("env") == {
        "PYTHONMALLOC": "malloc",
        "BB_DEFAULT_EVENTLOG": None,
        "SDKMACHINE": None,
        "BAKAR_PARALLEL_MAKE": None,
        "BAKAR_BB_NUMBER_THREADS": None,
        "PATH": None,
    }


def test_all_overlays_whitelist_bb_default_eventlog(nxp_overlay: dict, ti_overlay: dict, generic_overlay: dict) -> None:
    """Every BSP overlay must declare BB_DEFAULT_EVENTLOG (null = passthrough-only).

    bakar injects the per-run event-log path via ``docker -e``, but bitbake
    reads BB_DEFAULT_EVENTLOG from its datastore and clean_environment scrubs
    env vars not in BB_ENV_PASSTHROUGH_ADDITIONS. kas only whitelists vars
    declared in the ``env:`` section, so dropping this key silently kills the
    live UI's event feed (setscene line, cache note, failure alerts) and the
    build degrades to the knotty regex fallback.
    """
    for name, overlay in (("nxp", nxp_overlay), ("ti", ti_overlay), ("generic", generic_overlay)):
        env = overlay.get("env") or {}
        assert "BB_DEFAULT_EVENTLOG" in env, f"{name} overlay missing BB_DEFAULT_EVENTLOG env declaration"
        assert env["BB_DEFAULT_EVENTLOG"] is None, f"{name} overlay must declare BB_DEFAULT_EVENTLOG as null"


def test_all_overlays_whitelist_sdkmachine(nxp_overlay: dict, ti_overlay: dict, generic_overlay: dict) -> None:
    """Every BSP overlay must whitelist SDKMACHINE (null = passthrough-only).

    ``bakar build --target avocado-complete`` (and any SDK target) needs the host
    SDKMACHINE forwarded so bitbake picks the SDK arch. _build_env carries it into
    the kas-container process, but bitbake's clean_environment scrubs vars not in
    BB_ENV_PASSTHROUGH_ADDITIONS; declaring it null here makes kas whitelist it.
    """
    for name, overlay in (("nxp", nxp_overlay), ("ti", ti_overlay), ("generic", generic_overlay)):
        env = overlay.get("env") or {}
        assert "SDKMACHINE" in env, f"{name} overlay missing SDKMACHINE env declaration"
        assert env["SDKMACHINE"] is None, f"{name} overlay must declare SDKMACHINE as null"


def test_all_overlays_whitelist_path(nxp_overlay: dict, ti_overlay: dict, generic_overlay: dict) -> None:
    """Every BSP overlay must declare PATH (null = passthrough-only).

    Declaring PATH in the env: block makes kas set the bitbake-launch PATH to
    the live os.environ['PATH'] of the kas subprocess (config.py get_environment
    prefers os.environ over the YAML default), so bakar's host-mode launch PATH
    (py_bin : bitbake_bin : buildtools_bindir : inherited) reaches the bitbake
    process and OE's HOSTTOOLS resolves against the pinned buildtools gcc.
    """
    for name, overlay in (("nxp", nxp_overlay), ("ti", ti_overlay), ("generic", generic_overlay)):
        env = overlay.get("env") or {}
        assert "PATH" in env, f"{name} overlay missing PATH env declaration"
        assert env["PATH"] is None, f"{name} overlay must declare PATH as null"


@pytest.fixture
def hashequiv_overlay() -> dict:
    return _load(HASHEQUIV_OVERLAY)


def test_nxp_overlay_carries_perf_tuning(nxp_overlay: dict) -> None:
    body = nxp_overlay["local_conf_header"]["zz-bakar-10-base"]
    for needle in _TUNING_PERF_LINES:
        assert needle in body, f"NXP overlay missing: {needle!r}"


def test_ti_overlay_carries_perf_tuning(ti_overlay: dict) -> None:
    body = ti_overlay["local_conf_header"]["zz-bakar-10-base"]
    for needle in _TUNING_PERF_LINES:
        assert needle in body, f"TI overlay missing: {needle!r}"


def test_generic_overlay_carries_perf_tuning(generic_overlay: dict) -> None:
    body = generic_overlay["local_conf_header"]["zz-bakar-10-base"]
    for needle in _TUNING_PERF_LINES:
        assert needle in body, f"generic overlay missing: {needle!r}"


def test_all_overlays_decouple_parallelism(nxp_overlay: dict, ti_overlay: dict, generic_overlay: dict) -> None:
    """Every BSP overlay must read the decoupled BAKAR_* parallelism vars and
    whitelist them in its env: block.

    The three overlays (nxp, ti, generic) are mutually exclusive, so the
    decoupling only takes effect on a given build if THAT overlay carries it.
    Reading BAKAR_PARALLEL_MAKE / BAKAR_BB_NUMBER_THREADS in local_conf_header
    is inert in container mode unless the same vars are declared null in env:,
    because bitbake's clean_environment scrubs vars absent from
    BB_ENV_PASSTHROUGH_ADDITIONS - kas only whitelists env: keys.
    """
    for name, overlay in (("nxp", nxp_overlay), ("ti", ti_overlay), ("generic", generic_overlay)):
        body = overlay["local_conf_header"]["zz-bakar-10-base"]
        assert "BAKAR_PARALLEL_MAKE" in body, f"{name} overlay does not read BAKAR_PARALLEL_MAKE"
        assert "BAKAR_BB_NUMBER_THREADS" in body, f"{name} overlay does not read BAKAR_BB_NUMBER_THREADS"
        env = overlay.get("env") or {}
        assert "BAKAR_PARALLEL_MAKE" in env, f"{name} overlay missing BAKAR_PARALLEL_MAKE env declaration"
        assert env["BAKAR_PARALLEL_MAKE"] is None, f"{name} overlay must declare BAKAR_PARALLEL_MAKE as null"
        assert "BAKAR_BB_NUMBER_THREADS" in env, f"{name} overlay missing BAKAR_BB_NUMBER_THREADS env declaration"
        assert env["BAKAR_BB_NUMBER_THREADS"] is None, f"{name} overlay must declare BAKAR_BB_NUMBER_THREADS as null"


def test_generic_overlay_carries_nice_ionice(generic_overlay: dict) -> None:
    body = generic_overlay["local_conf_header"]["zz-bakar-10-base"]
    assert "BB_TASK_NICE_LEVEL" in body
    assert "BB_TASK_IONICE_LEVEL" in body


def test_hashequiv_overlay_has_kas_header(hashequiv_overlay: dict) -> None:
    assert hashequiv_overlay.get("header") == {"version": 21}


def test_hashequiv_overlay_sets_signature_handler(hashequiv_overlay: dict) -> None:
    body = hashequiv_overlay["local_conf_header"]["zz-bakar-30-hashequiv"]
    assert 'BB_SIGNATURE_HANDLER = "OEEquivHash"' in body
    assert "BB_HASHSERVE" in body
    assert "BB_HASHSERVE_UPSTREAM" in body


def test_hashequiv_overlay_bb_hashserve_reads_from_env(hashequiv_overlay: dict) -> None:
    """BB_HASHSERVE must resolve from the BB_HASHSERVE env var, falling back to 'auto'.

    The bakar-managed per-workspace hashserv daemon injects its own
    BB_HASHSERVE into the build environment. A hardcoded ``"auto"`` would
    defeat that injection and start a fresh ephemeral daemon per build.
    """
    body = hashequiv_overlay["local_conf_header"]["zz-bakar-30-hashequiv"]
    assert "BB_HASHSERVE = \"${@os.environ.get('BB_HASHSERVE', 'auto')}\"" in body
    for line in body.splitlines():
        assert line.strip() != 'BB_HASHSERVE = "auto"', (
            "BB_HASHSERVE must not be hardcoded to 'auto'; it must read from the BB_HASHSERVE env var"
        )


def test_hashequiv_overlay_colocates_db_with_sstate(hashequiv_overlay: dict) -> None:
    """BB_HASHSERVE_DB_DIR points at SSTATE_DIR so builds sharing the sstate cache
    share equivalence mappings instead of each keeping a private build-local DB.

    Without it OE warns that the hash-equivalence DB lives inside the build dir
    while SSTATE_DIR is shared, defeating the cross-build/workspace reuse this
    overlay advertises. Falsifier: drop the line and the assertion fails.
    """
    body = hashequiv_overlay["local_conf_header"]["zz-bakar-30-hashequiv"]
    assert 'BB_HASHSERVE_DB_DIR = "${SSTATE_DIR}"' in body


def test_base_overlays_no_longer_inherit_ccache(nxp_overlay: dict, ti_overlay: dict, generic_overlay: dict) -> None:
    """ccache wiring moved to the conditional bakar-tuning-ccache overlay."""
    for name, overlay in (("nxp", nxp_overlay), ("ti", ti_overlay), ("generic", generic_overlay)):
        body = overlay["local_conf_header"]["zz-bakar-10-base"]
        assert 'INHERIT += "ccache"' not in body, f"{name} base still inherits ccache"
        assert "CCACHE_DIR" not in body, f"{name} base still sets CCACHE_DIR"


def test_generic_overlay_omits_fetchcmd_wget(generic_overlay: dict) -> None:
    """FETCHCMD_wget left the generic overlay; the workspace's own fetch config owns it."""
    body = generic_overlay["local_conf_header"]["zz-bakar-10-base"]
    assert "FETCHCMD_wget" not in body


def test_generic_overlay_limits_binary_locales(generic_overlay: dict) -> None:
    """The generic overlay caps glibc binary-locale generation to en_US.UTF-8 (?= overridable)."""
    body = generic_overlay["local_conf_header"]["zz-bakar-10-base"]
    assert 'GLIBC_GENERATE_LOCALES ?= "en_US.UTF-8"' in body


def test_nxp_ti_overlays_keep_fetchcmd_wget(nxp_overlay: dict, ti_overlay: dict) -> None:
    """NXP/TI keep their crates.io FETCHCMD_wget workaround (vendor BSPs, no avocado fetch overlay)."""
    for name, overlay in (("nxp", nxp_overlay), ("ti", ti_overlay)):
        body = overlay["local_conf_header"]["zz-bakar-10-base"]
        assert "FETCHCMD_wget" in body, f"{name} overlay dropped FETCHCMD_wget"


def test_base_overlays_strip_rm_work(nxp_overlay: dict, ti_overlay: dict, generic_overlay: dict) -> None:
    """While bakar is in use, rm_work is stripped from both inherit paths."""
    for name, overlay in (("nxp", nxp_overlay), ("ti", ti_overlay), ("generic", generic_overlay)):
        body = overlay["local_conf_header"]["zz-bakar-10-base"]
        assert 'INHERIT:remove = "rm_work"' in body, f"{name} base missing INHERIT:remove rm_work"
        assert 'USER_CLASSES:remove = "rm_work"' in body, f"{name} base missing USER_CLASSES:remove rm_work"


def test_base_overlays_hashserve_upstream_plain_not_forcevariable(
    nxp_overlay: dict, ti_overlay: dict, generic_overlay: dict
) -> None:
    """forcevariable dropped: the sort-last key beats BSP YAMLs and lets opt-in overlays override."""
    for name, overlay in (("nxp", nxp_overlay), ("ti", ti_overlay), ("generic", generic_overlay)):
        body = overlay["local_conf_header"]["zz-bakar-10-base"]
        assert 'BB_HASHSERVE_UPSTREAM = ""' in body, f"{name} base missing plain BB_HASHSERVE_UPSTREAM"
        assert "BB_HASHSERVE_UPSTREAM:forcevariable" not in body, f"{name} base still uses forcevariable"


def test_ccache_overlay_carries_ccache_block() -> None:
    """The conditional ccache overlay carries the ccache wiring under its sort-last key."""
    overlay = _load(_OVERLAY_DIR / "bakar-tuning-ccache.yml")
    body = overlay["local_conf_header"]["zz-bakar-20-ccache"]
    assert 'INHERIT += "ccache"' in body
    assert 'CCACHE_DIR = "${TOPDIR}/ccache"' in body
    assert "CCACHE_DISABLE:pn-nodejs" in body


def test_all_bakar_overlay_keys_sort_last() -> None:
    """Every bakar local_conf_header key uses the zz-bakar- prefix so it sorts after workspace keys.

    kas emits local_conf_header sorted by key (kas/config.py _get_conf_header),
    so a key that does not sort last lets a workspace layer's plain `=` win.
    """
    import glob

    for path in sorted(glob.glob(str(_OVERLAY_DIR / "bakar-tuning-*.yml"))):
        overlay = _load(Path(path))
        for key in overlay.get("local_conf_header") or {}:
            assert key.startswith("zz-bakar-"), f"{path}: local_conf_header key {key!r} does not sort last"
