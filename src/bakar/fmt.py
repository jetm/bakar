"""Shared formatting utilities."""

from __future__ import annotations


def fmt_duration(seconds: float) -> str:
    """Format an elapsed duration compactly: ``42s``, ``24m31s``, ``1h02m``.

    Matches the build UI's global-timer style so a reported build time reads the
    same as the live ``󰦗`` clock. Sub-minute shows seconds; under an hour shows
    ``m``/``s``; an hour or more shows ``h``/``m`` (seconds dropped).
    """
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    m, s = divmod(total, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def fmt_bytes(n: float) -> str:
    """Format a byte count as a compact human-readable string.

    Uses SI-style single-letter suffixes without spaces (``"K"``, ``"M"``,
    ``"G"``, ``"T"``). Rounds to the nearest integer at each magnitude.

    Examples::

        fmt_bytes(512)          # "512B"
        fmt_bytes(1_536)        # "1K"
        fmt_bytes(220_000_000)  # "209M"
        fmt_bytes(2_200_000_000)  # "2G"
    """
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}T"


def fmt_bytes_iec(n_bytes: float) -> str:
    """Format a byte count using IEC binary prefixes.

    Uses IEC suffixes (``"B"``, ``"KiB"``, ``"MiB"``, ``"GiB"``, ``"TiB"``)
    with one decimal place and a space separator. Float division preserves
    sub-KiB precision.

    Examples::

        fmt_bytes_iec(500)        # "500.0 B"
        fmt_bytes_iec(2_048)      # "2.0 KiB"
        fmt_bytes_iec(5_242_880)  # "5.0 MiB"
    """
    n: float = float(n_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"
