"""PSI (Pressure Stall Information) helpers.

Reads ``/proc/pressure/<resource>`` and turns measured peaks into bitbake
``BB_PRESSURE_MAX_*`` thresholds for ``[build] psi_autocalibrate``, which
writes the values after each build.
"""

from __future__ import annotations

import math
from pathlib import Path

PSI_DIMS = ("cpu", "io", "memory")
PSI_CLAMP = 95
PSI_MEMORY_FLOOR = 20
PSI_HEADROOM = 0.20


def read_psi_avg10(resource: str) -> float | None:
    """Return the ``some avg10=`` value from ``/proc/pressure/<resource>``.

    Returns None when the file or field is missing - covers kernels without PSI
    support and containers where ``/proc/pressure`` is not exposed.
    """
    try:
        text = Path(f"/proc/pressure/{resource}").read_text(encoding="utf-8")
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


def psi_recommendation(peaks: dict[str, float]) -> dict[str, int]:
    """Convert measured peak avg10 values to recommended config thresholds.

    Adds PSI_HEADROOM, clamps each dimension to PSI_CLAMP, and floors the memory
    recommendation to PSI_MEMORY_FLOOR so it is never zero.
    """
    result: dict[str, int] = {}
    for dim, peak in peaks.items():
        value = math.ceil(peak * (1 + PSI_HEADROOM))
        value = max(value, PSI_MEMORY_FLOOR if dim == "memory" else 1)
        value = min(value, PSI_CLAMP)
        result[dim] = value
    return result


def plan_autocalibration(peaks: dict[str, float], current: dict[str, float | None]) -> dict[str, int]:
    """Decide which ``pressure_max_*`` values to write after a build.

    Ratchet-up-only: the thresholds approximate machine tolerance, which does
    not shrink when a particular build happens to be light (sstate-cached
    rebuilds, small BSPs sharing the global config). Lowering on such a build
    would over-throttle the next cold build - and the throttle then caps the
    very peaks that could correct the value, so a lowered threshold sticks.

    - Skip a dimension with no measured pressure (peak <= 0): nothing to learn.
    - Write the recommendation when the threshold is unset (bootstrap).
    - Raise when the build was NOT throttled on that dimension (the peak
      stayed below the configured ceiling, so it reflects real demand rather
      than the ceiling) and the recommendation exceeds the current value.
      A throttled build's measurement is circular and is never learned from.
    - Never lower. To recalibrate from scratch (e.g. after a hardware
      change), delete the pressure_max_* keys from config.toml; the next
      build re-bootstraps them.
    """
    rec = psi_recommendation(peaks)
    plan: dict[str, int] = {}
    for dim, peak in peaks.items():
        if peak <= 0:
            continue
        cur = current.get(dim)
        if cur is None or (peak < cur and rec[dim] > cur):
            plan[dim] = rec[dim]
    return plan


def apply_autocalibration(
    current: dict[str, float | None],
    peaks: dict[str, float],
    config_path: Path | None = None,
) -> dict[str, int]:
    """Write the planned ``pressure_max_*`` changes to config and return them.

    Returns the ``{dim: value}`` map that was written (empty when nothing
    changed). Delegates the write to :func:`bakar.user_config.set_setting` so the
    atomic-write and schema validation are shared with the ``settings`` command.
    """
    from bakar.user_config import set_setting

    plan = plan_autocalibration(peaks, current)
    for dim, value in plan.items():
        set_setting(f"build.pressure_max_{dim}", str(value), config_path)
    return plan
