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


def one_line(text: str) -> str:
    """Collapse ``text`` to one clean line, every whitespace run one space, unbounded."""
    # The second split collapses a space pair left behind by a removed control character.
    return " ".join(clean_text(" ".join(text.split())).split())


def join_bounded(entries: list[str], *, limit: int = MAX_LINE_CHARS, sep: str = "; ", marker: str = "...") -> str:
    """Join ``entries`` with ``sep`` in at most ``limit`` characters, shortening the longest fairly.

    Entries are served shortest first, each taking at most an equal share of what
    is left, so a short entry stays whole and passes its unused room on. A
    shortened entry ends with ``marker``.
    """
    if not entries:
        return ""
    joined = sep.join(entries)
    if len(joined) <= limit:
        return joined
    budget = limit - len(sep) * (len(entries) - 1)
    if budget < len(entries):
        return joined[:limit]
    takes = [0] * len(entries)
    remaining = budget
    order = sorted(range(len(entries)), key=lambda i: (len(entries[i]), i))
    for served, index in enumerate(order):
        takes[index] = min(len(entries[index]), remaining // (len(entries) - served))
        remaining -= takes[index]
    rendered = []
    for entry, take in zip(entries, takes, strict=True):
        if take >= len(entry):
            rendered.append(entry)
        elif take > len(marker):
            rendered.append(entry[: take - len(marker)].rstrip() + marker)
        else:
            rendered.append(entry[:take])
    return sep.join(rendered)
