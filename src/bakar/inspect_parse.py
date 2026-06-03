"""Pure parsers for bitbake -e and layer.conf output.

No Typer, no subprocess, no I/O.  Every function takes text as input and
returns a plain Python value.  The three public functions form a contract
consumed by getvar.py, inspect.py, and layers.py command modules.

Functions
---------
extract_var_history(env_text, var)
    Return the ordered list of ``# set <file>:<line>`` / ``# line: N, file:``
    source locations preceding a variable's assignment in ``bitbake -e`` text.

parse_env_vars(env_text, names)
    Extract named shell variable values from a ``bitbake -e`` dump.

parse_layer_conf(text)
    Extract BBFILE_PRIORITY, LAYERSERIES_COMPAT, and LAYERVERSION from
    layer.conf text.
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# extract_var_history
# ---------------------------------------------------------------------------

# Matches a history-block header line: "#\n# $VARNAME" or "# $VARNAME [N ops]"
_VAR_HEADER_RE = re.compile(r"^# \$([A-Za-z0-9_:]+)(?:\s+\[\d+ operations\])?$")

# Matches an operation line inside a history block:
#   "#   set /path/to/file.conf:42"
#   "#   append /path/to/file.conf:1"
#   "#   override[machine]:set /path/to/file.conf:3"
_OP_LINE_RE = re.compile(r"^#\s+\S+\s+(\S+:\d+)\s*(?:\[.*\])?$")

# Matches the shell-assignment line that closes the history block:
#   MACHINE="imx8mp-lpddr4-evk"
# Note: variable names can contain underscores but not spaces.
_ASSIGN_RE = re.compile(r"^([A-Za-z0-9_]+)=")

# Matches the sentinel "[no history recorded]" line.
_NO_HISTORY_RE = re.compile(r"#\s+\[no history recorded\]")


def extract_var_history(env_text: str, var: str) -> list[str]:
    """Return source locations from the ``bitbake -e`` history block for *var*.

    Scans *env_text* for the history comment block that precedes the shell
    assignment of *var* and returns the ordered ``file:line`` strings from
    each operation line (e.g. ``"/path/to/local.conf:5"``).

    Returns an empty list - not raising - when:
    - *var* is not found in *env_text*.
    - The history block is present but contains ``[no history recorded]``.
    - No ``#   <op> <file>:<line>`` lines precede the assignment.
    """
    lines = env_text.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        m = _VAR_HEADER_RE.match(line)
        if m and m.group(1) == var:
            # Found the header for this variable.  Collect op lines until we
            # hit the shell assignment or a line that resets the block.
            locations: list[str] = []
            j = i + 1
            while j < n:
                candidate = lines[j]
                # Shell assignment closes the block.
                am = _ASSIGN_RE.match(candidate)
                if am:
                    if am.group(1) == var:
                        return locations
                    # Different variable's assignment - block ended without
                    # finding our assignment (shouldn't happen with well-formed
                    # bitbake -e output, but be defensive).
                    return locations
                # No-history sentinel -> return empty list.
                if _NO_HISTORY_RE.search(candidate):
                    return []
                # Operation line.
                om = _OP_LINE_RE.match(candidate)
                if om:
                    locations.append(om.group(1))
                j += 1
            # Reached EOF inside the block.
            return locations
        i += 1
    return []


# ---------------------------------------------------------------------------
# parse_env_vars
# ---------------------------------------------------------------------------

# Matches: VARNAME="value" (possibly with escaped quotes or backslash newlines
# collapsed to a single logical line by bitbake's emit_var).
# We capture the raw value between the outer double-quotes.
_SHELL_VAR_RE = re.compile(r'^([A-Za-z0-9_]+)="(.*)"$', re.MULTILINE)


def parse_env_vars(env_text: str, names: list[str]) -> dict[str, str]:
    """Extract named variable values from a ``bitbake -e`` dump.

    Scans *env_text* for shell assignments of the form ``NAME="value"`` and
    returns a dict mapping each found name to its value (with escaped double
    quotes unescaped).  Names not found in *env_text* are omitted.

    The function reads the *last* occurrence when a name appears more than
    once (which can happen with overlays), matching what would be exported.

    Parameters
    ----------
    env_text:
        Full text from ``bitbake -e`` or ``bitbake -e <recipe>``.
    names:
        Variable names to extract, e.g. ``["PN", "PV", "WORKDIR"]``.
    """
    want = set(names)
    result: dict[str, str] = {}
    for m in _SHELL_VAR_RE.finditer(env_text):
        name = m.group(1)
        if name in want:
            # Unescape \" -> " as emitted by bitbake's emit_var.
            value = m.group(2).replace('\\"', '"')
            result[name] = value
    return result


# ---------------------------------------------------------------------------
# parse_layer_conf
# ---------------------------------------------------------------------------

# Matches:  BBFILE_PRIORITY_<collection> = "N"
# Collection names may contain hyphens (e.g. meta-example, perl-layer).
_PRIORITY_RE = re.compile(r'^BBFILE_PRIORITY_[\w-]+\s*=\s*"(\d+)"', re.MULTILINE)
# Matches:  LAYERSERIES_COMPAT_<collection> = "val1 val2 ..."
_COMPAT_RE = re.compile(r'^LAYERSERIES_COMPAT_[\w-]+\s*=\s*"([^"]*)"', re.MULTILINE)
# Matches:  LAYERVERSION_<collection> = "N"
_VERSION_RE = re.compile(r'^LAYERVERSION_[\w-]+\s*=\s*"([^"]*)"', re.MULTILINE)


def parse_layer_conf(text: str) -> dict[str, str]:
    """Extract key fields from layer.conf text.

    Returns a dict with up to three keys:

    - ``"BBFILE_PRIORITY"``: numeric priority string (e.g. ``"6"``).
    - ``"LAYERSERIES_COMPAT"``: space-separated compatible Yocto releases
      (e.g. ``"scarthgap wrynose"``).
    - ``"LAYERVERSION"``: version string (e.g. ``"3"``).

    Returns ``{}`` - not raising - on empty or malformed input.
    """
    if not text or not text.strip():
        return {}

    result: dict[str, str] = {}
    try:
        m = _PRIORITY_RE.search(text)
        if m:
            result["BBFILE_PRIORITY"] = m.group(1)

        m = _COMPAT_RE.search(text)
        if m:
            result["LAYERSERIES_COMPAT"] = m.group(1)

        m = _VERSION_RE.search(text)
        if m:
            result["LAYERVERSION"] = m.group(1)
    except Exception:
        return {}

    return result
