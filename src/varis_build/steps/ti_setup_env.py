"""Reconcile ``ti/build/conf/local.conf`` with the requested machine/distro.

Mirrors the role of :mod:`varis_build.steps.setup_env` for the NXP
side. ``oe-layertool-setup.sh`` writes a ``local.conf`` next to the
sources tree; this step idempotently overrides ``MACHINE``,
``DISTRO``, and strips any leftover ``DL_DIR`` line so the kas tuning
block stays authoritative.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from varis_build.config import BuildConfig
    from varis_build.observability import RunLogger


_MACHINE_RE = re.compile(r"^\s*MACHINE\s*\??=", re.MULTILINE)
_DISTRO_RE = re.compile(r"^\s*DISTRO\s*\??=", re.MULTILINE)
_DL_DIR_RE = re.compile(r"^\s*DL_DIR\b", re.MULTILINE)


def _set_or_replace(text: str, key: str, value: str, key_re: re.Pattern[str]) -> str:
    """Replace existing ``KEY ?= "..."`` / ``KEY = "..."`` lines, or
    append a fresh assignment when none exists.
    """
    new_line = f'{key} = "{value}"'
    lines = text.splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if key_re.match(line):
            if not replaced:
                out.append(new_line)
                replaced = True
            # Drop any duplicate KEY assignments after the first replacement.
            continue
        out.append(line)
    if not replaced:
        out.append(new_line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def run(cfg: BuildConfig, log: RunLogger) -> None:
    """Override MACHINE and DISTRO in ``ti/build/conf/local.conf``."""
    log.step_start("ti_setup_env", machine=cfg.machine, distro=cfg.distro)
    local_conf = cfg.bsp_root / "build" / "conf" / "local.conf"
    if not local_conf.is_file():
        raise FileNotFoundError(f"{local_conf} missing - did oe-layertool-setup.sh complete?")

    text = local_conf.read_text(encoding="utf-8")
    text = _set_or_replace(text, "MACHINE", cfg.machine, _MACHINE_RE)
    text = _set_or_replace(text, "DISTRO", cfg.distro, _DISTRO_RE)
    # Strip any DL_DIR line; the kas tuning block sets it inside the container.
    text = "\n".join(line for line in text.splitlines() if not _DL_DIR_RE.match(line)) + (
        "\n" if text.endswith("\n") else ""
    )
    local_conf.write_text(text)

    if not cfg.bblayers_conf.is_file():
        raise RuntimeError(f"{cfg.bblayers_conf} missing; oe-layertool-setup.sh did not write bblayers.conf.")
    log.step_ok("ti_setup_env", local_conf=str(local_conf))
