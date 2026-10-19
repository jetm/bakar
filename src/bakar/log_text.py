"""Turn a line of build output into bounded, display-safe text.

Single definition shared by every reader of kas.log and the build UI, so a
status line and a recorded failure reason clean text the same way. Stdlib only.
"""

from __future__ import annotations

import re

MAX_LINE_CHARS = 200

_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
# C0, DEL, C1, plus bidi marks/embeddings/isolates and the line/paragraph separators.
_INVISIBLE = (0x061C, 0x200E, 0x200F, 0x2028, 0x2029, *range(0x202A, 0x202F), *range(0x2066, 0x206A))
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f" + "".join(map(chr, _INVISIBLE)) + "]")

# bitbake: "ERROR: msg"; kas logging: "<date> <time> - ERROR    - msg".
_ERROR_LINE = re.compile(r"^(?:\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} - )?ERROR(?::\s*|\s+-\s+)(\S.*)$")


def clean_text(text: str) -> str:
    """Strip escape sequences, control, bidi and line-separator characters."""
    text = _ANSI_OSC.sub("", _ANSI_CSI.sub("", text))
    return _CONTROL.sub("", text.replace("\t", " "))


def clean_line(raw: str) -> str:
    """Return the last carriage-return segment of ``raw``, cleaned and bounded."""
    raw = raw.rstrip("\r").rsplit("\r", 1)[-1]
    return clean_text(raw).strip()[:MAX_LINE_CHARS]


def error_line(line: str) -> str | None:
    """Return ``ERROR: <message>`` when ``line`` is a bitbake or kas error line."""
    text = clean_text(line).rstrip()
    match = _ERROR_LINE.match(text)
    if match is None:
        return None
    return ("ERROR: " + match.group(1))[:MAX_LINE_CHARS]
