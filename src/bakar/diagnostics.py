"""Pre-flight diagnosis checks.

Each check is a callable returning a :class:`CheckResult`. Checks are
grouped by severity:

* ``BLOCK`` - halt `bakar build` before it spawns anything expensive
* ``WARN``  - print a warning and continue
* ``INFO``  - purely informational, never stops or warns the user

The check list is now BSP-aware: ``SHARED_CHECKS`` runs for every BSP,
and the dispatched :class:`~bakar.bsp_model.BspModel.doctor_extras`
adds the family-specific gates (``check_forks_linux_imx`` and friends
for NXP; the four ``check_ti_*`` functions for TI). Both ``bakar
doctor`` and the pre-flight gate inside ``bakar build`` consume the
same assembled list via :func:`run_all`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import tomllib
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from bakar import build_stop
from bakar.config import BuildConfig
from bakar.kas import parse_bblayers
from bakar.setup.profile import _read_sysctl
from bakar.user_config import load_user_config

if TYPE_CHECKING:
    from bakar.bsp_model import BspModel


class Severity(StrEnum):
    BLOCK = "BLOCK"
    WARN = "WARN"
    INFO = "INFO"


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class CheckResult:
    name: str
    severity: Severity
    status: Status
    message: str
    fix_hint: str | None = None


def _ok(name: str, severity: Severity, message: str) -> CheckResult:
    return CheckResult(name=name, severity=severity, status=Status.PASS, message=message)


def _fail(name: str, severity: Severity, message: str, fix_hint: str | None = None) -> CheckResult:
    return CheckResult(name=name, severity=severity, status=Status.FAIL, message=message, fix_hint=fix_hint)


def _skip(name: str, severity: Severity, message: str) -> CheckResult:
    return CheckResult(name=name, severity=severity, status=Status.SKIP, message=message)


def split_host_port(endpoint: str, default_port: int) -> tuple[str, int]:
    """Split a ``host:port`` endpoint; fall back to ``default_port`` when no numeric port.

    Shared by the cluster preflight checks and ``bakar monitor`` so a bare host
    like ``10.42.0.1`` probes the service's default port rather than failing to
    parse.
    """
    host, sep, port = endpoint.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return endpoint, default_port


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


def detect_buildtools() -> BuildtoolsToolchain:
    """Locate a pinned buildtools-extended toolchain without sourcing it.

    Detection order:

    1. Already sourced: ``OECORE_NATIVE_SYSROOT`` is set and its ``usr/bin/gcc``
       exists on disk. Nothing needs sourcing; ``env_script`` stays None.
    2. ``BAKAR_BUILDTOOLS_DIR`` names a dir containing an ``environment-setup-*``
       script. ``env_script`` is that script so callers can source it before
       invoking host bitbake.
    3. The persisted ``[build] buildtools_dir`` user-config value (the location
       ``bakar setup`` records), used only when the env var is unset so an
       explicit export still wins.

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

    config_dir = load_user_config().buildtools_dir
    if config_dir:
        return resolve_buildtools_dir(Path(config_dir), "[build] buildtools_dir")

    return BuildtoolsToolchain(
        present=False,
        detail=f"neither OECORE_NATIVE_SYSROOT nor {BUILDTOOLS_DIR_ENV} is set "
        "and [build] buildtools_dir is unconfigured",
    )


# ---------------------------------------------------------------------------
# Shared checks (run for every BSP family)
# ---------------------------------------------------------------------------


# Per-family host-tool requirement tuples. Mirrored on
# ``BspModel.required_host_tools``; kept here so ``check_host_tools`` can
# stay a pure ``(cfg) -> CheckResult`` callable without the import gymnastics
# a BspModel-typed argument would entail.
_REQUIRED_TOOLS_BY_FAMILY: dict[str, tuple[str, ...]] = {
    "nxp": ("repo", "kas-container", "docker", "python3"),
    "ti": ("git", "kas-container", "docker", "python3"),
    # Generic mode does not run repo-tool or oe-layertool-setup.sh; kas
    # itself does any cloning the YAML asks for.
    "generic": ("kas-container", "docker", "python3"),
    # bitbake-setup workspaces are initialized externally; bakar only
    # translates their config to a kas YAML and runs kas-container. Same
    # toolset as generic - no repo/oe-layertool tools.
    "bbsetup": ("kas-container", "docker", "python3"),
}


# Extra HOSTTOOLS the avocado distro declares (gfortran in meta-avocado
# kas/base.yml, git-lfs in kas/extra/atecc.yml). The kas container image ships
# them, so they only need checking on the host PATH in host mode.
_AVOCADO_HOST_TOOLS: tuple[str, ...] = ("gfortran", "git-lfs")


def check_host_tools(cfg: BuildConfig) -> CheckResult:
    base = _REQUIRED_TOOLS_BY_FAMILY.get(
        cfg.bsp_family,
        _REQUIRED_TOOLS_BY_FAMILY["nxp"],
    )
    if cfg.host_mode:
        # Host-mode builds run plain `kas` directly; the container runtime
        # is not exercised so drop `docker` and substitute `kas` for
        # `kas-container` in the per-family canonical list.
        required = tuple("kas" if t == "kas-container" else t for t in base if t != "docker")
        # Avocado declares extra HOSTTOOLS the kas image normally provides; a
        # host-mode build needs them on the host PATH or bitbake aborts at parse
        # ("required tools ... unavailable in PATH").
        if cfg.is_meta_avocado:
            required += _AVOCADO_HOST_TOOLS
    else:
        required = base
    missing = [t for t in required if shutil.which(t) is None]
    if missing:
        return _fail(
            "host-tools",
            Severity.BLOCK,
            f"missing on PATH: {', '.join(missing)}",
            fix_hint="Install with your package manager or `uv tool install kas`.",
        )
    return _ok(
        "host-tools",
        Severity.BLOCK,
        f"{cfg.bsp_family.upper()} required binaries present ({', '.join(required)})",
    )


def check_docker_daemon(cfg: BuildConfig) -> CheckResult:
    try:
        out = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return _fail("docker-daemon", Severity.BLOCK, f"not reachable: {exc}")
    if out.returncode != 0:
        return _fail(
            "docker-daemon",
            Severity.BLOCK,
            out.stderr.strip() or "docker info failed",
            fix_hint="sudo systemctl start docker",
        )
    return _ok("docker-daemon", Severity.BLOCK, f"server v{out.stdout.strip()}")


def check_container_image(cfg: BuildConfig) -> CheckResult:
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", cfg.kas_container_image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        return _fail("container-image", Severity.BLOCK, "docker missing")
    if out.returncode != 0:
        return _fail(
            "container-image",
            Severity.BLOCK,
            f"image `{cfg.kas_container_image}` not found locally",
            fix_hint=(
                "Pull via `docker pull jetm/kas-build-env:latest` or build from https://github.com/jetm/kas-build-env"
            ),
        )
    return _ok("container-image", Severity.BLOCK, f"{cfg.kas_container_image} present")


def _docker_run_probe(cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    """Run a ``docker run`` probe, retrying once on timeout.

    The first ``docker run`` against an idle or busy daemon can cold-start
    past ``timeout`` even when the steady-state launch is sub-second. One
    retry absorbs the cold start so a lone transient stall does not turn
    ``check_container_bitbake`` into a spurious skip; ``FileNotFoundError``
    (no docker binary) is left to propagate.
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _find_local_bitbake_dir(cfg: BuildConfig) -> Path | None:
    """Return the workspace bitbake source directory if present, else None.

    Search order mirrors :func:`bakar.hashserv._find_binary`:
    1. ``cfg.bsp_bitbake_path``              - NXP (sources/poky/bitbake) / TI (sources/bitbake)
    2. ``<bsp_root>/layers/bitbake``         - some generic BYO layouts
    3. ``<bsp_root>/sources/bitbake``        - generic BYO (alternative)
    4. ``<bsp_root>/bitbake``               - workspace-root bitbake
    5. ``<bsp_root.parent>/bitbake``         - meta-avocado style (bsp_root = workspace/build-<stem>)
    """
    candidate = cfg.bsp_bitbake_path
    if (candidate / "bin" / "bitbake").is_file():
        return candidate
    for root in (cfg.bsp_root, cfg.bsp_root.parent):
        for subdir in ("layers", "sources", ""):
            bb = root / subdir / "bitbake" if subdir else root / "bitbake"
            if (bb / "bin" / "bitbake").is_file():
                return bb
    return None


def _parse_bitbake_version(output: str) -> tuple[int, ...] | None:
    """Parse a ``bitbake --version`` stdout string into a version tuple.

    Returns ``None`` when no version number can be extracted.

    Example::

        >>> _parse_bitbake_version("BitBake Build Tool Core version 2.8.0")
        (2, 8, 0)
    """
    m = re.search(r"(\d+(?:\.\d+)+)", output)
    if not m:
        return None
    try:
        return tuple(int(p) for p in m.group(1).split("."))
    except ValueError:
        return None


def _collect_layer_paths_for_check(cfg: BuildConfig) -> list[Path]:
    """Return all layer directories visible to the current kas composition.

    Calls :func:`bakar.kas.parse_bblayers` on ``cfg.bblayers_conf`` to get
    ``{repo: {layer, ...}}``.  For each repo, the resolved workspace root is
    tried in order:

    1. ``<bsp_root>/sources/<repo>``  - NXP / TI workspaces
    2. ``<bsp_root>/layers/<repo>``   - bbsetup workspaces

    Returns every immediate subdirectory of the resolved repo root that
    contains a ``conf/layer.conf`` file.  When ``cfg.bblayers_conf`` is
    ``None`` or does not exist, returns an empty list.
    """
    if cfg.bblayers_conf is None or not cfg.bblayers_conf.is_file():
        return []

    layer_paths: list[Path] = []
    for repo in parse_bblayers(cfg.bblayers_conf):
        sources_path = cfg.bsp_root / "sources" / repo
        layers_path = cfg.bsp_root / "layers" / repo
        if sources_path.is_dir():
            repo_root = sources_path
        elif layers_path.is_dir():
            repo_root = layers_path
        else:
            continue
        layer_paths.extend(
            child
            for child in sorted(repo_root.iterdir())
            if child.is_dir() and (child / "conf" / "layer.conf").is_file()
        )
    return layer_paths


# Compiled regex for deprecated underscore override syntax.
# Matches variable assignments (VAR_append = ...) and function definitions
# (do_install_append() {). Variable names may contain ${...} expansions such
# as FILES_${PN}_append. Does NOT match colon-form overrides.
_OVERRIDE_SYNTAX_RE = re.compile(
    r"^\s*([\w${}]+)_(append|prepend|remove|class-native|class-nativesdk)"
    r"(\s*(?:\?\?=|\?=|:=|\+=|\.=|=\.|=|\(\s*\)\s*\{|\s*\{))",
    re.MULTILINE,
)


def check_override_syntax(cfg: BuildConfig) -> CheckResult:
    """Block (or warn) when recipe/conf files use the deprecated underscore override form.

    Scarthgap (bitbake >= 2.8) rejects ``VAR_append``, ``VAR_prepend``,
    ``VAR_remove``, ``VAR_class-native``, and ``VAR_class-nativesdk`` at
    parse time with a hard ``bb.fatal``. This check surfaces the problem
    before the build starts.

    Gated on the local bitbake version: skipped when the workspace is not
    yet synced, the binary is absent, or the detected version is < 2.8.

    Severity: BLOCK for ``.bb``/``.bbappend``/``.inc``; WARN for ``.conf``.
    """
    name = "override-syntax"

    # --- version gate ---
    bb_dir = _find_local_bitbake_dir(cfg)
    if bb_dir is None:
        return _skip(name, Severity.WARN, "bitbake binary not found (workspace not synced?)")

    try:
        result = subprocess.run(  # pragma: no cover
            [str(bb_dir / "bin" / "bitbake"), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return _skip(name, Severity.WARN, f"bitbake --version failed: {exc}")

    ver = _parse_bitbake_version(result.stdout)  # pragma: no cover
    if ver is None or ver < (2, 8):  # pragma: no cover
        return _skip(  # pragma: no cover
            name,
            Severity.WARN,
            f"bitbake version {ver} < 2.8; override-syntax check not applicable",
        )

    # --- collect layer paths ---
    layer_paths = _collect_layer_paths_for_check(cfg)
    if not layer_paths:
        return _skip(name, Severity.WARN, "no layer paths found")

    # --- scan recipe files (BLOCK) then conf files (WARN) ---
    recipe_extensions = ("*.bb", "*.bbappend", "*.inc")
    conf_extensions = ("*.conf",)

    for layer_path in layer_paths:
        for glob in recipe_extensions:
            for file_path in sorted(layer_path.rglob(glob)):
                try:
                    content = file_path.read_text(errors="replace")
                except OSError:
                    continue
                m = _OVERRIDE_SYNTAX_RE.search(content)
                if m:
                    line_no = content[: m.start()].count("\n") + 1
                    matched = m.group(0).strip()
                    return _fail(
                        name,
                        Severity.BLOCK,
                        f"{file_path}:{line_no}: {matched}",
                        fix_hint="Use colon form: VAR:append, VAR:prepend, VAR:remove",
                    )

    for layer_path in layer_paths:
        for glob in conf_extensions:
            for file_path in sorted(layer_path.rglob(glob)):
                try:
                    content = file_path.read_text(errors="replace")
                except OSError:
                    continue
                m = _OVERRIDE_SYNTAX_RE.search(content)
                if m:
                    line_no = content[: m.start()].count("\n") + 1
                    matched = m.group(0).strip()
                    return _fail(
                        name,
                        Severity.WARN,
                        f"{file_path}:{line_no}: {matched}",
                        fix_hint="Use colon form: VAR:append, VAR:prepend, VAR:remove",
                    )

    return _ok(name, Severity.BLOCK, "no deprecated underscore override syntax found")


def check_container_bitbake(cfg: BuildConfig) -> CheckResult:
    bb_dir = _find_local_bitbake_dir(cfg)
    cmd = ["docker", "run", "--rm", "--entrypoint", "bash"]
    if bb_dir is not None:
        cmd += ["-v", f"{bb_dir}:/tmp/bitbake:ro"]
        shell = "export PATH=/tmp/bitbake/bin:$PATH && which bitbake && bitbake --version"
    else:
        shell = "which bitbake && bitbake --version"
    cmd += [cfg.kas_container_image, "-c", shell]
    try:
        out = _docker_run_probe(cmd)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return _skip("container-bitbake", Severity.INFO, f"could not inspect: {exc}")
    if out.returncode != 0:
        detail = (
            "not in container PATH (workspace-sourced)"
            if "no bitbake" in out.stderr
            else f"inspection failed: {out.stderr.strip()}"
        )
        return _skip("container-bitbake", Severity.INFO, detail)
    lines = out.stdout.strip().splitlines()
    raw_ver = lines[1].strip() if len(lines) > 1 else ""
    m = re.search(r"(\d+\.\d+[\.\d]*)", raw_ver)
    version = f"BitBake v{m.group(1)}" if m else raw_ver
    return _ok("container-bitbake", Severity.INFO, version)


def check_cache_dirs(cfg: BuildConfig) -> CheckResult:
    sstate_env = os.environ.get("SSTATE_DIR", "")
    dl_env = os.environ.get("DL_DIR", "")
    configured = {k: Path(v) for k, v in (("SSTATE_DIR", sstate_env), ("DL_DIR", dl_env)) if v}
    if not configured:
        return _ok("cache-dirs", Severity.BLOCK, "SSTATE_DIR/DL_DIR not set; using kas defaults")
    missing = [str(p) for p in configured.values() if not p.is_dir() or not os.access(p, os.W_OK)]
    if missing:
        return _fail(
            "cache-dirs",
            Severity.BLOCK,
            f"missing or not writable: {', '.join(missing)}",
            fix_hint="mkdir -p the paths above and chown them to $USER",
        )
    return _ok("cache-dirs", Severity.BLOCK, ", ".join(f"{k}={v}" for k, v in configured.items()))


def check_sysctl(cfg: BuildConfig) -> CheckResult:
    keys = {
        "fs.inotify.max_user_instances": (cfg.host_inotify_instances, Severity.WARN),
        "fs.inotify.max_user_watches": (cfg.host_inotify_watches, Severity.WARN),
        "vm.swappiness": (cfg.host_swappiness_max, Severity.INFO),  # check as "<= threshold"
    }
    issues: list[str] = []
    worst_sev = Severity.INFO
    for key, (threshold, sev) in keys.items():
        value = _read_sysctl(key)
        if value is None:
            issues.append(f"{key}: unreadable")
            worst_sev = _max_sev(worst_sev, sev)
            continue
        if key == "vm.swappiness":
            if value > threshold:
                issues.append(f"{key}={value} (>{threshold})")
                worst_sev = _max_sev(worst_sev, sev)
        else:
            if value < threshold:
                issues.append(f"{key}={value} (<{threshold})")
                worst_sev = _max_sev(worst_sev, sev)
    if issues:
        return _fail(
            "sysctl",
            worst_sev,
            "; ".join(issues),
            fix_hint=(
                "Write /etc/sysctl.d/99-yocto.conf with\n"
                "  fs.inotify.max_user_instances = 8192\n"
                "  fs.inotify.max_user_watches = 1048576\n"
                "  vm.swappiness = 10\n"
                "and run `sudo sysctl --system`."
            ),
        )
    return _ok("sysctl", Severity.WARN, "inotify/swappiness sane")


def check_docker_ulimits(cfg: BuildConfig) -> CheckResult:
    """Confirm the daemon default-ulimits nofile soft limit is raised."""
    daemon_json = Path("/etc/docker/daemon.json")
    if not daemon_json.is_file():
        return _fail(
            "docker-ulimits",
            Severity.WARN,
            "/etc/docker/daemon.json missing",
            fix_hint="See the bakar README for the recommended daemon.json template.",
        )
    try:
        data = json.loads(daemon_json.read_text())
    except json.JSONDecodeError as exc:
        return _fail("docker-ulimits", Severity.WARN, f"daemon.json parse error: {exc}")
    nofile = data.get("default-ulimits", {}).get("nofile", {})
    soft = nofile.get("Soft", 0)
    if soft < cfg.host_nofile_soft:
        return _fail(
            "docker-ulimits",
            Severity.WARN,
            f"default-ulimits.nofile.Soft={soft} (<{cfg.host_nofile_soft})",
            fix_hint='Set `"default-ulimits": {"nofile": {"Soft": 65536, "Hard": 2097152}}`'
            " in /etc/docker/daemon.json and `sudo systemctl restart docker`.",
        )
    return _ok("docker-ulimits", Severity.WARN, f"nofile soft={soft}")


def check_disk_free(cfg: BuildConfig) -> CheckResult:
    """Each checked mount needs at least ``cfg.disk_free_threshold_gb`` free.

    sstate and downloads paths are sourced from ``cfg.sstate_dir`` /
    ``cfg.dl_dir`` first; ``SSTATE_DIR`` / ``DL_DIR`` env vars are used
    only as a fallback when the corresponding cfg field is ``None`` (config
    intent is persistent, env is a transient override). Candidates that do
    not exist are skipped, and candidates that resolve to a filesystem
    device already checked (by ``os.stat().st_dev``) are deduplicated so a
    workspace and sstate dir on the same partition are measured once.
    """
    sstate = cfg.sstate_dir if cfg.sstate_dir is not None else os.environ.get("SSTATE_DIR")
    dl = cfg.dl_dir if cfg.dl_dir is not None else os.environ.get("DL_DIR")
    candidates = [
        ("workspace", cfg.workspace),
        *([("sstate", Path(sstate))] if sstate else []),
        *([("downloads", Path(dl))] if dl else []),
    ]
    low: list[str] = []
    seen_devs: set[int] = set()
    for label, path in candidates:
        if not path.exists():
            continue
        try:
            dev = os.stat(path).st_dev
        except OSError:
            continue
        if dev in seen_devs:
            continue
        seen_devs.add(dev)
        st = shutil.disk_usage(path)
        free_gb = st.free / (1024**3)
        if free_gb < cfg.disk_free_threshold_gb:
            low.append(f"{label}@{path} free={free_gb:.1f}G")
    if low:
        return _fail(
            "disk-free",
            Severity.BLOCK,
            "; ".join(low),
            fix_hint="Remove stale build artifacts or sstate slices.",
        )
    return _ok("disk-free", Severity.BLOCK, f">= {cfg.disk_free_threshold_gb:.0f}G free on each checked mount")


def _swap_free_kb_by_kind() -> tuple[int, int]:
    """Split free swap into ``(disk_kb, zram_kb)`` from ``/proc/swaps``.

    zram swap is RAM-backed, so counting it toward the host memory budget
    double-counts physical RAM. Disk-backed swap is genuine extra capacity, so
    the two kinds are summed separately and only disk swap feeds the floor.
    """
    try:
        lines = Path("/proc/swaps").read_text().splitlines()
    except OSError:
        return (0, 0)
    disk_kb = 0
    zram_kb = 0
    for line in lines[1:]:  # skip the header row
        parts = line.split()
        if len(parts) < 4:
            continue
        name, size_kb, used_kb = parts[0], parts[2], parts[3]
        try:
            free_kb = int(size_kb) - int(used_kb)
        except ValueError:
            continue
        if name.startswith("/dev/zram"):
            zram_kb += free_kb
        else:
            disk_kb += free_kb
    return (disk_kb, zram_kb)


def check_memory(cfg: BuildConfig) -> CheckResult:
    avail_kb = 0
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            avail_kb = int(line.split()[1])
            break
    disk_kb, zram_kb = _swap_free_kb_by_kind()
    total_mb = (avail_kb + disk_kb) / 1024
    detail = f"available={avail_kb / (1024**2):.1f}G + disk-swap={disk_kb / (1024**2):.1f}G"
    if zram_kb:
        detail += f" (zram {zram_kb / (1024**2):.1f}G excluded: RAM-backed)"
    if total_mb < cfg.host_mem_min_gb * 1024:
        return _fail(
            "memory",
            Severity.WARN,
            f"{detail} (<{cfg.host_mem_min_gb:g}G)",
            fix_hint="Close RAM-heavy apps before starting a big bitbake run.",
        )
    return _ok("memory", Severity.WARN, detail)


def check_bitbake_override(cfg: BuildConfig) -> CheckResult:
    """Report whether the upstream-bitbake override is in place.

    Runs for both BSP families: ``override_status(cfg)`` reads from
    ``cfg.bsp_bitbake_path`` (NXP: ``sources/poky/bitbake``; TI:
    ``sources/bitbake``) and ``cfg.bsp_root / upstream-bitbake``.
    """
    from bakar.steps.bitbake_override import status as override_status

    st = override_status(cfg)
    detail_parts: list[str] = [st.detail]
    if st.branch:
        detail_parts.append(f"branch={st.branch}")
    if st.sha:
        detail_parts.append(f"sha={st.sha}")
    if st.upstream_version:
        detail_parts.append(f"upstream={st.upstream_version}")
    detail = " ".join(detail_parts)

    if st.state == "active":
        return _ok("bitbake-override", Severity.INFO, detail)
    if st.state == "stale":
        return _fail(
            "bitbake-override",
            Severity.INFO,
            detail,
            fix_hint="Run `bakar bitbake-override --apply` (or it auto-applies on `bakar build`).",
        )
    if st.state == "disabled":
        return _skip("bitbake-override", Severity.INFO, "BAKAR_BITBAKE_OVERRIDE=0")
    return _skip("bitbake-override", Severity.INFO, detail)


# ---------------------------------------------------------------------------
# NXP-only checks
# ---------------------------------------------------------------------------


def check_forks_linux_imx(cfg: BuildConfig) -> CheckResult:
    path = cfg.workspace / "nxp" / "forks" / "linux-imx"
    if not path.is_dir():
        return _fail(
            "forks-linux-imx",
            Severity.INFO,
            "forks/linux-imx absent; kernel fetch will go over the network",
            fix_hint=f"git clone https://github.com/varigit/linux-imx {path}",
        )
    return _ok("forks-linux-imx", Severity.INFO, "present (PREMIRROR will use it)")


def check_manifest_consistency(cfg: BuildConfig) -> CheckResult:
    """Report when the requested manifest/branch or on-disk SHAs drift from .repo/.

    Imported inside the function to keep the workspace/diagnostics
    dependency direction one-way.
    """
    from bakar.workspace import detect

    state = detect(cfg)
    if not state.repo_initialized:
        return _skip("manifest", Severity.INFO, ".repo/ missing (first run)")
    issues: list[str] = []
    if state.manifest_mismatch:
        issues.append(f"manifest tracked={state.repo_manifest_include!r} requested={cfg.manifest!r}")
    if state.branch_mismatch:
        issues.append(f"branch tracked={state.repo_manifests_branch!r} requested={cfg.repo_branch!r}")
    if state.sha_drift:
        sample = ", ".join(p for p, _, _ in state.sha_drift[:3])
        issues.append(f"{len(state.sha_drift)} pinned SHA drift (e.g. {sample})")
    if state.repo_broken:
        issues.append(".repo/manifest.xml unreadable")
    if issues:
        return _fail(
            "manifest",
            Severity.INFO,
            "; ".join(issues),
            fix_hint="`bakar build` will force a full re-sync to reconcile.",
        )
    return _ok("manifest", Severity.INFO, "matches .repo/ state")


def check_git_object_cache(cfg: BuildConfig) -> CheckResult:
    """Report bare git-object cache size under .repo/project-objects/."""
    cache_root = cfg.workspace / "nxp" / ".repo" / "project-objects"
    if not cache_root.is_dir():
        return _skip("git-cache", Severity.INFO, ".repo/project-objects/ absent (pre-bootstrap)")
    entries: list[tuple[str, int]] = []
    total = 0
    for entry in cache_root.iterdir():
        if not entry.name.endswith(".git"):
            continue
        size = _dir_size(entry)
        total += size
        entries.append((entry.name, size))
    if not entries:
        return _skip("git-cache", Severity.INFO, "no *.git entries under .repo/project-objects/")
    entries.sort(key=lambda item: item[1], reverse=True)
    top = ", ".join(f"{name.removesuffix('.git')}={_fmt_size(size)}" for name, size in entries[:5])
    return _ok(
        "git-cache",
        Severity.INFO,
        f"{_fmt_size(total)} across {len(entries)} repos (top: {top})",
    )


# ---------------------------------------------------------------------------
# TI-only checks (Phase A skeletons; no behavior change for NXP builds)
# ---------------------------------------------------------------------------


def check_ti_layertool_present(cfg: BuildConfig) -> CheckResult:
    """Confirm ``ti/oe-layertool/oe-layertool-setup.sh`` is on disk."""
    script = cfg.workspace / "ti" / "oe-layertool" / "oe-layertool-setup.sh"
    if not script.is_file():
        return _fail(
            "ti-layertool",
            Severity.BLOCK,
            f"{script} missing - bakar cannot populate ti/sources/ without it",
            fix_hint=(
                "git clone -b master_var01 https://github.com/varigit/oe-layersetup "
                f"{cfg.workspace / 'ti' / 'oe-layertool'}"
            ),
        )
    return _ok("ti-layertool", Severity.BLOCK, "oe-layertool-setup.sh present")


def check_ti_layertool_config_consistency(cfg: BuildConfig) -> CheckResult:
    """Compare ``ti/conf/active-config.txt`` (last applied) against the
        requested config filename. SKIP on first run before any populate
    has succeeded; FAIL on drift so ``bakar build`` knows to force a
        re-populate.
    """
    tracked = cfg.workspace / "ti" / "conf" / "active-config.txt"
    if not tracked.is_file():
        return _skip("ti-config", Severity.INFO, "active-config.txt absent (first run)")
    try:
        recorded = tracked.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return _fail("ti-config", Severity.INFO, f"unreadable: {exc}")
    if recorded != cfg.manifest:
        return _fail(
            "ti-config",
            Severity.INFO,
            f"tracked={recorded!r} requested={cfg.manifest!r}",
            fix_hint="`bakar build` will re-run oe-layertool-setup.sh to reconcile.",
        )
    return _ok("ti-config", Severity.INFO, f"matches {recorded}")


def check_forks_ti_linux_kernel(cfg: BuildConfig) -> CheckResult:
    """Mirror :func:`check_forks_linux_imx` for the TI kernel fork."""
    path = cfg.workspace / "ti" / "forks" / "ti-linux-kernel"
    if not path.is_dir():
        return _fail(
            "forks-ti-linux-kernel",
            Severity.INFO,
            "ti/forks/ti-linux-kernel absent; kernel fetch will go over the network",
            fix_hint=(
                f"git clone -b ti-linux-6.12.y_11.00.09.04_var01 https://github.com/varigit/ti-linux-kernel {path}"
            ),
        )
    return _ok("forks-ti-linux-kernel", Severity.INFO, "present (PREMIRROR will use it)")


def check_forks_ti_u_boot(cfg: BuildConfig) -> CheckResult:
    """Mirror :func:`check_forks_linux_imx` for the TI u-boot fork."""
    path = cfg.workspace / "ti" / "forks" / "ti-u-boot"
    if not path.is_dir():
        return _fail(
            "forks-ti-u-boot",
            Severity.INFO,
            "ti/forks/ti-u-boot absent; u-boot fetch will go over the network",
            fix_hint=(f"git clone -b ti-u-boot-2025.01_11.00.09.04_var01 https://github.com/varigit/ti-u-boot {path}"),
        )
    return _ok("forks-ti-u-boot", Severity.INFO, "present (PREMIRROR will use it)")


# Bitbake thread knobs the tuning overlay derives from NPROC. A user
# local_conf_header section can re-assign them; _thread_var_overrides
# detects when such an assignment wins over the overlay's NPROC line.
_THREAD_VAR_RE = re.compile(
    r"^\s*(BB_NUMBER_THREADS|BB_NUMBER_PARSE_THREADS|PARALLEL_MAKE)\s*(\?\?=|\?=|:=|\+=|\.=|=)\s*(.*?)\s*$",
    re.MULTILINE,
)


def _thread_var_overrides(local_conf: Path) -> dict[str, str]:
    """Return ``{var: value}`` for thread vars whose winning assignment ignores NPROC.

    kas merges every ``local_conf_header`` section (user yaml and bakar
    tuning overlay alike) into ``local.conf``, so scanning that file with
    last-assignment-wins semantics resolves section ordering empirically
    instead of guessing kas merge order. The tuning overlay's own lines
    derive from NPROC (``${@os.environ.get('NPROC', ...)}``); when the
    last assignment of a variable still references NPROC, the overlay
    won and there is no override to report.

    Weak (``?=``/``??=``) and append (``+=``/``.=``) operators are skipped:
    a weak assignment never beats the overlay's plain ``=`` regardless of
    position, and appends to a thread count are malformed config rather
    than a deliberate override. Returns ``{}`` when ``local.conf`` does
    not exist yet (pre-first-build).
    """
    try:
        text = local_conf.read_text()
    except OSError:
        return {}
    non_comment = "\n".join(line for line in text.splitlines() if not re.match(r"^\s*#", line))
    winners: dict[str, str] = {}
    for m in _THREAD_VAR_RE.finditer(non_comment):
        var, op, value = m.group(1), m.group(2), m.group(3)
        if op not in ("=", ":="):
            continue
        winners[var] = value.strip().strip('"').strip("'")
    return {var: value for var, value in winners.items() if "NPROC" not in value and not value.startswith("${@")}


def check_nproc(cfg: BuildConfig) -> CheckResult:
    """Report the NPROC base and the effective bitbake thread settings.

    The tuning overlay maps the build threads to NPROC unless a config.toml
    [build] override is set: ``bb_number_threads`` drives BB_NUMBER_THREADS and
    BB_NUMBER_PARSE_THREADS (exported as BAKAR_BB_NUMBER_THREADS), and
    ``parallel_make`` drives PARALLEL_MAKE (exported as BAKAR_PARALLEL_MAKE),
    decoupled from NPROC. ``cfg.nproc`` overrides the auto-detected NPROC base.
    The effective values shown here are what bitbake will actually use - unless
    a user conf section re-assigns one of the knobs after the tuning section, in
    which case the local.conf value wins and is shown with a marker.
    """
    # Match _build_env's NPROC precedence: a non-empty live env var wins over
    # cfg.nproc, which wins over the cpu_count auto-detect. The truthiness check
    # treats an exported-but-empty NPROC ("") as unset, exactly as _build_env
    # does, so the doctor and the build never disagree on the value bitbake uses.
    if os.environ.get("NPROC"):
        nproc, source = os.environ["NPROC"], "from environment"
    elif cfg.nproc is not None:
        nproc, source = str(cfg.nproc), "from config.toml"
    else:
        nproc, source = str(os.cpu_count() or 16), "auto-detected; override with $NPROC"
    overrides = _thread_var_overrides(cfg.bsp_root / "build" / "conf" / "local.conf")
    threads_default = str(cfg.bb_number_threads) if cfg.bb_number_threads is not None else nproc
    make_default = f"-j {cfg.parallel_make}" if cfg.parallel_make is not None else f"-j {nproc}"

    def knob(var: str, default: str) -> str:
        if var in overrides:
            return f"{overrides[var]} (local.conf override)"
        return default

    tasks = knob("BB_NUMBER_THREADS", threads_default)
    parse = knob("BB_NUMBER_PARSE_THREADS", threads_default)
    make = knob("PARALLEL_MAKE", make_default)
    threads = f"{tasks} bitbake tasks, {parse} parse threads, make {make}"
    return _ok("nproc", Severity.INFO, f"NPROC={nproc} ({source}) -> {threads}")


def check_bitbake_locks(cfg: BuildConfig) -> CheckResult:
    """Remove stale bitbake lock and socket files and report the result.

    A crashed build leaves bitbake.lock, bitbake.sock, and hashserve.sock
    behind. This check auto-repairs: if the owning PID is gone all three
    are removed. If a live bitbake holds the lock the check fails with BLOCK
    so the user knows a build is in progress.
    """
    from bakar.steps.kas_build import clear_stale_bitbake_locks

    build_dir = cfg.bsp_root / "build"
    lock = build_dir / "bitbake.lock"
    sockets = [build_dir / "bitbake.sock", build_dir / "hashserve.sock"]
    stale_sockets = [s for s in sockets if s.exists() or s.is_socket()]

    if not lock.exists():
        if stale_sockets:
            for s in stale_sockets:
                s.unlink(missing_ok=True)
            names = ", ".join(s.name for s in stale_sockets)
            return _ok("bitbake-locks", Severity.BLOCK, f"orphaned sockets removed: {names}")
        return _ok("bitbake-locks", Severity.BLOCK, "no stale locks or sockets")

    try:
        pid = int(lock.read_text().strip())
    except ValueError, OSError:
        removed = clear_stale_bitbake_locks(cfg)
        names = ", ".join(p.name for p in removed)
        return _ok("bitbake-locks", Severity.BLOCK, f"unreadable lock and sockets removed: {names}")

    try:
        os.kill(pid, 0)
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        if cmdline_path.exists():
            cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode(errors="replace")
            if "bitbake" not in cmdline.lower():
                removed = clear_stale_bitbake_locks(cfg)
                names = ", ".join(p.name for p in removed)
                return _ok("bitbake-locks", Severity.BLOCK, f"stale files removed (PID {pid} reused): {names}")
        return _fail(
            "bitbake-locks",
            Severity.BLOCK,
            f"bitbake.lock held by PID {pid} - another build is running",
            fix_hint="wait for the running build to finish or kill it, then re-run doctor",
        )
    except ProcessLookupError:
        removed = clear_stale_bitbake_locks(cfg)
        names = ", ".join(p.name for p in removed)
        return _ok("bitbake-locks", Severity.BLOCK, f"stale files removed (PID {pid} gone): {names}")
    except PermissionError:
        return _skip("bitbake-locks", Severity.BLOCK, f"cannot signal PID {pid} to check liveness")


# ---------------------------------------------------------------------------
# bbsetup-only checks
# ---------------------------------------------------------------------------


def check_bbsetup_initialized(cfg: BuildConfig) -> CheckResult:
    """Confirm the bitbake-setup workspace was initialized.

    A ``bitbake-setup init`` run writes ``config/config-upstream.json``
    and ``build/init-build-env`` under the setup dir. Either being absent
    means the workspace is not ready for a bakar build.
    """
    config_json = cfg.bsp_root / "config" / "config-upstream.json"
    init_env = cfg.bsp_root / "build" / "init-build-env"
    missing = [str(p) for p in (config_json, init_env) if not p.exists()]
    if missing:
        return _fail(
            "bbsetup-init",
            Severity.BLOCK,
            f"workspace not initialized; missing: {', '.join(missing)}",
            fix_hint="Run `bitbake-setup init` to initialize the workspace, then retry.",
        )
    return _ok("bbsetup-init", Severity.BLOCK, "config-upstream.json and build/init-build-env present")


def check_bbsetup_config_sources(cfg: BuildConfig) -> CheckResult:
    """Confirm ``config-upstream.json`` carries a non-empty ``data.sources`` block."""
    config_json = cfg.bsp_root / "config" / "config-upstream.json"
    try:
        data = json.loads(config_json.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return _fail(
            "bbsetup-sources",
            Severity.BLOCK,
            f"config-upstream.json unreadable: {exc}",
            fix_hint="Re-run `bitbake-setup init` to regenerate config/config-upstream.json.",
        )
    sources = data.get("data", {}).get("sources", {})
    if not sources:
        return _fail(
            "bbsetup-sources",
            Severity.BLOCK,
            "config-upstream.json data.sources is empty or absent",
            fix_hint="Re-run `bitbake-setup init` to regenerate config/config-upstream.json.",
        )
    return _ok("bbsetup-sources", Severity.BLOCK, f"{len(sources)} source(s) in data.sources")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


CheckFunc = Callable[[BuildConfig], CheckResult]


def _read_psi_avg10(resource: str) -> float | None:
    """Return the ``some avg10=`` value from ``/proc/pressure/<resource>``.

    Returns None when the file is absent, unreadable, or the expected
    field is missing - covers kernels without PSI support and containers
    that lack access to the host procfs.
    """
    try:
        text = Path(f"/proc/pressure/{resource}").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("some "):
            for field in line.split():
                if field.startswith("avg10="):
                    try:
                        return float(field.split("=", 1)[1])
                    except ValueError:
                        return None
    return None


def check_psi_support(cfg: BuildConfig) -> CheckResult:
    """Check whether PSI throttling is available and configured."""
    name = "psi_support"
    available = _read_psi_avg10("cpu") is not None
    any_set = any(v is not None for v in (cfg.pressure_max_cpu, cfg.pressure_max_io, cfg.pressure_max_memory))

    if not available and any_set:
        return _fail(
            name,
            Severity.WARN,
            "PSI throttling configured in config.toml but kernel lacks /proc/pressure support",
        )
    if not available:
        return _skip(name, Severity.INFO, "PSI not available on this kernel; throttling disabled")
    if not any_set:
        # The hint depends on whether auto-calibration is already on: when it
        # is, the missing thresholds just mean no calibrated build has
        # completed yet, and suggesting the user set the flag they already
        # set reads as a contradiction.
        if cfg.psi_autocalibrate:
            return _skip(
                name,
                Severity.INFO,
                "PSI available; auto-calibration on, thresholds written after the next successful build",
            )
        return _skip(
            name,
            Severity.INFO,
            "PSI available; no thresholds configured (set [build] psi_autocalibrate = true to tune)",
        )
    active = ", ".join(
        f"{k}={v}"
        for k, v in (
            ("cpu", cfg.pressure_max_cpu),
            ("io", cfg.pressure_max_io),
            ("memory", cfg.pressure_max_memory),
        )
        if v is not None
    )
    return _ok(name, Severity.INFO, f"PSI throttling active: {active}")


def _buildtools_gcc(toolchain: BuildtoolsToolchain) -> Path | None:
    """Locate the native gcc whose loader the uninative probe must run.

    Two detection paths feed this:

    * already-sourced: ``toolchain.sysroot`` points at the native sysroot, so
      its ``usr/bin/gcc`` is the binary to probe.
    * env-script: the toolchain is found via ``BAKAR_BUILDTOOLS_DIR`` and not
      yet sourced; the install root carries the native sysroot under
      ``sysroots/*-pokysdk-linux``. Probe that gcc when it is on disk.

    Returns None when no concrete gcc is locatable (env-script path with an
    unconventional layout); the caller then reports presence without a loader
    probe rather than a false failure.
    """
    if toolchain.sysroot is not None:
        gcc = toolchain.sysroot / "usr" / "bin" / "gcc"
        return gcc if gcc.exists() else None
    if toolchain.env_script is not None:
        for gcc in sorted(toolchain.env_script.parent.glob("sysroots/*-pokysdk-linux/usr/bin/gcc")):
            return gcc
    return None


def check_host_preflight(cfg: BuildConfig) -> CheckResult:
    """Host-mode preflight: buildtools-extended present AND uninative loader runs.

    Host builds run bitbake against the pinned ``buildtools-extended`` toolchain
    whose ``-native`` binaries carry uninative's shipped dynamic loader. This
    gate fails loudly before a build when the toolchain is absent (so bitbake
    never falls back to the system gcc) or when its loader cannot execute on
    the host kernel (the uninative independence the host path depends on).

    Container builds do not exercise the host toolchain, so the check skips
    when ``cfg.host_mode`` is False.
    """
    name = "host-preflight"
    if not cfg.host_mode:
        return _skip(name, Severity.INFO, "container build; host toolchain not exercised")

    toolchain = detect_buildtools()
    if not toolchain.present:
        return _fail(
            name,
            Severity.BLOCK,
            f"buildtools-extended toolchain not found ({toolchain.detail})",
            fix_hint=(
                f"Install the buildtools-extended-tarball and source its environment-setup-* "
                f"script, or set {BUILDTOOLS_DIR_ENV} to its install dir."
            ),
        )

    gcc = _buildtools_gcc(toolchain)
    if gcc is None:
        return _ok(
            name,
            Severity.BLOCK,
            f"buildtools-extended present ({toolchain.detail}); loader probe skipped (gcc not locatable)",
        )

    try:
        out = subprocess.run(
            [str(gcc), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError as exc:
        return _fail(
            name,
            Severity.BLOCK,
            f"uninative loader for {gcc} is not runnable: {exc}",
            fix_hint="Rebuild uninative against a newer glibc or pin a worker kernel/glibc floor.",
        )
    if out.returncode != 0:
        return _fail(
            name,
            Severity.BLOCK,
            f"uninative loader for {gcc} failed (exit {out.returncode}): {out.stderr.strip()}",
            fix_hint="Rebuild uninative against a newer glibc or pin a worker kernel/glibc floor.",
        )
    return _ok(
        name,
        Severity.BLOCK,
        f"buildtools-extended present and uninative loader runs ({toolchain.detail})",
    )


def _git_identity_probe_dir(workspace: Path) -> str | None:
    """Pick a directory whose ``git config`` query resolves the identity git
    will actually use during a build.

    ``includeIf "gitdir:..."`` conditionals only fire when git is operating
    inside a repository. The workspace root is frequently not a git repo (the
    kas/repo-tool layout keeps the layers as sub-repos), so querying from there
    never matches the conditional and a valid per-tree identity reads as
    missing. Probe the first sub-repo instead so the conditional resolves the
    same way the sync steps will.
    """
    if not workspace.is_dir():
        return None
    if (workspace / ".git").exists():
        return str(workspace)
    try:
        for child in sorted(workspace.iterdir()):
            if child.is_dir() and (child / ".git").exists():
                return str(child)
    except OSError:
        pass
    return str(workspace)


def check_git_global_config(cfg: BuildConfig) -> CheckResult:
    """Verify that ``user.email`` and ``user.name`` are configured for the workspace.

    A missing identity makes ``repo`` and ``oe-layertool`` sync steps fail
    mid-fetch with opaque errors (``please tell me who you are``). This BLOCK
    check surfaces the misconfiguration before any sync runs.

    Runs ``git config <key>`` (without ``--global``) from a workspace sub-repo
    (see ``_git_identity_probe_dir``) so that ``includeIf "gitdir:..."``
    conditionals in ``~/.gitconfig`` are honoured - a developer who keeps
    separate identities for different project trees (work vs. personal) would
    otherwise see a false BLOCK even though git resolves the right identity
    during a build. The workspace root itself is often not a git repo, where
    the conditional cannot match.
    """
    name = "git-global-config"
    cwd = _git_identity_probe_dir(cfg.workspace)

    def _read(key: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", "config", key],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=cwd,
                check=False,
            )
        except FileNotFoundError, subprocess.TimeoutExpired:
            return None
        if out.returncode != 0:
            return None
        value = out.stdout.strip()
        return value or None

    email = _read("user.email")
    user_name = _read("user.name")

    missing = [k for k, v in (("user.email", email), ("user.name", user_name)) if v is None]
    if missing:
        hint_lines = [f'git config {k} "{"you@example.com" if k == "user.email" else "Your Name"}"' for k in missing]
        return _fail(
            name,
            Severity.BLOCK,
            f"missing global git identity: {', '.join(missing)}",
            fix_hint="; ".join(hint_lines),
        )
    return _ok(name, Severity.BLOCK, f"user.email={email}")


def check_kas_yaml_syntax(cfg: BuildConfig) -> CheckResult:
    """Validate the generated kas YAML parses cleanly via ``kas dump``.

    A malformed kas YAML otherwise fails mid-build with an opaque kas-container
    traceback after the image has already been pulled. This BLOCK check runs
    ``kas dump <file>`` before any expensive step and surfaces the parser's
    error verbatim.

    Skipped when the YAML has not been generated yet (manifest-flow runs
    create it on the fly) or when no host ``kas`` binary is on PATH and the
    workspace runs in container mode (``check_host_tools`` enforces the
    ``kas`` requirement in host mode separately).
    """
    name = "kas-yaml-syntax"
    kas_yaml = cfg.kas_yaml
    if not kas_yaml.exists():
        return _skip(name, Severity.BLOCK, f"kas YAML {kas_yaml} not yet generated")
    if not cfg.host_mode and shutil.which("kas") is None:
        return _skip(
            name,
            Severity.BLOCK,
            "kas binary not on host PATH; container-mode workspace, deferring to in-container parse",
        )
    try:
        out = subprocess.run(
            ["kas", "dump", str(kas_yaml)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return _skip(name, Severity.BLOCK, f"kas unavailable: {exc}")
    if out.returncode != 0:
        stderr_lines = out.stderr.splitlines()
        # kas emits INFO/WARNING lines before ERROR lines; find the first ERROR.
        error_line = next((line for line in stderr_lines if " - ERROR" in line), None)
        first_line = error_line or next(
            (line for line in stderr_lines if line.strip()),
            "kas dump exited non-zero with empty stderr",
        )
        # Git repo state errors (branch/commit mismatch) are not YAML syntax
        # problems. Skip so a stale commit reference does not block an
        # otherwise-valid build; the sync step will reconcile the state.
        if "does not contain commit" in first_line or "no such remote ref" in first_line:
            # Strip kas log prefix "DATE TIME - LEVEL    - " and truncate long commit
            # hashes to 12 chars so the detail fits in a single table row.
            parts = first_line.strip().split(" - ", 2)
            msg = parts[2] if len(parts) >= 3 else first_line.strip()
            msg = re.sub(r"\b([0-9a-f]{12})[0-9a-f]{28}\b", r"\1", msg)
            return _skip(
                name,
                Severity.BLOCK,
                f"git-state mismatch (run bakar sync): {msg}",
            )
        return _fail(
            name,
            Severity.BLOCK,
            f"{kas_yaml}: {first_line.strip()}",
            fix_hint=f"Edit {kas_yaml} and re-run; see `kas dump {kas_yaml}` for the full parser error.",
        )
    return _ok(name, Severity.BLOCK, f"{kas_yaml} parses cleanly")


# Recognized local-disk filesystems where Yocto/BitBake builds run cleanly.
_FS_ALLOW: frozenset[str] = frozenset({"ext4", "btrfs", "xfs", "zfs", "overlay"})

# Filesystems known to break or severely degrade BitBake builds: case
# sensitivity, hardlink/permission semantics, or network latency render them
# unusable as a workspace root.
_FS_BLOCK: frozenset[str] = frozenset({"vfat", "exfat", "ntfs", "9p", "nfs", "nfs4", "cifs", "smb", "smb3", "smbfs"})


def _mount_entry_in(mounts_raw: str, path: Path) -> tuple[str, str, str, str] | None:
    """Longest-prefix ``/proc/mounts`` entry covering ``path``.

    Returns ``(source, mountpoint, fstype, opts)`` or None when no mountpoint
    covers the path. Sorting by mountpoint length descending makes the most
    specific (longest) prefix win, which resolves bind/overlay mounts to the
    real backing filesystem. Shared by :func:`check_workspace_filesystem`
    (fstype only) and :func:`check_shared_cache_mounts` (source + opts too).
    """
    entries: list[tuple[str, str, str, str]] = []
    for line in mounts_raw.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        entries.append((fields[0], fields[1], fields[2], fields[3]))
    entries.sort(key=lambda e: len(e[1]), reverse=True)

    target = path.resolve()
    for source, mountpoint, fstype, opts in entries:
        try:
            mp = Path(mountpoint)
        except TypeError, ValueError:
            continue
        if target == mp or target.is_relative_to(mp):
            return (source, mountpoint, fstype, opts)
    return None


def check_workspace_filesystem(cfg: BuildConfig) -> CheckResult:
    """Detect the filesystem hosting ``cfg.workspace`` via ``/proc/mounts``.

    Network filesystems and FAT variants silently break BitBake (case
    folding, missing xattrs, no atomic rename). This WARN check inspects
    the kernel's authoritative mount table and surfaces the fstype so the
    user can move the workspace before the build wastes hours.

    PASS when fstype is in :data:`_FS_ALLOW`, FAIL when in :data:`_FS_BLOCK`,
    PASS with an "unrecognized, assumed OK" message otherwise. Reading
    ``/proc/mounts`` is portable on Linux and avoids a ``stat`` subprocess.
    """
    name = "workspace-filesystem"
    try:
        mounts_raw = Path("/proc/mounts").read_text()
    except OSError as exc:
        return _skip(name, Severity.WARN, f"/proc/mounts unreadable: {exc}")

    workspace = cfg.workspace.resolve()
    entry = _mount_entry_in(mounts_raw, workspace)
    if entry is None:
        return _skip(
            name,
            Severity.WARN,
            f"no mountpoint covers {workspace} in /proc/mounts",
        )
    _source, matched_mount, fstype, _opts = entry

    if fstype in _FS_ALLOW:
        return _ok(name, Severity.WARN, f"{fstype} at {matched_mount}")

    if fstype in _FS_BLOCK:
        return _fail(
            name,
            Severity.WARN,
            f"{fstype} at {matched_mount} cannot host a Yocto build",
            fix_hint=(
                "Move the workspace to a local ext4/btrfs/xfs path, e.g. /var/cache/sstate or ~/yocto, then re-run."
            ),
        )

    return _ok(name, Severity.WARN, f"{fstype} (unrecognized, assumed OK)")


def check_docker_version(cfg: BuildConfig) -> CheckResult:
    """Verify Docker server is >= 20.10 so ``--add-host=...:host-gateway`` works.

    A planned hashserv feature relies on ``--add-host`` host-gateway support,
    which Docker only ships from 20.10 onward. SKIP when the daemon is
    unreachable so this WARN check does not duplicate the BLOCK signal from
    :func:`check_docker_daemon`.
    """
    name = "docker-version"
    try:
        out = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return _skip(name, Severity.WARN, f"docker not reachable: {exc}")
    if out.returncode != 0:
        return _skip(
            name,
            Severity.WARN,
            out.stderr.strip() or "docker info failed",
        )

    raw = out.stdout.strip()
    # Strip suffixes like "-ce" or "+azure" before splitting on ".".
    head = raw.split("-", 1)[0].split("+", 1)[0]
    parts = head.split(".")
    try:
        seg = [int(x) for x in parts[:3]]
        # Pad to 3 segments so (20, 10) >= (20, 10, 0) compares correctly.
        while len(seg) < 3:
            seg.append(0)
        version = tuple(seg)
    except ValueError:
        return _skip(name, Severity.WARN, f"unparseable docker version: {raw!r}")

    if version >= (20, 10, 0):
        return _ok(name, Severity.WARN, f"server v{raw}")

    return _fail(
        name,
        Severity.WARN,
        f"server v{raw} lacks --add-host=...:host-gateway (need >= 20.10)",
        fix_hint="Upgrade Docker, e.g. `sudo apt upgrade docker-ce` (or the equivalent for your distro).",
    )


def check_docker_storage_driver(cfg: BuildConfig) -> CheckResult:
    """Verify the Docker storage driver is ``overlay2``.

    Devicemapper, btrfs, and zfs storage drivers degrade build performance and
    can cause sstate restore failures. SKIP when the daemon is unreachable so
    this WARN check does not duplicate the BLOCK signal from
    :func:`check_docker_daemon`.
    """
    name = "docker-storage-driver"
    try:
        out = subprocess.run(
            ["docker", "info", "--format", "{{.Driver}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return _skip(name, Severity.WARN, f"docker not reachable: {exc}")
    if out.returncode != 0:
        return _skip(
            name,
            Severity.WARN,
            out.stderr.strip() or "docker info failed",
        )

    driver = out.stdout.strip()
    if driver == "overlay2":
        return _ok(name, Severity.WARN, f"driver={driver}")

    return _fail(
        name,
        Severity.WARN,
        f"driver={driver!r} (want 'overlay2')",
        fix_hint=(
            'Set `"storage-driver": "overlay2"` in /etc/docker/daemon.json and run `sudo systemctl restart docker`.'
        ),
    )


def _ccache_max_size_is_default(env: dict[str, str]) -> bool:
    """Return True when ccache's max_size comes from its built-in default.

    ``ccache --show-config`` annotates each value's origin, e.g.
    ``(default) max_size = 5.0 GiB`` versus ``(environment) max_size = 50.0 GB``
    or ``(/path/ccache.conf) max_size = 0``. A ``default`` origin means no cap is
    actually configured for this cache dir. On any error, return False so the
    caller falls back to the normal threshold check.
    """
    try:
        out = subprocess.run(
            ["ccache", "--show-config"],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
            check=False,
        )
    except FileNotFoundError, subprocess.TimeoutExpired:
        return False
    if out.returncode != 0:
        return False
    for line in out.stdout.splitlines():
        m = re.match(r"\((?P<origin>[^)]*)\)\s+max_size\s*=", line)
        if m:
            return m.group("origin") == "default"
    return False


def check_ccache_health(cfg: BuildConfig) -> CheckResult:
    """Verify the workspace ccache is not at its eviction threshold.

    ccache is a host-side directory bind-mounted into the container at
    build time. A near-full cache evicts entries on every store, defeating
    the speedup it exists to provide. This WARN check parses
    ``ccache --print-stats`` (ccache 4.0+) and fails when the cache is
    >=90% full so the user can grow ``max_size`` or clear the cache before
    the next build wastes cycles.

    SKIP when the ccache directory has not been populated yet, the
    ``ccache`` binary is missing, the stats command fails, or the stats
    output predates the 4.0 machine-readable keys.
    """
    name = "ccache-health"
    ccache_dir = cfg.effective_ccache_dir
    if not ccache_dir.exists():
        return _skip(
            name,
            Severity.WARN,
            f"{ccache_dir} absent; ccache populates on first build",
        )

    if shutil.which("ccache") is None:
        return _skip(name, Severity.WARN, "ccache binary not on PATH")

    env = {**os.environ, "CCACHE_DIR": str(ccache_dir)}
    try:
        out = subprocess.run(
            ["ccache", "--print-stats"],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return _skip(name, Severity.WARN, f"ccache --print-stats failed: {exc}")
    if out.returncode != 0:
        return _skip(
            name,
            Severity.WARN,
            out.stderr.strip() or "ccache --print-stats exited non-zero",
        )

    cache_size: int | None = None
    max_size: int | None = None
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        key, value = parts
        if key == "cache_size_kibibyte":
            try:
                cache_size = int(value)
            except ValueError:
                continue
        elif key in ("max_cache_size_kibibyte", "max_size_kibibyte"):
            # ccache 4.x renamed max_size_kibibyte -> max_cache_size_kibibyte;
            # accept both so the check works across versions.
            try:
                max_size = int(value)
            except ValueError:
                continue

    if cache_size is None or max_size is None:
        return _skip(
            name,
            Severity.WARN,
            "ccache --print-stats output missing size keys",
        )

    if max_size == 0:
        return _ok(
            name,
            Severity.WARN,
            "ccache uncapped (max cache size = 0)",
        )

    # The build configures ccache via oe-core's CCACHE_CONFIGPATH
    # (meta/conf/ccache.conf, uncapped). The doctor only sets CCACHE_DIR, so
    # when no cap is configured for this dir, max_size resolves to ccache's
    # built-in 5 GiB default that nothing enforces - failing against it is a
    # false positive. Report the size without the threshold check in that case.
    if _ccache_max_size_is_default(env):
        used = _fmt_size(cache_size * 1024)
        return _ok(
            name,
            Severity.WARN,
            f"{used} cached; max_size not configured here (build sets its own ccache config)",
        )

    ratio = cache_size / max_size
    used = _fmt_size(cache_size * 1024)
    cap = _fmt_size(max_size * 1024)
    pct = int(ratio * 100)
    if ratio < 0.90:
        return _ok(name, Severity.WARN, f"{pct}% full ({used} of {cap})")

    return _fail(
        name,
        Severity.WARN,
        f"{pct}% full ({used} of {cap})",
        fix_hint=(
            f"Grow the cache with `ccache --max-size=<larger-than-{cap}>` "
            "or clear it with `ccache -C` before the next build."
        ),
    )


@dataclass
class ClusterCapacity:
    """Aggregate scheduler capacity from ``sccache --dist-status``.

    ``servers`` is None against the current upstream scheduler, which serializes
    only the aggregate counts. It is parsed opportunistically so a forked
    scheduler that adds a per-server array lights up the node table without a
    bakar-side change.
    """

    num_servers: int
    num_cpus: int
    in_progress: int
    servers: list | None = None


@dataclass
class ClusterReport:
    """Result of probing the dist scheduler.

    ``reachable`` is True only when ``sccache --dist-status`` returned parseable
    capacity; ``error`` carries a short human reason otherwise.
    """

    reachable: bool
    capacity: ClusterCapacity | None = None
    error: str | None = None


def _parse_cluster_status(stdout: str) -> ClusterCapacity | None:
    """Parse `sccache --dist-status` JSON into a :class:`ClusterCapacity`.

    Returns None on any parse failure or unexpected shape - cluster status is
    informational and must never raise into a caller's gate.
    """
    try:
        info = json.loads(stdout)["SchedulerStatus"][1]
        return ClusterCapacity(
            num_servers=info["num_servers"],
            num_cpus=info["num_cpus"],
            in_progress=info["in_progress"],
            servers=info.get("servers"),
        )
    except ValueError, KeyError, IndexError, TypeError:
        return None


def _format_capacity(cap: ClusterCapacity) -> str:
    """Render a :class:`ClusterCapacity` as the one-line preflight summary."""
    return f"{cap.num_servers} build server(s), {cap.num_cpus} cpus, {cap.in_progress} job(s) in progress"


def _parse_cluster_capacity(stdout: str) -> str | None:
    """Summarize `sccache --dist-status` JSON for the preflight message.

    Returns a string like "2 build server(s), 64 cpus, 0 job(s) in progress" so
    the user sees the live cluster size before the build, or None on any parse
    failure - the capacity line is informational and must never fail the gate.
    """
    cap = _parse_cluster_status(stdout)
    return None if cap is None else _format_capacity(cap)


def probe_cluster(scheduler_url: str | None = None) -> ClusterReport:
    """Query the dist scheduler via ``sccache --dist-status`` and report capacity.

    When ``scheduler_url`` is given it is forwarded as ``SCCACHE_DIST_SCHEDULER_URL``
    so the probe targets that cluster instead of the one in sccache's own config.
    Never raises: a missing binary, a failed subprocess, or an unparseable
    response all return an unreachable :class:`ClusterReport` carrying the reason.
    """
    env = None
    if scheduler_url:
        env = {**os.environ, "SCCACHE_DIST_SCHEDULER_URL": scheduler_url}
    try:
        status = subprocess.run(
            ["sccache", "--dist-status"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        return ClusterReport(reachable=False, error="sccache binary not found on PATH")
    except (OSError, subprocess.SubprocessError) as exc:
        return ClusterReport(reachable=False, error=f"sccache --dist-status failed: {exc}")
    if status.returncode != 0:
        detail = status.stderr.strip() or status.stdout.strip()
        msg = f"sccache --dist-status exited {status.returncode}"
        return ClusterReport(reachable=False, error=f"{msg}: {detail}" if detail else msg)
    cap = _parse_cluster_status(status.stdout)
    if cap is None:
        detail = status.stderr.strip()
        base = "scheduler unreachable or returned no capacity"
        return ClusterReport(reachable=False, error=f"{base}: {detail}" if detail else base)
    return ClusterReport(reachable=True, capacity=cap)


@dataclass
class BuildDaemonReport:
    """In-container sccache daemon view for a running bakar build.

    ``running`` is False when no build container is up. ``distributed`` is the
    total jobs sent to the cluster; ``per_node`` breaks it down by server. A
    build that compiles (``cache_misses`` > 0) with ``distributed`` == 0 is the
    local-only failure mode the dist guard exists to catch.
    """

    running: bool
    container: str | None = None
    error: str | None = None
    cache_hits: int = 0
    cache_misses: int = 0
    cache_hits_by_lang: dict[str, int] = field(default_factory=dict)
    cache_misses_by_lang: dict[str, int] = field(default_factory=dict)
    distributed: int = 0
    dist_errors: int = 0
    cache_location: str | None = None
    per_node: tuple[tuple[str, int], ...] = ()

    @property
    def verdict(self) -> str:
        if not self.running:
            return "no build container running"
        if self.error:
            return "stats unavailable"
        if self.distributed > 0:
            return "DISTRIBUTING"
        if self.cache_misses > 0:
            return "LOCAL-ONLY"
        return "idle (no compiles yet)"


def probe_build_daemon() -> BuildDaemonReport:
    """Inspect the sccache daemon inside a running bakar build container.

    Finds the build container by its ``bakar.run_id`` label and queries the
    in-container daemon's stats, so ``bakar cluster-info`` can show whether an
    in-progress build is actually distributing - not just the scheduler's
    aggregate capacity, which says nothing about the client. Uses
    :func:`bakar.build_stop.detect_runtime` to resolve the container runtime
    (docker or podman) the same way ``kas-container`` does. Host-mode builds
    run no container, so an empty result falls back to the host UDS
    daemon (:func:`_probe_host_uds_daemon`). Never raises: returns
    ``running=False`` only when neither a container nor a host daemon answers,
    and ``error=...`` when the query fails.
    """
    runtime = build_stop.detect_runtime()
    try:
        ps = subprocess.run(
            [runtime, "ps", "--filter", "label=bakar.run_id", "--format", "{{.ID}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        return BuildDaemonReport(running=False, error=f"{runtime} ps failed: {exc}")
    cids = ps.stdout.split()
    if not cids:
        # No build container (host-mode build): fall back to the host UDS daemon.
        return _probe_host_uds_daemon()
    cid = cids[0]
    return _query_sccache_daemon([runtime, "exec", cid, "sccache"], env=None, cid=cid)


def _query_sccache_daemon(sccache_argv: list[str], env: dict[str, str] | None, cid: str | None) -> BuildDaemonReport:
    """Query a running sccache daemon's stats + cache location and map to a report.

    ``sccache_argv`` is the command prefix that reaches the daemon: ``docker exec
    <cid> sccache`` for the in-container probe, or ``sccache`` with ``env`` carrying
    ``SCCACHE_SERVER_UDS`` for the host UDS probe. Returns an error report when the
    JSON stats query fails; the ``Cache location`` scan is best-effort.
    """
    try:
        out = subprocess.run(
            [*sccache_argv, "--show-stats", "--stats-format=json"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=env,
        )
        stats = json.loads(out.stdout)["stats"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        return BuildDaemonReport(running=True, container=cid, error=f"stats query failed: {exc}")
    location = None
    try:
        txt = subprocess.run(
            [*sccache_argv, "--show-stats"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=env,
        )
        for line in txt.stdout.splitlines():
            if line.strip().startswith("Cache location"):
                location = line.split("Cache location", 1)[1].strip()
                break
    except OSError, subprocess.SubprocessError:
        pass
    return _build_daemon_report_from_stats(stats, cid, location)


def _probe_host_uds_daemon() -> BuildDaemonReport:
    """Query the host-mode sccache daemon over its unix-domain socket.

    Host-mode builds run no ``bakar.run_id`` container, so
    :func:`probe_build_daemon`'s docker path finds nothing; the client daemon
    still answers on the host UDS (:func:`bakar.sccache_server.default_uds_path`).
    Query it directly so per-language and per-node stats surface for host builds
    too. Returns ``running=False`` when no host daemon answers, and never
    auto-starts one - the ``_uds_responding`` pre-check avoids sccache's implicit
    server spawn on ``--show-stats``.
    """
    from bakar import sccache_server

    uds = str(sccache_server.default_uds_path())
    if not sccache_server._uds_responding(uds):
        return BuildDaemonReport(running=False)
    env = {**os.environ, "SCCACHE_SERVER_UDS": uds}
    return _query_sccache_daemon(["sccache"], env=env, cid=None)


def _build_daemon_report_from_stats(stats: dict, cid: str | None, location: str | None) -> BuildDaemonReport:
    """Map an sccache ``--show-stats --stats-format=json`` ``stats`` block to a report.

    Pure (no docker/subprocess) so it is unit-testable without a build
    container. Preserves the per-language ``counts`` dicts sccache keys by
    display name (``C/C++``, ``Rust``, ``Assembler``) and sets the scalar
    ``cache_hits``/``cache_misses`` totals to the sums of those dicts, keeping
    the existing aggregate contract every scalar caller relies on. Missing or
    empty ``counts`` yield empty dicts and zero totals without raising.
    """
    dist = stats.get("dist_compiles", {}) or {}
    hits_by_lang = dict(stats.get("cache_hits", {}).get("counts", {}))
    misses_by_lang = dict(stats.get("cache_misses", {}).get("counts", {}))
    return BuildDaemonReport(
        running=True,
        container=cid,
        cache_hits=sum(hits_by_lang.values()),
        cache_misses=sum(misses_by_lang.values()),
        cache_hits_by_lang=hits_by_lang,
        cache_misses_by_lang=misses_by_lang,
        distributed=sum(dist.values()),
        dist_errors=int(stats.get("dist_errors", 0) or 0),
        cache_location=location,
        per_node=tuple(sorted(dist.items())),
    )


@dataclass
class CcacheReport:
    """Host ccache hit/miss view for a running bakar build.

    ``available`` is False when the cache dir is absent, the ``ccache`` binary
    is missing, or ``ccache --print-stats`` fails; ``error`` then names why.
    On success ``cache_hits`` sums the direct and preprocessed hits and
    ``cache_misses`` is the miss count.
    """

    available: bool
    cache_hits: int = 0
    cache_misses: int = 0
    error: str | None = None

    @property
    def hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return 100.0 * self.cache_hits / total if total else 0.0


def probe_ccache(ccache_dir: Path) -> CcacheReport:
    """Read host ccache hit/miss counts from ``ccache --print-stats``.

    Mirrors the ``ccache-health`` doctor check's guards: returns
    ``available=False`` when the cache dir is absent, the ``ccache`` binary is
    missing, or the stats command fails/times out. Never raises. On success
    sums ``cache_hit_direct`` + ``cache_hit_preprocessed`` into ``cache_hits``
    and reads ``cache_miss`` into ``cache_misses``.
    """
    if not ccache_dir.exists():
        return CcacheReport(available=False, error=f"{ccache_dir} absent")

    if shutil.which("ccache") is None:
        return CcacheReport(available=False, error="ccache binary not on PATH")

    env = {**os.environ, "CCACHE_DIR": str(ccache_dir)}
    try:
        out = subprocess.run(
            ["ccache", "--print-stats"],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return CcacheReport(available=False, error=f"ccache --print-stats failed: {exc}")
    if out.returncode != 0:
        return CcacheReport(
            available=False,
            error=out.stderr.strip() or "ccache --print-stats exited non-zero",
        )

    hit_direct = 0
    hit_preprocessed = 0
    misses = 0
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        key, value = parts
        try:
            count = int(value)
        except ValueError:
            continue
        if key == "cache_hit_direct":
            hit_direct = count
        elif key == "cache_hit_preprocessed":
            hit_preprocessed = count
        elif key == "cache_miss":
            misses = count

    return CcacheReport(available=True, cache_hits=hit_direct + hit_preprocessed, cache_misses=misses)


def _query_cluster_capacity() -> str | None:
    """Run `sccache --dist-status` and return its capacity summary, or None.

    Used by the container path, which has no host-side reachability probe of its
    own; the scheduler's capacity is global, so the host can still report it.
    """
    report = probe_cluster()
    return None if report.capacity is None else _format_capacity(report.capacity)


def _container_sccache_scheduler_check(name: str) -> CheckResult:
    """Warn when the sccache config names a localhost scheduler in container mode.

    The in-container client reads its scheduler URL from ``~/.config/sccache/config``;
    localhost there resolves to the container itself, so distribution silently
    fails over to local compiles. Return WARN when the config names a loopback
    scheduler, INFO-SKIP when the config is absent/unparseable or names a routable
    address (nothing else is host-side checkable for the container path).
    """
    skip_msg = "sccache-dist preflight runs in host mode only"
    conf = Path.home() / ".config" / "sccache" / "config"
    if not conf.is_file():
        return _skip(name, Severity.INFO, skip_msg)
    try:
        sched = (tomllib.loads(conf.read_text()).get("dist") or {}).get("scheduler_url", "")
    except OSError, tomllib.TOMLDecodeError:
        return _skip(name, Severity.INFO, skip_msg)
    hostname = urllib.parse.urlparse(sched).hostname if sched else None
    if hostname in ("localhost", "127.0.0.1", "::1"):
        return _fail(
            name,
            Severity.WARN,
            f"sccache config scheduler_url `{sched}` is localhost; the in-container client cannot reach it",
            fix_hint=(
                "Set [dist] scheduler_url to the host LAN address (e.g. http://<host-ip>:10600) "
                "in ~/.config/sccache/config so container builds can reach the scheduler."
            ),
        )
    # The config names a routable scheduler. We cannot verify the in-container
    # client's path from here, but the scheduler's capacity is global, so report
    # it: the user sees the build power on offer before committing to the build.
    capacity = _query_cluster_capacity()
    if capacity:
        return _skip(
            name,
            Severity.INFO,
            f"distributed compile cluster: {capacity} (in-container client reachability not host-checkable)",
        )
    return _skip(name, Severity.INFO, skip_msg)


def check_sccache_dist(cfg: BuildConfig) -> CheckResult:
    """Verify the sccache client + scheduler are usable when distributed compile is on.

    SKIP at INFO severity when ``cfg.use_sccache_dist`` is False - the user
    did not opt into distributed compilation so there is nothing to verify and
    the build is byte-for-byte unchanged.

    When enabled, the check enforces three prerequisites so a missing one fails
    fast at BLOCK severity instead of silently degrading to a local-only
    compile:

    1. The ``sccache`` binary is on PATH (the overlay routes ``CC`` through it).
    2. The configured scheduler URL responds: the host/port from
       ``cfg.sccache_scheduler_url`` is parsed and a 1-second TCP
       ``create_connection`` to it succeeds (mirroring :func:`check_hashserv`).
       An unset URL fails too - there is nothing to distribute to.
    3. The running client has distributed compile enabled: ``sccache
       --dist-status`` does not report ``Disabled``. A reachable scheduler with
       a client whose ``~/.config/sccache/config`` lacks the auth token still
       compiles local-only, which the TCP probe alone cannot catch.
    """
    name = "sccache-dist"
    if not cfg.use_sccache_dist:
        return _skip(name, Severity.INFO, "distributed compile not configured ([build] sccache_dist = false)")

    # The reachability probe below is host-side; it cannot speak for the
    # in-container client, which reaches the scheduler in its own network
    # namespace. Scope that probe to host mode. The one container precondition
    # checkable here is the config's scheduler address: localhost resolves to the
    # container itself, so a localhost scheduler_url silently forces every compile
    # local. Read the config and warn when it names one; otherwise skip.
    if not cfg.host_mode:
        return _container_sccache_scheduler_check(name)

    if shutil.which("sccache") is None:
        return _fail(
            name,
            Severity.BLOCK,
            "sccache binary not found on PATH",
            fix_hint="Install sccache (e.g. `cargo install sccache` or your package manager).",
        )

    url = cfg.sccache_scheduler_url
    if not url:
        return _fail(
            name,
            Severity.BLOCK,
            "sccache_dist is enabled but no scheduler URL is set",
            fix_hint="Set [build] sccache_scheduler_url or pass --sccache-scheduler URL.",
        )

    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    port = parsed.port
    if host is None or port is None:
        return _fail(
            name,
            Severity.BLOCK,
            f"scheduler URL `{url}` has no host:port to probe",
            fix_hint="Use a URL like http://localhost:10600 with an explicit port.",
        )

    try:
        sock = socket.create_connection((host, port), timeout=1.0)
    except OSError:
        return _fail(
            name,
            Severity.BLOCK,
            f"scheduler unreachable at {host}:{port}",
            fix_hint="Start the sccache-dist scheduler and confirm the URL/port.",
        )
    sock.close()

    # A reachable scheduler is necessary but not sufficient: if the running
    # sccache client never loaded its dist config (e.g. ~/.config/sccache/config
    # lacks the auth token) it reports "Disabled" and every compile silently
    # runs local-only. `sccache --dist-status` is the only signal that reflects
    # the client's real runtime state, so probe it rather than trust the TCP
    # connect alone. A CLI hiccup must not block - the scheduler is reachable.
    try:
        status = subprocess.run(
            ["sccache", "--dist-status"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return _ok(name, Severity.BLOCK, f"sccache present; scheduler reachable at {host}:{port}")

    if "Disabled" in status.stdout:
        return _fail(
            name,
            Severity.BLOCK,
            "sccache client reports distributed compile disabled",
            fix_hint=(
                "Set [dist] scheduler_url and [dist.auth] token in ~/.config/sccache/config, "
                "then restart the client: sccache --stop-server && "
                "SCCACHE_CONF=~/.config/sccache/config sccache --start-server."
            ),
        )

    base = f"sccache present; scheduler reachable at {host}:{port}; client dist enabled"
    capacity = _parse_cluster_capacity(status.stdout)
    message = f"{base}; cluster: {capacity}" if capacity else base
    return _ok(name, Severity.BLOCK, message)


def check_hashserv(cfg: BuildConfig) -> CheckResult:
    """Verify the workspace hashserv daemon is reachable when configured.

    SKIP at INFO severity when ``cfg.use_hashequiv`` is False - the user
    did not opt into the persistent daemon so the overlay's ``auto``
    fallback is in play and there is nothing to inspect.

    When configured, the check probes three layers in order: (1) the
    recorded PID is alive AND its cmdline names ``bitbake-hashserv``
    (delegated to :func:`bakar.hashserv.is_running`), (2) the port
    file under ``<bsp_root>/.bakar/`` is still present (a concurrent
    ``bakar hashserv stop`` between the PID-liveness probe and the
    port read is treated as "not running"), and (3) a TCP
    ``create_connection`` to ``127.0.0.1:<port>`` succeeds within 1s.
    Only when all three pass does the check PASS at WARN severity.
    """
    from bakar import hashserv

    name = "hashserv"
    if not cfg.use_hashequiv:
        return _skip(name, Severity.INFO, "hashserv daemon not configured ([build] hashserv = false)")

    if not hashserv.is_running(cfg.hashserv_state_key):
        # The build auto-starts the daemon via _build_env, so a not-yet-running
        # daemon is benign as long as the bitbake-hashserv binary is present -
        # warning here fires on every pre-build doctor run for no reason. Only
        # flag the case the user cannot recover by building: the binary is not
        # synced, so the build silently falls back to bitbake's per-build "auto"
        # server and loses the persistent cross-build hash-equivalence DB.
        if hashserv.binary_available(cfg.bsp_root):
            return _ok(name, Severity.INFO, "daemon not running; bakar build will auto-start it")
        return _fail(
            name,
            Severity.WARN,
            "hashserv enabled but bitbake-hashserv is not synced; builds fall back to "
            "bitbake's per-build auto server (no persistent cross-build hash DB)",
            fix_hint="sync the workspace so bitbake-hashserv exists, then `bakar hashserv start`",
        )

    # is_running() returned True but the PID/port files may vanish mid-check (a
    # concurrent `bakar hashserv stop`, or a crash between the two writes): treat
    # that race as the daemon not running.
    not_running_msg = "hashserv daemon is not running (state files vanished mid-check)"
    not_running_hint = "bakar hashserv start"
    state_dir = cfg.hashserv_state_key / ".bakar"
    port_file = state_dir / "hashserv.port"
    pid_file = state_dir / "hashserv.pid"

    try:
        port = int(port_file.read_text().strip())
    except FileNotFoundError:
        return _fail(name, Severity.WARN, not_running_msg, fix_hint=not_running_hint)

    try:
        pid = int(pid_file.read_text().strip())
    except FileNotFoundError:
        return _fail(name, Severity.WARN, not_running_msg, fix_hint=not_running_hint)

    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=1.0)
    except OSError:
        return _fail(
            name,
            Severity.WARN,
            f"daemon configured but unreachable at ws://localhost:{port} (PID {pid} alive, TCP probe failed)",
            fix_hint="bakar hashserv stop && bakar hashserv start",
        )
    sock.close()
    return _ok(name, Severity.WARN, f"running at ws://localhost:{port} (PID {pid})")


# Host-specific variables that, if they feed bitbake task signatures, make
# sstate hashes vary across builds/hosts. Each must be excluded from
# signature computation with a ``[vardepsexclude]`` annotation.
_HASH_LEAK_VARS: tuple[str, ...] = (
    "DATETIME",
    "BUILD_REPRODUCIBLE_BINARIES",
    "PWD",
    "USER",
    "HOME",
    "HOSTNAME",
)


def _scan_hash_leak_conf_files(conf_dir: Path) -> list[Path]:
    """Return ``local.conf`` plus sibling conf-include/overlay files to scan.

    kas may write the host-variable assignments into ``local.conf`` directly
    (``local_conf_header``) or into a sibling ``.conf``/``.inc`` include in the
    same ``build/conf`` directory. Scanning the whole conf dir covers design
    assumption A2's fallback without parsing every overlay reference.
    """
    files: list[Path] = []
    local_conf = conf_dir / "local.conf"
    if local_conf.is_file():
        files.append(local_conf)
    try:
        siblings = sorted(conf_dir.glob("*.conf")) + sorted(conf_dir.glob("*.inc"))
    except OSError:
        return files
    for path in siblings:
        if path == local_conf or not path.is_file():
            continue
        files.append(path)
    return files


def check_sstate_hash_leak(cfg: BuildConfig) -> CheckResult:
    """Warn when host-specific variables can corrupt sstate task signatures.

    Yocto computes sstate hashes from the variables a task depends on. If a
    host-varying variable (``DATETIME``, ``PWD``, ``USER``, ``HOME``,
    ``HOSTNAME``, ``BUILD_REPRODUCIBLE_BINARIES``) is assigned in
    ``build/conf/local.conf`` (or a sibling conf-include/overlay) without a
    matching ``[vardepsexclude]`` annotation, that variable leaks into the
    signature and breaks sstate reuse across builds and hosts.

    This is advisory (``WARN``, never ``BLOCK``): it scans config text, not
    real signatures. It reads only host-side files, so it is NOT a
    ``_DOCKER_CHECKS`` member - it must run in host mode too. ``_skip`` when
    ``local.conf`` does not exist yet (pre-sync).
    """
    name = "sstate-hash-leak"
    conf_dir = cfg.bsp_root / "build" / "conf"
    local_conf = conf_dir / "local.conf"
    if not local_conf.is_file():
        return _skip(name, Severity.WARN, f"{local_conf} not present (pre-sync)")

    conf_files = _scan_hash_leak_conf_files(conf_dir)
    assigned: set[str] = set()
    excluded: set[str] = set()
    for path in conf_files:
        try:
            text = path.read_text()
        except OSError:
            continue
        non_comment_lines = "\n".join(line for line in text.splitlines() if not re.match(r"^\s*#", line))
        for var in _HASH_LEAK_VARS:
            # Detect assignments: =, ?=, ??=, :=, +=, .=, =. and :override-suffix forms
            if re.search(
                rf"^\s*{re.escape(var)}(?::[A-Za-z0-9_-]+)*\s*(?:\?\?=|\?=|:=|\+=|\.=|=\.|=)",
                non_comment_lines,
                re.MULTILINE,
            ):
                assigned.add(var)
            # Only count exclusion annotations that are not commented out
            if re.search(rf"\[\s*vardepsexclude\s*\].*\b{re.escape(var)}\b", non_comment_lines):
                excluded.add(var)

    leaked = sorted(var for var in assigned if var not in excluded)
    if not leaked:
        return _ok(name, Severity.WARN, "no host-specific variables leak into sstate signatures")

    leaked_list = ", ".join(leaked)
    fix_lines = "\n".join(f'{var}[vardepsexclude] += "{var}"' for var in leaked)
    return _fail(
        name,
        Severity.WARN,
        f"host-specific variable(s) assigned without [vardepsexclude]: {leaked_list}",
        fix_hint=(
            "Add a [vardepsexclude] annotation in local.conf (or an overlay) so these "
            f"do not corrupt sstate hashes:\n{fix_lines}"
        ),
    )


# Checks that run unconditionally for every BSP family. Per-BSP extras
# are sourced from ``BspModel.doctor_extras`` at dispatch time.
#
# NOTE: Any new check that exercises the Docker daemon, the container
# image, or anything else that does not apply under ``cfg.host_mode``
# MUST also be added to ``_DOCKER_CHECKS`` below so host-mode runs of
# :func:`run_all` keep skipping it.
#
# NOTE: check_psi_support reads host /proc/pressure/ - do NOT add it
# to _DOCKER_CHECKS; it must run in both container and host mode.
#
# NOTE: ``check_git_global_config``, ``check_kas_yaml_syntax``,
# ``check_workspace_filesystem``, ``check_ccache_health``, and
# ``check_hashserv`` are NOT in ``_DOCKER_CHECKS`` - they exercise
# host-side resources (git config, host kas binary, host /proc/mounts,
# host ccache, host hashserv daemon) reachable in both container and
# host mode.
#
# NOTE: this tuple is the run registry; its order does not matter (checks are
# independent). The pre-flight REPORT is sorted by group via ``CHECK_GROUPS``
# below - when adding a check here, also list it in the matching group there.
def _is_loopback(host: str) -> bool:
    h = host.strip("[]")  # tolerate a bracketed IPv6 literal like [::1]
    return h in {"localhost", "127.0.0.1", "::1"} or h.startswith("127.")


def _check_central_endpoint(
    *,
    name: str,
    endpoint: str | None,
    default_port: int,
    probe: Callable[[str, int], bool],
    service: str,
) -> CheckResult:
    """Cluster-mode liveness of a central-tier endpoint.

    Only runs in cluster mode - ``run_all`` filters the cluster checks out when
    ``cfg.cluster`` is False. An unset endpoint is WARN (a cluster build with no
    central service is almost always a misconfiguration, but must not block); a
    loopback endpoint is WARN (valid on the hub, poisonous when the config is
    reused on another node); unreachable is BLOCK; reachable is PASS.
    """
    config_key = {"hashserv": "bb_hashserve", "prserv": "prserv_host"}[service]
    if not endpoint:
        return _fail(
            name,
            Severity.WARN,
            f"cluster mode is on but no central {service} is configured - intentional?",
            fix_hint=f"set [build] {config_key} to the shared {service} endpoint, or disable cluster mode",
        )
    host, port = split_host_port(endpoint, default_port)
    if _is_loopback(host):
        return _fail(
            name,
            Severity.WARN,
            f"central {service} endpoint {endpoint} is loopback - valid only on the hub node, "
            "but breaks when this config is reused on another cluster node",
        )
    if probe(host, port):
        return _ok(name, Severity.BLOCK, f"central {service} reachable at {host}:{port}")
    return _fail(
        name,
        Severity.BLOCK,
        f"central {service} unreachable at {host}:{port}",
        fix_hint=f"start the central {service} service, or fix [build] {config_key}",
    )


def check_central_hashserv(cfg: BuildConfig) -> CheckResult:
    """Cluster-mode central hashserv liveness (bb_hashserve); filtered out when cluster is off."""
    from bakar import hashserv

    return _check_central_endpoint(
        name="central-hashserv",
        endpoint=cfg.bb_hashserve,
        default_port=hashserv.CENTRAL_DEFAULT_PORT,
        probe=hashserv.central_listening,
        service="hashserv",
    )


def check_central_prserv(cfg: BuildConfig) -> CheckResult:
    """Cluster-mode central prserv liveness (prserv_host); filtered out when cluster is off."""
    from bakar import prserv

    return _check_central_endpoint(
        name="central-prserv",
        endpoint=cfg.prserv_host,
        default_port=prserv.CENTRAL_DEFAULT_PORT,
        probe=prserv.central_listening,
        service="prserv",
    )


def check_shared_cache_mounts(cfg: BuildConfig) -> CheckResult:
    """Cluster-mode: verify each effective shared cache dir is a writable NFS mount.

    Only runs in cluster mode (run_all filters the cluster checks out otherwise).
    Validates the *effective* dirs the build uses - the SSTATE_DIR/DL_DIR env
    overrides and effective_ccache_dir, not the raw config fields - and probes the
    ccache dir only when ccache is enabled. Per dir the order is load-bearing: a
    tempfile write probe first (it triggers an autofs automount and is truthful
    where os.access lies under NFS root_squash), THEN the /proc/mounts fstype. A
    silently-local shared dir is a BLOCK (a private cache defeats cross-node
    reuse); clock skew and soft mounts are WARN riders off the same probe file.
    """
    name = "shared-mounts"
    # (label, path, critical): sstate + downloads must be the shared NFS mounts
    # for cross-node reuse, so a local one is a BLOCK. ccache is non-critical - a
    # local ccache is a legitimate config (it only loses cross-node hit-rate, it
    # does not make the build wrong), so a non-shared ccache is a WARN.
    targets: list[tuple[str, Path, bool]] = []
    sstate = os.environ.get("SSTATE_DIR") or cfg.sstate_dir
    if sstate:
        targets.append(("sstate_dir", Path(sstate), True))
    dl = os.environ.get("DL_DIR") or cfg.dl_dir
    if dl:
        targets.append(("dl_dir", Path(dl), True))
    if cfg.ccache and cfg.effective_ccache_dir:
        targets.append(("ccache_dir", Path(cfg.effective_ccache_dir), False))
    if not targets:
        return _skip(name, Severity.BLOCK, "cluster mode with no shared cache directories configured")

    block_problems: list[str] = []
    warnings: list[str] = []
    oks: list[str] = []
    for label, path, critical in targets:
        sink = block_problems if critical else warnings
        if not path.exists():
            sink.append(f"{label} {path} is missing")
            continue
        # Write probe first: triggers an autofs automount and is truthful where
        # os.access lies on an NFS root_squash export. A unique temp name avoids a
        # stale probe from a killed run colliding under PID reuse.
        try:
            fd, probe_name = tempfile.mkstemp(prefix=".bakar-doctor-probe-", dir=path)
        except OSError:
            sink.append(f"{label} {path} exists but is not writable")
            continue
        probe = Path(probe_name)
        try:
            os.close(fd)
            skew = abs(time.time() - probe.stat().st_mtime)
        finally:
            probe.unlink(missing_ok=True)
        if skew > 5.0:
            warnings.append(f"{label}: clock skew ~{skew:.0f}s across the shared mount")
        # Read /proc/mounts AFTER the probe so a just-triggered autofs mount shows.
        try:
            mounts_raw = Path("/proc/mounts").read_text()
        except OSError as exc:
            return _skip(name, Severity.BLOCK, f"/proc/mounts unreadable: {exc}")
        entry = _mount_entry_in(mounts_raw, path)
        if entry is None:
            sink.append(f"{label} {path}: no covering mountpoint in /proc/mounts")
            continue
        source, mountpoint, fstype, opts = entry
        if fstype not in {"nfs", "nfs4"}:
            sink.append(f"{label} {path} is not an NFS mount ({fstype} at {mountpoint})")
            continue
        if "soft" in opts.split(","):
            warnings.append(f"{label} {source} is a soft mount (a timeout can corrupt the cache; use hard)")
        oks.append(f"{label} {source}")

    if block_problems:
        return _fail(
            name,
            Severity.BLOCK,
            "; ".join(block_problems),
            fix_hint=(
                "mount the shared NFS exports (hard) at these paths so the node does not build into a private cache"
            ),
        )
    detail = "; ".join(oks)
    if warnings:
        return _fail(name, Severity.WARN, "; ".join(warnings) + (f" ({detail})" if detail else ""))
    return _ok(name, Severity.BLOCK, detail or "shared cache mounts OK")


# Preflight audit (doctor-cluster-preflight): the severity policy is BLOCK iff a
# check's failure prevents a correct build, else WARN (a degradation) or INFO.
# The full surface was reviewed against that policy - every current severity is
# appropriate and the _DOCKER_CHECKS membership below is correct, so the audit
# produced no severity or membership flips. Two overlaps with the Cluster-group
# checks are intentional and must NOT be "deduplicated"; see the inline notes on
# check_cache_dirs and check_hashserv.
SHARED_CHECKS: tuple[CheckFunc, ...] = (
    check_host_tools,
    check_docker_daemon,
    check_container_image,
    check_container_bitbake,
    # BLOCK: the env-set SSTATE_DIR/DL_DIR exist and are writable. Overlaps the
    # Cluster group's check_shared_cache_mounts (which validates the *effective*
    # dirs are NFS mounts) - related but distinct: this one validates presence,
    # that one validates the shared mount. Do not merge them.
    check_cache_dirs,
    check_sysctl,
    check_docker_ulimits,
    check_disk_free,
    check_memory,
    check_nproc,
    check_bitbake_override,
    check_bitbake_locks,
    check_psi_support,
    check_git_global_config,
    check_kas_yaml_syntax,
    check_workspace_filesystem,
    check_docker_version,
    check_docker_storage_driver,
    check_ccache_health,
    # Per-workspace hashserv daemon (127.0.0.1). Distinct from the Cluster
    # group's check_central_hashserv (the shared central-tier daemon) - same
    # word, different service.
    check_hashserv,
    check_sccache_dist,
    check_sstate_hash_leak,
    check_override_syntax,
    check_host_preflight,
    check_central_hashserv,
    check_central_prserv,
    check_shared_cache_mounts,
)

# Docker-dependent checks from ``SHARED_CHECKS``. Filtered out of
# :func:`run_all` when ``cfg.host_mode`` is True because plain ``kas``
# does not invoke the container runtime. Keep this list in sync with
# any new container/Docker check added above. Audit-verified: all six members
# are docker-daemon-dependent, and host-pure checks (check_psi_support,
# check_hashserv, check_cache_dirs, check_workspace_filesystem, and the
# cluster checks) are correctly excluded so they survive host mode.
_DOCKER_CHECKS: tuple[CheckFunc, ...] = (
    check_docker_daemon,
    check_container_image,
    check_container_bitbake,
    check_docker_ulimits,
    check_docker_version,
    check_docker_storage_driver,
)


# Cluster-mode checks, filtered out of run_all when cfg.cluster is False (mirrors
# the _DOCKER_CHECKS / host_mode filter) so a default single-node build never
# runs or lists them. They are host-pure (TCP + /proc/mounts probes), so they
# stay OUT of _DOCKER_CHECKS and survive host mode when cluster mode is on.
_CLUSTER_CHECKS: tuple[CheckFunc, ...] = (
    check_central_hashserv,
    check_central_prserv,
    check_shared_cache_mounts,
)


# Single source of the pre-flight report's grouping and sort order. Each check
# runs independently (run_all passes only cfg, so SHARED_CHECKS order never
# affects results); this table alone decides how the printed report is sorted
# by group. KEEP THE CHECKS SORTED BY GROUP: assign every new check to exactly
# one group here, beside its siblings - a check name absent from every group
# renders under a trailing "Other" section so it is never dropped, but it will
# not be grouped with its peers until listed.
CHECK_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Compute & parallelism",
        ("nproc", "sccache-dist", "memory"),
    ),
    (
        "Tools & container runtime",
        (
            "host-tools",
            "docker-daemon",
            "docker-version",
            "docker-storage-driver",
            "docker-ulimits",
            "container-image",
            "container-bitbake",
        ),
    ),
    (
        "Caches & storage",
        ("cache-dirs", "ccache-health", "hashserv", "sstate-hash-leak", "disk-free"),
    ),
    (
        "Cluster",
        ("central-hashserv", "central-prserv", "shared-mounts"),
    ),
    (
        "Host tuning",
        ("sysctl", "workspace-filesystem", "psi_support", "host-preflight"),
    ),
    (
        "Workspace & build config",
        ("git-global-config", "kas-yaml-syntax", "override-syntax", "bitbake-override", "bitbake-locks"),
    ),
)


def group_results(results: list[CheckResult]) -> list[tuple[str, list[CheckResult]]]:
    """Order check results into display groups for the pre-flight report.

    Returns ``(group_name, rows)`` pairs in ``CHECK_GROUPS`` order, each group's
    rows kept in the order they were produced. A result whose name is not listed
    in ``CHECK_GROUPS`` is collected into a trailing ``"Other"`` group so a newly
    added check still appears. Groups with no rows are omitted.
    """
    by_name: dict[str, str] = {n: g for g, names in CHECK_GROUPS for n in names}
    buckets: dict[str, list[CheckResult]] = {g: [] for g, _ in CHECK_GROUPS}
    other: list[CheckResult] = []
    for r in results:
        group = by_name.get(r.name)
        (buckets[group] if group is not None else other).append(r)
    grouped = [(g, buckets[g]) for g, _ in CHECK_GROUPS if buckets[g]]
    if other:
        grouped.append(("Other", other))
    return grouped


def run_all(cfg: BuildConfig, bsp: BspModel | None = None) -> list[CheckResult]:
    """Run every applicable check, return results in order.

    When ``bsp`` is provided, the assembled list is
    ``SHARED_CHECKS + bsp.doctor_extras``. ``bsp=None`` runs only
    ``SHARED_CHECKS`` - the generic BYO path
    (``cli._dispatch_from_yaml`` returns ``bsp=None`` when the YAML
    does not target an NXP/TI SoM); family-specific gates such as
    ``check_forks_linux_imx`` would always fail in that mode and are
    skipped.

    When ``cfg.host_mode`` is True, the Docker-dependent checks in
    ``_DOCKER_CHECKS`` are filtered out (plain ``kas`` does not use the
    container runtime); the order of the remaining checks is preserved.

    When ``cfg.bsp_family == "bbsetup"`` the bbsetup pre-flight checks
    are appended after the shared checks (bbsetup has no ``BspModel``,
    so it cannot carry them via ``doctor_extras``). The host-mode filter
    still applies to the combined list afterward.
    """
    if bsp is None:
        checks: tuple[CheckFunc, ...] = SHARED_CHECKS
    else:
        checks = SHARED_CHECKS + tuple(bsp.doctor_extras)
    if cfg.bsp_family == "bbsetup":
        checks = (*checks, check_bbsetup_initialized, check_bbsetup_config_sources)
    if cfg.host_mode:
        checks = tuple(c for c in checks if c not in _DOCKER_CHECKS)
    if not cfg.cluster:
        checks = tuple(c for c in checks if c not in _CLUSTER_CHECKS)
    return [check(cfg) for check in checks]


def any_blocking_failure(results: list[CheckResult]) -> bool:
    return any(r.severity is Severity.BLOCK and r.status is Status.FAIL for r in results)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SEV_RANK = {Severity.INFO: 0, Severity.WARN: 1, Severity.BLOCK: 2}


def _max_sev(a: Severity, b: Severity) -> Severity:
    return a if _SEV_RANK[a] >= _SEV_RANK[b] else b


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(Path(root) / name, follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def _fmt_size(num_bytes: float) -> str:
    from bakar.fmt import fmt_bytes

    return fmt_bytes(num_bytes)
