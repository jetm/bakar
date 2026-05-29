"""Populate `ti/sources/` via the `oe-layertool-setup.sh` script.

Mirrors the role of :mod:`bakar.steps.repo` for the NXP side, but
TI BSP delivery uses the `varigit/oe-layersetup` script (a
shell wrapper around sequential `git clone` + checkout against pinned
SHAs in a `processor-sdk-*-config_var<N>.txt` config file) instead of
Google `repo`.

The script writes ``sources/<layer>/`` checkouts for every layer the
config pins and drops a ``build/conf/{local,bblayers}.conf`` skeleton
next to the sources tree. We invoke it with ``-d <DL_DIR>`` so the
tiered downloads cache is reused, and run it from ``cfg.workspace /
"ti"`` so the script's default ``$(pwd)``-rooted ``sources/`` and
``build/conf/`` land directly under ``ti/`` (not under
``ti/oe-layertool/``).

After a successful run we record the active config name in
``ti/conf/active-config.txt`` so future ``bakar build`` invocations
can skip the script when the requested config already matches.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from bakar.config import BuildConfig
    from bakar.observability import RunLogger


def _record_active_config(cfg: BuildConfig) -> None:
    """Atomically write the just-applied config name to ``ti/conf/active-config.txt``."""
    conf_dir = cfg.workspace / "ti" / "conf"
    conf_dir.mkdir(parents=True, exist_ok=True)
    target = conf_dir / "active-config.txt"
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(cfg.manifest + "\n")
    tmp.replace(target)


def _build_layertool_cmd(cfg: BuildConfig, *, force_init: bool = False) -> list[str]:
    """Assemble the argv for ``oe-layertool-setup.sh``.

    ``-f`` is the config relative to the ``oe-layertool`` checkout (the
    script hardcodes ``scriptdir=$(pwd)``), ``-b ..`` redirects the
    generated ``sources/`` and ``build/`` to live under ``ti/``, and
    ``-d`` points the script at the tiered downloads cache so it is
    reused instead of recreated under the layertool checkout.

    ``-r`` is appended when ``force_init`` is True, asking the script to
    reset every checkout to the SHA pinned by the config file.
    """
    ti_root = cfg.workspace / "ti"
    layertool_dir = ti_root / "oe-layertool"
    config_path = ti_root / "oe-layertool" / "configs" / "variscite" / cfg.manifest
    dl_dir = os.environ.get("DL_DIR", "/tmp/yocto-downloads")
    config_rel = str(config_path.relative_to(layertool_dir))
    cmd: list[str] = [
        "bash",
        "./oe-layertool-setup.sh",
        "-f",
        config_rel,
        "-b",
        str(ti_root),
        "-d",
        dl_dir,
    ]
    if force_init:
        cmd.append("-r")  # reset all checkouts
    return cmd


def _strip_dl_dir(local_conf: Path) -> None:
    """Remove any ``DL_DIR`` assignment lines from ``local.conf`` in place.

    oe-layertool seeds ``local.conf`` with a ``DL_DIR`` line pointed at
    its own cache; the kas tuning block is authoritative and the
    in-container ``DL_DIR`` resolves via env passthrough, not via
    ``local.conf``. No-op when the file is absent or contains no
    matching line.
    """
    if not local_conf.is_file():
        return
    text = local_conf.read_text(encoding="utf-8")
    new = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("DL_DIR"))
    if new != text:
        local_conf.write_text(new + ("\n" if not new.endswith("\n") else ""))


def populate(
    cfg: BuildConfig,
    log: RunLogger,
    *,
    force_init: bool = False,
) -> None:
    """Run ``oe-layertool-setup.sh`` to populate ``ti/sources/``.

    Skips re-running the script when ``ti/sources/oe-core/oe-init-build-env``
    already exists AND the tracked config matches ``cfg.manifest`` -
    unless ``force_init`` is True.
    """
    log.step_start(
        "ti_layertool",
        manifest=cfg.manifest,
        force_init=force_init,
    )
    ti_root = cfg.workspace / "ti"
    script = ti_root / "oe-layertool" / "oe-layertool-setup.sh"
    if not script.is_file():
        raise FileNotFoundError(f"{script} missing - clone varigit/oe-layersetup at master_var01 first.")

    config_path = ti_root / "oe-layertool" / "configs" / "variscite" / cfg.manifest
    if not config_path.is_file():
        raise FileNotFoundError(f"{config_path} missing - check the manifest filename and the oe-layertool branch.")

    sources_marker = ti_root / "sources" / "oe-core" / "oe-init-build-env"
    tracked = ti_root / "conf" / "active-config.txt"
    already_applied = (
        sources_marker.is_file() and tracked.is_file() and tracked.read_text(encoding="utf-8").strip() == cfg.manifest
    )
    if already_applied and not force_init:
        log.step_skip("ti_layertool", reason=f"{cfg.manifest} already applied")
        return

    # oe-layertool-setup.sh hardcodes ``scriptdir=$(pwd)`` at the top
    # and looks for ``$scriptdir/git_retry.sh``, so it MUST be invoked
    # from its own directory.
    layertool_dir = ti_root / "oe-layertool"
    cmd = _build_layertool_cmd(cfg, force_init=force_init)
    subprocess.run(cmd, cwd=layertool_dir, check=True)  # pragma: no cover

    if not sources_marker.is_file():
        raise RuntimeError(f"{sources_marker} missing after oe-layertool-setup.sh; check the script output above.")

    _strip_dl_dir(ti_root / "build" / "conf" / "local.conf")

    _record_active_config(cfg)

    sources_count = (
        len([p for p in (ti_root / "sources").iterdir() if p.is_dir()]) if (ti_root / "sources").is_dir() else 0
    )
    log.step_ok("ti_layertool", sources=sources_count, config=cfg.manifest)


def reset_sources(cfg: BuildConfig) -> None:
    """Remove ``ti/sources/`` and ``ti/conf/active-config.txt``.

    Convenience helper for users who want to force a from-scratch
    populate; not called from the build pipeline. Equivalent of
    ``rm -rf`` followed by a fresh ``oe-layertool-setup.sh``.
    """
    ti_root = cfg.workspace / "ti"
    sources = ti_root / "sources"
    if sources.is_dir():
        shutil.rmtree(sources)
    tracked = ti_root / "conf" / "active-config.txt"
    if tracked.is_file():
        tracked.unlink()
