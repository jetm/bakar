"""Kas tuning overlay resolution for bakar subcommands.

Split out of ``_helpers.py`` (which re-exports everything here so existing
``from bakar.commands._helpers import ...`` call sites keep working). Every
symbol used from outside this module is re-exported at ``_helpers`` -
import from there unless you are inside ``commands/`` and want the direct
path.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer

from bakar.config import _overlay_dir

if TYPE_CHECKING:
    from bakar.bsp_model import BspModel
    from bakar.config import BuildConfig

# ---------------------------------------------------------------------------
# Overlay lookup
# ---------------------------------------------------------------------------


def _overlay_for(bsp: BspModel | None) -> Path:
    """Return the absolute path to the static tuning overlay.

    ``bsp=None`` selects ``bakar-tuning-generic.yml`` - the BSP-agnostic
    overlay used by the ``bakar build my.yml`` flow when the YAML does
    not classify as NXP or TI.
    """
    filename = bsp.tuning_overlay_filename if bsp is not None else "bakar-tuning-generic.yml"
    path = _overlay_dir() / filename
    if not path.is_file():
        raise typer.BadParameter(f"tuning overlay missing: {path}. Reinstall bakar or restore the overlays/ directory.")
    return path


def _conditional_overlay(flag: bool, filename: str) -> list[Path]:
    """Return ``[<overlay-dir>/<filename>]`` when *flag* is True and the file exists, else ``[]``."""
    if not flag:
        return []
    path = _overlay_dir() / filename
    return [path] if path.is_file() else []


def _hashequiv_extra_overlays(cfg: BuildConfig) -> list[Path]:
    """Return the hashequiv overlay path when ``cfg.use_hashequiv`` is True."""
    return _conditional_overlay(cfg.use_hashequiv, "bakar-tuning-hashequiv.yml")


def _shared_cache_extra_overlays(cfg: BuildConfig) -> list[Path]:
    """Return the shared-cache overlay path when ``cfg.use_shared_cache`` is True."""
    return _conditional_overlay(cfg.use_shared_cache, "bakar-tuning-shared-cache.yml")


def _sccache_extra_overlays(cfg: BuildConfig) -> list[Path]:
    """Return the sccache overlay path when ``cfg.use_sccache_dist`` is True."""
    return _conditional_overlay(cfg.use_sccache_dist, "bakar-tuning-sccache.yml")


def _mold_extra_overlays(cfg: BuildConfig) -> list[Path]:
    """Return the mold overlay path when ``cfg.mold`` is on."""
    return _conditional_overlay(cfg.mold, "bakar-tuning-mold.yml")


# Where the yocto-uninative-tarball Arch package installs its bitbake fragment.
# Its presence is the opt-in signal: the package exists only for Arch-family
# hosts, and installing it is a deliberate act, so no separate config toggle.
_UNINATIVE_FRAGMENT = Path("/usr/share/yocto-uninative/uninative.inc")

# Module-level so tests can point host detection at a fixture file.
_UNINATIVE_OS_RELEASE = Path("/etc/os-release")


def _host_is_arch_like() -> bool:
    """Return True when the build host is Arch Linux or an Arch derivative.

    Reads ID and ID_LIKE from /etc/os-release and looks for ``arch`` in either.
    CachyOS reports ``ID=cachyos`` with ``ID_LIKE=arch``, so matching ID_LIKE
    covers the derivatives (CachyOS, EndeavourOS, Manjaro) without enumerating
    them. Returns False when /etc/os-release is absent or unreadable, which is
    the safe direction: the overlay is skipped rather than required on a host
    that cannot have the package installed.
    """
    try:
        text = _UNINATIVE_OS_RELEASE.read_text(encoding="utf-8")
    except OSError:
        return False
    ids: set[str] = set()
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in ("ID", "ID_LIKE"):
            ids.update(value.strip().strip('"').strip("'").split())
    return "arch" in ids


def _uninative_extra_overlays(cfg: BuildConfig) -> list[Path]:
    """Return the uninative overlay path for host-mode builds on Arch-like hosts.

    Points uninative at a tarball built from the host's own glibc commit, so
    oe-core's UNINATIVE_MAXGLIBCVERSION cap (which trails the host libc on a
    rolling distro) can never silently disable uninative and change sstate
    signatures mid-stream.

    Four conditions, all required:

    - ``cfg.uninative`` - explicit opt-in. Without it the selection would depend
      on which packages happen to be installed on the build host, so the same
      config would produce different sstate signatures on two machines. Enabling
      uninative sets NATIVELSBSTRING to "universal", so it must be a decision.
    - ``cfg.host_mode`` - the fragment's UNINATIVE_URL is a ``file://`` URL
      under /usr/share, which does not exist inside the kas-container image;
      requiring it in container mode would fail at parse time.
    - Arch-family host - the only distro the providing package targets.
    - Fragment present - the package is actually installed.
    """
    if not (cfg.uninative and cfg.host_mode and _host_is_arch_like() and _UNINATIVE_FRAGMENT.is_file()):
        return []
    return _conditional_overlay(True, "bakar-tuning-uninative.yml")


def _arch_probe_extra_overlays(cfg: BuildConfig) -> list[Path]:
    """Return the host-probe containment overlay for host-mode builds on Arch-like hosts.

    Arch-family distros ship development headers in the base system that
    Debian-family hosts keep in separate -dev packages. Native recipes running
    their own configure probes (cmake-native's bundled cmcurl calls
    check_library_exists(idn2 ...), which searches the default linker path
    rather than the native sysroot) then link a host library that nothing
    stages into recipe-sysroot-native, producing a binary the uninative loader
    cannot start.

    Shares ``_uninative_extra_overlays``' gate, minus the fragment check, and
    that coupling is causal rather than convenience. A leaked host library is
    only fatal because uninative swapped the program interpreter: the uninative
    loader searches RUNPATH and its own sysroot, never the host's default
    paths, so it refuses a /usr/lib library the host loader would have resolved
    without complaint. Turning uninative on for an Arch host is what turns
    these probe leaks into build failures, so the containment belongs on the
    same switch.

    Three conditions, all required:

    - ``cfg.uninative`` - without it native binaries keep the host loader,
      which finds the leaked library and builds fine. Gating here also keeps
      the tuning stack independent of the machine running the build, so the
      same config cannot yield different sstate signatures on two hosts.
    - ``cfg.host_mode`` - container builds run on a clean image, so no host
      header can leak into a probe in the first place.
    - Arch-family host - a Debian-family host does not ship the headers, so
      applying this would change task signatures for no benefit.
    """
    gate = cfg.uninative and cfg.host_mode and _host_is_arch_like()
    return _conditional_overlay(gate, "bakar-tuning-arch-probes.yml")


def _ccache_extra_overlays(cfg: BuildConfig) -> list[Path]:
    """Return the ccache overlay path whenever ``[build] ccache`` is on.

    Gated on the raw ``cfg.ccache`` toggle, NOT ``cfg.use_ccache`` (which stays
    the parallelism-dominant-launcher marker). ccache and sccache are
    complementary under the hybrid: with sccache-dist on, the ccache overlay is
    co-selected so the non-allowlisted recipe tail still gets a local object
    cache while sccache distributes the allowlisted heavy recipes. Ordered before
    the sccache overlay in ``_tuning_extra_overlays`` (lower ``zz-bakar-NN`` key)
    so ``INHERIT += "ccache"`` lands before ``INHERIT += "sccache"`` and
    sccache.bbclass's per-recipe ``CCACHE`` override wins for allowlisted PNs.
    """
    return _conditional_overlay(cfg.ccache, "bakar-tuning-ccache.yml")


def _host_extra_overlays(cfg: BuildConfig) -> list[Path]:
    """Return the host-mode isolation overlay path when building in host mode.

    Adds the meta-bakar-host layer (rpm bbappend disabling rpm transaction
    plugins) so rpm-native does not dlopen the build host's ABI-incompatible
    /usr/lib/rpm-plugins during do_rootfs. Container builds run on a clean image
    with no host rpm, so this is gated on ``cfg.host_mode``.
    """
    return _conditional_overlay(cfg.host_mode, "bakar-tuning-host.yml")


def _tuning_extra_overlays(cfg: BuildConfig) -> list[Path]:
    """Return all opt-in tuning overlay paths for cfg.

    cache-classify (always on) + host (host-mode rpm isolation) + ccache (when
    effective) + hashequiv + shared-cache + sccache + mold + uninative. The
    cache-classify overlay is unconditional so plain-ccache builds still get the
    cache-hit emitter; every other entry is gated on its toggle. List order does
    not set local.conf precedence - kas sorts local_conf_header by key, and the
    bakar overlays use sort-last ``zz-bakar-NN-*`` keys so the numeric segment
    decides (base < ccache < hashequiv < shared-cache < sccache < mold <
    ``zz-bakar-70-uninative``). The host overlay adds only a layer (no
    local_conf_header), so its position is immaterial. The uninative overlay is
    gated on the host environment rather than a config toggle - see
    ``_uninative_extra_overlays``."""
    return [
        _overlay_dir() / "bakar-tuning-cache-classify.yml",
        *_host_extra_overlays(cfg),
        *_ccache_extra_overlays(cfg),
        *_hashequiv_extra_overlays(cfg),
        *_shared_cache_extra_overlays(cfg),
        *_sccache_extra_overlays(cfg),
        *_mold_extra_overlays(cfg),
        *_uninative_extra_overlays(cfg),
        *_arch_probe_extra_overlays(cfg),
    ]


def _combine_overlays_with_tuning(user_extras: list[Path], cfg: BuildConfig) -> list[Path]:
    """Append cfg's opt-in tuning overlays to user_extras, deduping by resolved path.

    User-supplied colon overlays come first; tuning overlays land last so they win
    in kas merge order (the sccache/hashequiv overlays do ``INHERIT:remove`` after
    the base config's ``INHERIT +=``). Mirrors the BYO combine in ``build.py`` so
    inspection commands (``dump``, ``getvar``) flatten the same overlay set the
    build actually runs.
    """
    combined = list(user_extras)
    seen = {p.resolve() for p in combined}
    for overlay in _tuning_extra_overlays(cfg):
        resolved = overlay.resolve()
        if resolved not in seen:
            combined.append(overlay)
            seen.add(resolved)
    return combined
