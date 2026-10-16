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


def _workspace_table_keys() -> list[tuple[tuple[str, ...], str]]:
    return sorted((section, key) for section, mapping, _extra in workspace_config._WORKSPACE_TABLES for key in mapping)


def test_workspace_tables_are_nonempty() -> None:
    assert len(_workspace_table_keys()) > 10, "workspace table map is nearly empty; the test would pass vacuously"


@pytest.mark.parametrize(
    ("section", "key"),
    _workspace_table_keys(),
    ids=lambda v: ".".join(v) if isinstance(v, tuple) else v,
)
def test_workspace_table_key_has_reference_row(doc_text: str, section: tuple[str, ...], key: str) -> None:
    body = _subsection(_top_level_section(doc_text, WORKSPACE_HEADING), section)
    assert _has_row(body, key), f"workspace [{'.'.join(section)}] key `{key}` has no row in {DOC.name}"


SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "bakar"

# Names the source reads that are deliberately absent from config-reference.md.
# Every entry needs a reason; an undocumented variable must not hide here
# without one.
_UNDOCUMENTED_ENV: dict[str, str] = {
    "BAKAR_EOF": "heredoc delimiter in generated setup scripts, not an environment variable",
    "BAKAR_SSTATE_MIRROR_URL": "exported by bakar into the build from `sstate_mirror_url`, documented in docs/build.md",
    "BAKAR_SCCACHE_SCHEDULER_URL": "exported by bakar into the build from `sccache_scheduler_url`, see sccache-dist.md",
    "BAKAR_REPO_NAME": "exported to the child command by `bakar for-all`, documented in docs/for-all.md",
    "BAKAR_REPO_PATH": "exported to the child command by `bakar for-all`, documented in docs/for-all.md",
    "BAKAR_REPO_COMMIT": "exported to the child command by `bakar for-all`, documented in docs/for-all.md",
}

_ENV_LITERAL = re.compile(r"""["'](BAKAR_[A-Z0-9_]+)["']""")


def _env_names_read_by_source() -> set[str]:
    """Every ``BAKAR_*`` name that appears as a whole string literal in src.

    Reads go through ``pick(...)``, ``os.environ``, ``environ.get``, name
    constants and per-field tables, so matching the literal itself is the only
    scan that does not miss a spelling.
    """
    names: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        names.update(_ENV_LITERAL.findall(path.read_text(encoding="utf-8")))
    return names


def test_env_var_scan_finds_names() -> None:
    names = _env_names_read_by_source()
    assert {"BAKAR_MACHINE", "BAKAR_BITBAKE_OVERRIDE"} <= names, "scan regex no longer matches env reads"


@pytest.mark.parametrize("name", sorted(_env_names_read_by_source()))
def test_env_var_is_documented(doc_text: str, name: str) -> None:
    if name in _UNDOCUMENTED_ENV:
        return
    assert re.search(rf"`{re.escape(name)}`", doc_text), f"{name} is read by src but has no entry in {DOC.name}"
