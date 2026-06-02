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
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from bakar.config import BuildConfig

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
            ["docker", "image", "inspect", cfg.container_image, "--format", "{{.Id}}"],
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
            f"image `{cfg.container_image}` not found locally",
            fix_hint=(
                "Pull via `docker pull jetm/kas-build-env:latest` or build from https://github.com/jetm/kas-build-env"
            ),
        )
    return _ok("container-image", Severity.BLOCK, f"{cfg.container_image} present")


_CONTAINER_PY_FIX_HINT = (
    "Override now: `KAS_CONTAINER_IMAGE=jetm/kas-build-env:5.2-ubuntu24.04` "
    "(Python 3.12). Long-term: rebuild jetm/kas-build-env against an oe-core "
    "scarthgap host-validation OS (Fedora 38-40, Ubuntu 22.04/24.04 LTS, "
    "Debian 11/12)."
)


def check_container_os(cfg: BuildConfig) -> CheckResult:
    """Block if container Python is 3.13.x or 3.14.x.

    bitbake's parser is broken on both: 3.13 deadlocks under the
    fork-in-multi-thread tightening; 3.14 fails immediately with
    ``_pickle.PicklingError`` on ``CoreRecipeInfo.init_cacheData``'s
    lambda because the default multiprocessing context flipped to
    ``forkserver``. The ``trixie`` codename collapses into the 3.13
    case (Debian 13 ships Python 3.13).
    """
    try:
        out = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "bash",
                cfg.container_image,
                "-c",
                '. /etc/os-release && echo "$ID ${VERSION_ID:-$VERSION_CODENAME}" && python3 --version',
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return _skip("container-os", Severity.WARN, f"could not inspect: {exc}")
    if out.returncode != 0:
        return _skip("container-os", Severity.WARN, "inspection failed")
    lines = out.stdout.strip().splitlines()
    if not lines:
        return _skip("container-os", Severity.WARN, "no output from inspection")
    parts = lines[0].split()
    os_line = f"{parts[0].capitalize()} v{parts[1]}" if len(parts) >= 2 else lines[0]
    raw_py = lines[1] if len(lines) > 1 else ""
    py_minor: int | None = None
    match = re.search(r"Python (3\.\d+\.\d+)", raw_py)
    if match:
        py_minor = int(match.group(1).split(".")[1])
        py_line = f"Python v{match.group(1)}"
    else:
        py_line = raw_py
    if py_minor == 13:
        return _fail(
            "container-os",
            Severity.BLOCK,
            f"{py_line} in container ({os_line}); bitbake parser "
            "deadlocks under fork-in-multi-thread - build will hang at parsing.",
            fix_hint=_CONTAINER_PY_FIX_HINT,
        )
    if py_minor == 14:
        return _fail(
            "container-os",
            Severity.BLOCK,
            f"{py_line} in container ({os_line}); bitbake parser "
            "trips _pickle.PicklingError under default forkserver context - "
            "build fails immediately at parsing.",
            fix_hint=_CONTAINER_PY_FIX_HINT,
        )
    return _ok("container-os", Severity.BLOCK, f"{os_line} / {py_line}")


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


def check_container_bitbake(cfg: BuildConfig) -> CheckResult:
    bb_dir = _find_local_bitbake_dir(cfg)
    cmd = ["docker", "run", "--rm", "--entrypoint", "bash"]
    if bb_dir is not None:
        cmd += ["-v", f"{bb_dir}:/tmp/bitbake:ro"]
        shell = "export PATH=/tmp/bitbake/bin:$PATH && which bitbake && bitbake --version"
    else:
        shell = "which bitbake && bitbake --version"
    cmd += [cfg.container_image, "-c", shell]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
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
        "fs.inotify.max_user_instances": (4096, Severity.WARN),
        "fs.inotify.max_user_watches": (524288, Severity.WARN),
        "vm.swappiness": (20, Severity.INFO),  # check as "<= threshold"
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
    if soft < 8192:
        return _fail(
            "docker-ulimits",
            Severity.WARN,
            f"default-ulimits.nofile.Soft={soft} (<8192)",
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


def check_memory(cfg: BuildConfig) -> CheckResult:
    meminfo = Path("/proc/meminfo").read_text()
    free_kb = 0
    swap_kb = 0
    for line in meminfo.splitlines():
        if line.startswith("MemAvailable:"):
            free_kb = int(line.split()[1])
        elif line.startswith("SwapFree:"):
            swap_kb = int(line.split()[1])
    total_mb = (free_kb + swap_kb) / 1024
    if total_mb < 16 * 1024:
        return _fail(
            "memory",
            Severity.WARN,
            f"available+swap={total_mb:.0f}M (<16G)",
            fix_hint="Close RAM-heavy apps before starting a big bitbake run.",
        )
    return _ok("memory", Severity.WARN, f"available+swap={total_mb:.0f}M")


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


def check_nproc(cfg: BuildConfig) -> CheckResult:
    """Report the NPROC value that will drive BB_NUMBER_THREADS / PARALLEL_MAKE."""
    env_val = os.environ.get("NPROC")
    if env_val:
        return _ok("nproc", Severity.INFO, f"NPROC={env_val} (from environment)")
    detected = os.cpu_count() or 16
    return _ok("nproc", Severity.INFO, f"NPROC={detected} (auto-detected; override with $NPROC)")


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
        return _skip(
            name,
            Severity.INFO,
            "PSI available; no thresholds configured (run --psi-calibrate to tune)",
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


def check_git_global_config(cfg: BuildConfig) -> CheckResult:
    """Verify that ``user.email`` and ``user.name`` are configured for the workspace.

    A missing identity makes ``repo`` and ``oe-layertool`` sync steps fail
    mid-fetch with opaque errors (``please tell me who you are``). This BLOCK
    check surfaces the misconfiguration before any sync runs.

    Runs ``git config <key>`` (without ``--global``) from ``cfg.workspace`` so
    that ``includeIf "gitdir:..."`` conditionals in ``~/.gitconfig`` are
    honoured - a developer who keeps separate identities for different project
    trees (work vs. personal) would otherwise see a false BLOCK even though
    git itself would resolve the right identity during a build.
    """
    name = "git-global-config"
    cwd = str(cfg.workspace) if cfg.workspace.is_dir() else None

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
        hint_lines = [
            f'git config --global {k} "{"you@example.com" if k == "user.email" else "Your Name"}"' for k in missing
        ]
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

    entries: list[tuple[str, str]] = []
    for line in mounts_raw.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        entries.append((fields[1], fields[2]))

    # Sort by mountpoint length descending so the longest (most specific)
    # prefix wins - this handles bind/overlay mounts where multiple parent
    # mountpoints cover the workspace path.
    entries.sort(key=lambda pair: len(pair[0]), reverse=True)

    workspace = cfg.workspace.resolve()
    fstype: str | None = None
    matched_mount: str | None = None
    for mountpoint, kind in entries:
        try:
            mp = Path(mountpoint)
        except TypeError, ValueError:
            continue
        if workspace == mp or workspace.is_relative_to(mp):
            fstype = kind
            matched_mount = mountpoint
            break

    if fstype is None:
        return _skip(
            name,
            Severity.WARN,
            f"no mountpoint covers {workspace} in /proc/mounts",
        )

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
    ccache_dir = cfg.workspace / "ccache"
    if not ccache_dir.exists():
        return _skip(
            name,
            Severity.WARN,
            f"{ccache_dir} absent; ccache populates on first build",
        )

    if shutil.which("ccache") is None:
        return _skip(name, Severity.WARN, "ccache binary not on PATH")

    try:
        out = subprocess.run(
            ["ccache", "--print-stats"],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "CCACHE_DIR": str(ccache_dir)},
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

    not_running_msg = "hashserv configured but daemon is not running"
    not_running_hint = "bakar hashserv start  (bakar build will auto-start it)"

    if not hashserv.is_running(cfg.bsp_root):
        return _fail(name, Severity.WARN, not_running_msg, fix_hint=not_running_hint)

    state_dir = cfg.bsp_root / ".bakar"
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
SHARED_CHECKS: tuple[CheckFunc, ...] = (
    check_host_tools,
    check_docker_daemon,
    check_container_image,
    check_container_os,
    check_container_bitbake,
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
    check_hashserv,
)

# Docker-dependent checks from ``SHARED_CHECKS``. Filtered out of
# :func:`run_all` when ``cfg.host_mode`` is True because plain ``kas``
# does not invoke the container runtime. Keep this list in sync with
# any new container/Docker check added above.
_DOCKER_CHECKS: tuple[CheckFunc, ...] = (
    check_docker_daemon,
    check_container_image,
    check_container_os,
    check_container_bitbake,
    check_docker_ulimits,
    check_docker_version,
    check_docker_storage_driver,
)


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
    return [check(cfg) for check in checks]


def any_blocking_failure(results: list[CheckResult]) -> bool:
    return any(r.severity is Severity.BLOCK and r.status is Status.FAIL for r in results)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SEV_RANK = {Severity.INFO: 0, Severity.WARN: 1, Severity.BLOCK: 2}


def _max_sev(a: Severity, b: Severity) -> Severity:
    return a if _SEV_RANK[a] >= _SEV_RANK[b] else b


def _read_sysctl(key: str) -> int | None:
    path = Path("/proc/sys") / key.replace(".", "/")
    try:
        return int(path.read_text().strip())
    except FileNotFoundError, ValueError:
        return None


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
    for unit in ("B", "K", "M", "G"):
        if num_bytes < 1024:
            return f"{num_bytes:.0f}{unit}"
        num_bytes /= 1024
    return f"{num_bytes:.0f}T"
