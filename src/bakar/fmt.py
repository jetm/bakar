"""Shared formatting utilities."""

from __future__ import annotations

import re

# Characters stripped from untrusted text before it reaches a markup-enabled
# Rich renderer: C0/DEL/C1 controls, lone surrogates (undecodable filesystem
# bytes), Unicode ``Cf`` format characters (bidi overrides, zero-width), the
# U+2028/U+2029 line breaks Rich renders as row splits, and U+2022 (the bullet
# elfscan uses as its report entry separator). ``re`` has no category escape,
# so the ``Cf`` ranges are spelled out.
_CONTROL_RE = re.compile(
    "["
    "\x00-\x1f\x7f-\x9f"
    "\ud800-\udfff"
    "\\u00ad\\u0600-\\u0605\\u061c\\u06dd\\u070f\\u0890-\\u0891\\u08e2\\u180e"
    "\\u200b-\\u200f\\u202a-\\u202e\\u2060-\\u2064\\u2066-\\u206f\\ufeff\\ufff9-\\ufffb"
    "\\U000110bd\\U000110cd\\U00013430-\\U0001343f\\U0001bca0-\\U0001bca3\\U0001d173-\\U0001d17a"
    "\\U000e0001\\U000e0020-\\U000e007f"
    "\\u2028\\u2029"
    "\\u2022"
    "]"
)

# The bound's elision takes the MIDDLE so a long path keeps both its directory
# and its file name.
_NEUTRALIZE_LIMIT = 1024
_ELISION = "..."


def neutralize(value: object) -> str:
    """Render an untrusted value safe to print through a markup-enabled Rich console.

    A name such as ``foo[/]bar`` raises ``MarkupError``, ``[on red blink]``
    forges formatting, and an ESC rewrites the terminal title. Three defences,
    in this order: strip the characters ``_CONTROL_RE`` names, bound the length
    to 1024 characters, then escape markup - escaping last so the backslashes
    it inserts are neither stripped nor counted against the bound.
    """
    # Deferred: bakar.steps.build_ui imports this module, and only the renderer may load rich.
    from rich.markup import escape

    text = _CONTROL_RE.sub("", str(value))
    if len(text) > _NEUTRALIZE_LIMIT:
        keep = _NEUTRALIZE_LIMIT - len(_ELISION)
        head = keep // 2
        text = text[:head] + _ELISION + text[len(text) - (keep - head) :]
    return escape(text)


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
