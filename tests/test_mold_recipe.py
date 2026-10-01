"""Static source-layout invariants of the meta-bakar-mold mold recipe.

The recipe only builds inside a Yocto tree, so these tests do not run bitbake.
They pin the contract that decides whether ``LIC_FILES_CHKSUM`` and the cargo
``[patch]`` logic resolve on both supported releases: scarthgap's git fetcher
unpacks to ``git/`` by default while wrynose's unpacks to ``${BP}``, and S is
``${BP}`` under WORKDIR/UNPACKDIR on both.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import bakar

_RECIPE = Path(bakar.__file__).parent / "overlays/meta-bakar-mold/recipes-devtools/mold/mold_git.bb"


def _src_uri_entries() -> dict[str, dict[str, str]]:
    """Return ``{name: {param: value}}`` for each SRC_URI entry carrying ``name=``."""
    text = _RECIPE.read_text()
    block = re.search(r'^SRC_URI = "\\\n(.*?)^"', text, re.DOTALL | re.MULTILINE)
    assert block, "SRC_URI block not found in mold_git.bb"
    entries: dict[str, dict[str, str]] = {}
    for line in block.group(1).splitlines():
        parts = line.strip().rstrip("\\").strip().split(";")
        params = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
        if "name" in params:
            entries[params["name"]] = params
    return entries


@pytest.mark.unit
def test_mold_checkout_is_placed_by_subdir_bp() -> None:
    """The mold checkout lands in ${BP}, where the default S looks for LICENSE.

    Without it scarthgap unpacks to git/ and do_populate_lic fails on
    LIC_FILES_CHKSUM against the empty ${WORKDIR}/${BP}.
    """
    assert _src_uri_entries()["mold"].get("subdir") == "${BP}"


@pytest.mark.unit
def test_mold_checkout_sets_no_destsuffix() -> None:
    """name= plus destsuffix= makes cargo_common_do_patch_paths [patch] the mold repo onto itself."""
    assert "destsuffix" not in _src_uri_entries()["mold"]


@pytest.mark.unit
def test_mimalloc_entry_keeps_name_and_destsuffix() -> None:
    """The mimalloc_rust [patch] entry is generated from exactly this name+destsuffix pair."""
    mimalloc = _src_uri_entries()["mimalloc"]
    assert mimalloc.get("destsuffix") == "mimalloc_rust"


@pytest.mark.unit
def test_recipe_does_not_assign_s() -> None:
    """An explicit S = "${WORKDIR}/git" is a fatal QA error on wrynose, where S is under UNPACKDIR."""
    assert not re.search(r"^S\s*[:?+.]*=", _RECIPE.read_text(), re.MULTILINE)
