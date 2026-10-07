"""Static source-layout invariants of the meta-bakar-mold mold recipes.

The recipes only build inside a Yocto tree, so these tests do not run bitbake.
They pin the contract that decides whether ``LIC_FILES_CHKSUM`` and the cargo
``[patch]`` logic resolve on both supported releases: scarthgap's git fetcher
unpacks to ``git/`` by default while wrynose's unpacks to ``${BP}``, and S is
``${BP}`` under WORKDIR/UNPACKDIR on both.

``mold_3.0.0.bb`` is the recipe in use. ``mold_git.bb`` is kept as a reference
for building an unreleased upstream commit; the tests below pin both, plus the
rule that keeps the reference from ever being selected.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import bakar

_MOLD_DIR = Path(bakar.__file__).parent / "overlays/meta-bakar-mold/recipes-devtools/mold"
_RECIPE = _MOLD_DIR / "mold_git.bb"
_RELEASE = _MOLD_DIR / "mold_3.0.0.bb"


def _src_uri_entries(recipe: Path = _RECIPE) -> dict[str, dict[str, str]]:
    """Return ``{name: {param: value}}`` per SRC_URI entry; an unnamed one is keyed by its repo basename."""
    text = recipe.read_text()
    block = re.search(r'^SRC_URI = "\\\n(.*?)^"', text, re.DOTALL | re.MULTILINE)
    assert block, f"SRC_URI block not found in {recipe.name}"
    entries: dict[str, dict[str, str]] = {}
    for line in block.group(1).splitlines():
        parts = line.strip().rstrip("\\").strip().split(";")
        params = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
        key = params.get("name") or parts[0].rsplit("/", 1)[-1].removesuffix(".git")
        if parts[0]:
            entries[key] = params
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


@pytest.mark.unit
def test_release_recipe_is_not_deprioritised() -> None:
    """The recipe in use must not carry the negative preference the reference recipe sets."""
    assert not re.search(r"^DEFAULT_PREFERENCE\s*=", _RELEASE.read_text(), re.MULTILINE)


@pytest.mark.unit
def test_reference_recipe_is_never_selected_by_default() -> None:
    """A negative DEFAULT_PREFERENCE keeps mold_git.bb from outranking the release after a PV bump."""
    assert re.search(r'^DEFAULT_PREFERENCE = "-1"$', _RECIPE.read_text(), re.MULTILINE)


@pytest.mark.unit
def test_release_checkout_is_placed_by_subdir_bp_without_destsuffix() -> None:
    """subdir=${BP} puts the checkout in S on scarthgap; destsuffix would add a self-[patch] entry."""
    mold = _src_uri_entries(_RELEASE)["mold"]
    assert mold.get("subdir") == "${BP}"
    assert "destsuffix" not in mold


@pytest.mark.unit
def test_release_source_is_pinned_git_not_a_github_archive() -> None:
    """oe-core's src-uri-bad is an error on wrynose for github.com/.../archive/ URLs."""
    text = _RELEASE.read_text()
    src_uri = re.search(r'^SRC_URI = "\\\n(.*?)^"', text, re.DOTALL | re.MULTILINE)
    assert src_uri, "SRC_URI block not found in mold_3.0.0.bb"
    assert "/archive/" not in src_uri.group(1)
    assert "codeload.github.com" not in src_uri.group(1)
    assert re.search(r'^SRCREV = "[0-9a-f]{40}"$', text, re.MULTILINE)
    assert re.search(r'^SRCREV_mimalloc = "[0-9a-f]{40}"$', text, re.MULTILINE)


@pytest.mark.unit
def test_release_mimalloc_entry_keeps_name_and_destsuffix() -> None:
    """The release recipe's [patch] entry for mimalloc_rust comes from this name+destsuffix pair."""
    assert _src_uri_entries(_RELEASE)["mimalloc"].get("destsuffix") == "mimalloc_rust"


@pytest.mark.unit
def test_release_recipe_does_not_assign_s() -> None:
    """An explicit S = "${WORKDIR}/git" is a fatal QA error on wrynose, where S is under UNPACKDIR."""
    assert not re.search(r"^S\s*[:?+.]*=", _RELEASE.read_text(), re.MULTILINE)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("recipe", "required", "crates"),
    [
        (_RELEASE, "${BPN}-crates.inc", "mold-crates.inc"),
        (_RECIPE, "mold-git-crates.inc", "mold-git-crates.inc"),
    ],
)
def test_each_recipe_requires_its_own_crate_list(recipe: Path, required: str, crates: str) -> None:
    """update_crates writes ${BPN}-crates.inc, so only the release recipe may use that name."""
    assert re.search(rf"^require {re.escape(required)}$", recipe.read_text(), re.MULTILINE)
    assert (_MOLD_DIR / crates).is_file()


@pytest.mark.unit
@pytest.mark.parametrize("crates", ["mold-crates.inc", "mold-git-crates.inc"])
def test_every_crate_has_a_checksum(crates: str) -> None:
    """A crate:// entry without a matching sha256sum fails bitbake's fetch check at parse time."""
    text = (_MOLD_DIR / crates).read_text()
    uris = set(re.findall(r"crate://crates\.io/(\S+?)/(\S+?) \\", text))
    sums = set(re.findall(r"^SRC_URI\[(\S+?)\.sha256sum\]", text, re.MULTILINE))
    assert uris, f"no crate:// entries in {crates}"
    assert {f"{name}-{ver}" for name, ver in uris} == sums
