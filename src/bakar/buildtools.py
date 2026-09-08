"""buildtools-extended toolchain detection.

Locates a pinned Yocto buildtools-extended toolchain without sourcing it,
from the environment or from the user config. Shared by the host build
path, the qcom setup-environment path, ``bakar setup`` and doctor.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from bakar.user_config import load_user_config

# ---------------------------------------------------------------------------
# buildtools-extended detection (shared by the host build path and doctor)
# ---------------------------------------------------------------------------

# Env var pointing at an installed buildtools-extended-tarball directory (the
# dir holding the ``environment-setup-*`` script). config.py owns no field for
# this yet, so detection is env-driven; a config field can layer on later
# without changing the contract here.
BUILDTOOLS_DIR_ENV = "BAKAR_BUILDTOOLS_DIR"

# Glob for the script Yocto's buildtools-extended installer drops at the
# install root (e.g. ``environment-setup-x86_64-pokysdk-linux``). Sourcing it
# exports OECORE_NATIVE_SYSROOT and prepends the pinned gcc to PATH.
_BUILDTOOLS_ENV_SCRIPT_GLOB = "environment-setup-*"


@dataclass(frozen=True)
class BuildtoolsToolchain:
    """Result of probing for a buildtools-extended toolchain.

    ``present`` is True only when a pinned toolchain is locatable: either the
    process already has it sourced (``OECORE_NATIVE_SYSROOT`` set and its gcc
    on disk), or ``BAKAR_BUILDTOOLS_DIR`` names a dir with an
    ``environment-setup-*`` script. ``env_script`` is the script to source when
    the toolchain is found via the env-var path; it is None when the toolchain
    is already sourced (nothing to source) or absent.
    """

    present: bool
    sysroot: Path | None = None
    env_script: Path | None = None
    detail: str = ""


def resolve_buildtools_dir(install_dir: Path, source: str) -> BuildtoolsToolchain:
    """Probe ``install_dir`` for an ``environment-setup-*`` script.

    ``source`` names where the dir came from (the env var or the config key) so
    the ``detail`` message points the user at the right knob to fix.
    """
    scripts = sorted(install_dir.glob(_BUILDTOOLS_ENV_SCRIPT_GLOB))
    if scripts:
        return BuildtoolsToolchain(
            present=True,
            sysroot=None,
            env_script=scripts[0],
            detail=f"found env script {scripts[0]}",
        )
    return BuildtoolsToolchain(
        present=False,
        detail=f"{source}={install_dir} has no environment-setup-* script",
    )


def detect_buildtools(release_key: str | None = None) -> BuildtoolsToolchain:
    """Locate a pinned buildtools-extended toolchain without sourcing it.

    Detection order:

    1. Already sourced: ``OECORE_NATIVE_SYSROOT`` is set and its ``usr/bin/gcc``
       exists on disk. Nothing needs sourcing; ``env_script`` stays None.
    2. ``BAKAR_BUILDTOOLS_DIR`` names a dir containing an ``environment-setup-*``
       script. ``env_script`` is that script so callers can source it before
       invoking host bitbake. Wins regardless of ``release_key`` - an explicit
       export is always the caller's intent, release-tagging or not.
    3. When ``release_key`` is given: the persisted ``[build.buildtools_dirs]``
       entry for that key, and ONLY that key - no fallback to the untagged
       ``[build] buildtools_dir``. A toolchain built for one Yocto release
       (e.g. scarthgap) must never silently satisfy a build against a
       different one (e.g. wrynose): the two can require different host
       gcc/glibc/python baselines, so an absent release-scoped entry means
       "not present", not "reuse whatever else is configured".
    4. When ``release_key`` is None (callers that don't distinguish release,
       e.g. non-oe-core BSP families): the persisted ``[build] buildtools_dir``
       value, unchanged from before release-scoping existed.

    Returns ``present=False`` when none holds, so the caller can fail loudly
    naming the missing toolchain instead of letting bitbake fall back to the
    system gcc.
    """
    sysroot_env = os.environ.get("OECORE_NATIVE_SYSROOT")
    if sysroot_env:
        sysroot = Path(sysroot_env)
        if (sysroot / "usr" / "bin" / "gcc").exists():
            return BuildtoolsToolchain(
                present=True,
                sysroot=sysroot,
                env_script=None,
                detail=f"already sourced ({sysroot})",
            )

    dir_env = os.environ.get(BUILDTOOLS_DIR_ENV)
    if dir_env:
        return resolve_buildtools_dir(Path(dir_env), BUILDTOOLS_DIR_ENV)

    config = load_user_config()

    if release_key is not None:
        release_dirs = config.buildtools_dirs or {}
        release_dir = release_dirs.get(release_key)
        if release_dir:
            return resolve_buildtools_dir(Path(release_dir), f"[build.buildtools_dirs] {release_key!r}")
        return BuildtoolsToolchain(
            present=False,
            detail=f"no [build.buildtools_dirs] entry for release {release_key!r} and {BUILDTOOLS_DIR_ENV} is unset",
        )

    config_dir = config.buildtools_dir
    if config_dir:
        return resolve_buildtools_dir(Path(config_dir), "[build] buildtools_dir")

    return BuildtoolsToolchain(
        present=False,
        detail=f"neither OECORE_NATIVE_SYSROOT nor {BUILDTOOLS_DIR_ENV} is set "
        "and [build] buildtools_dir is unconfigured",
    )


def resolve_oe_core_release_key(workspace: Path) -> str | None:
    """Derive a release key from the workspace's oe-core Yocto release codename.

    Host-mode builds must not silently reuse a buildtools-extended toolchain
    built for a different Yocto release (e.g. scarthgap vs wrynose) - the two
    releases can pin materially different host gcc/glibc/python baselines. That
    baseline is fixed per Yocto *release*, not per commit, so the key is the
    release codename read from oe-core's authoritative ``LAYERSERIES_CORENAMES``
    in ``meta/conf/layer.conf``. Keying on the codename (rather than the oe-core
    commit hash) is stable across every commit on a release branch - one
    scarthgap toolchain serves all scarthgap commits - and detached-HEAD-safe
    (it reads a tracked file, not a git ref), while still keeping scarthgap and
    wrynose on distinct keys. oe-core declares a single corename per release; if
    several are listed, the first token is used deterministically.

    Returns None when ``meta/conf/layer.conf`` is absent or declares no
    ``LAYERSERIES_CORENAMES`` (a non-oe-core BSP family, or before kas has
    cloned anything yet) - callers treat that as "no release-scoped detection
    available", not an error.
    """
    layer_conf = workspace / "openembedded-core" / "meta" / "conf" / "layer.conf"
    try:
        text = layer_conf.read_text()
    except OSError:
        return None
    var = "LAYERSERIES_CORENAMES"
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(var):
            continue
        rest = stripped[len(var) :].lstrip()
        if not rest or rest[0] not in "?:+=":
            continue  # a different var, e.g. LAYERSERIES_CORENAMES_foo
        value = rest.partition("=")[2].strip().strip("\"'")
        names = value.split()
        if names:
            return names[0]
    return None
