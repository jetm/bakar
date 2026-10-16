"""Every user-settable config key must have a row in docs/config-reference.md.

The key lists come from the code (``SETTINGS_SCHEMA`` for ``config.toml`` and
the workspace ``[build]`` key map for ``.bakar.toml``), so a key added to the
schema without a documentation row fails here by name.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bakar import workspace_config
from bakar.user_config import SETTINGS_SCHEMA

DOC = Path(__file__).resolve().parent.parent / "docs" / "config-reference.md"
USER_HEADING = "## `~/.config/bakar/config.toml`"
WORKSPACE_HEADING = "## Workspace `.bakar.toml`"


def _top_level_section(text: str, heading: str) -> str:
    """Return the body of the ``##`` section whose heading line is ``heading``."""
    parts = re.split(r"^(?=## )", text, flags=re.MULTILINE)
    for part in parts:
        if part.splitlines()[0].strip() == heading:
            return part
    raise AssertionError(f"{DOC.name} has no section {heading!r}")


def _subsection(section: str, table: tuple[str, ...]) -> str:
    """Return the body of the ``###`` subsection documenting ``table``."""
    label = f"`[{'.'.join(table)}]`"
    for part in re.split(r"^(?=### )", section, flags=re.MULTILINE):
        first = part.splitlines()[0]
        if first.startswith("### ") and first[4:].split(" - ")[0].strip() == label:
            return part
    raise AssertionError(f"{DOC.name} has no subsection {label} in the expected section")


def _has_row(body: str, key: str) -> bool:
    return re.search(rf"^\| `{re.escape(key)}` \|", body, flags=re.MULTILINE) is not None


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


@pytest.mark.parametrize("dotted", sorted(SETTINGS_SCHEMA))
def test_settings_key_has_reference_row(doc_text: str, dotted: str) -> None:
    spec = SETTINGS_SCHEMA[dotted]
    body = _subsection(_top_level_section(doc_text, USER_HEADING), spec.section)
    assert _has_row(body, spec.key), f"{dotted}: no row for `{spec.key}` under [{'.'.join(spec.section)}] in {DOC.name}"


def test_workspace_build_keys_are_nonempty() -> None:
    assert workspace_config._BUILD_KEYS, "workspace [build] key map is empty; the test would pass vacuously"


@pytest.mark.parametrize("key", sorted(workspace_config._BUILD_KEYS))
def test_workspace_build_key_has_reference_row(doc_text: str, key: str) -> None:
    body = _subsection(_top_level_section(doc_text, WORKSPACE_HEADING), ("build",))
    assert _has_row(body, key), f"workspace [build] key `{key}` has no row in {DOC.name}"
