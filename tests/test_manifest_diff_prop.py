"""Property-based tests for :func:`bakar.manifest_diff.diff_manifests`.

All cases pin ``checkout_root=None`` (the default), so no git subprocess runs
and the diff is computed purely from the parsed manifest XML, keeping every
case hermetic.

The synthetic manifests use the ``path`` attribute and 40-char hex SHAs
because :func:`bakar.workspace.parse_manifest_pins` (which ``diff_manifests``
delegates to) reads ``proj.getAttribute("path")`` and keeps only revisions
matching ``^[0-9a-fA-F]{40}$``. A ``name=``/short-SHA manifest parses to an
empty pin map, so the diff would be trivially empty and exercise nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bakar.manifest_diff import diff_manifests

if TYPE_CHECKING:
    from pathlib import Path

# Layer paths: identifier-ish strings using letters, digits, and the path
# punctuation real repo-tool project paths contain (e.g. "sources/meta-foo").
_layer_names = st.text(
    min_size=1,
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="-/_",
    ),
)

# SHAs: exactly 40 lowercase hex chars, matching parse_manifest_pins's
# _HEX40_RE filter. Shorter strings are silently dropped by the parser.
_shas = st.text(min_size=40, max_size=40, alphabet="0123456789abcdef")

# {layer -> sha} mapping. Dict keys are unique, matching the dict() coalescing
# diff_manifests applies to the parse_manifest_pins output.
_pin_dicts = st.dictionaries(_layer_names, _shas, min_size=1, max_size=8)


def _write_manifest(tmp_path, name: str, pins: dict[str, str]) -> Path:
    """Write a synthetic repo-tool manifest XML with the given {project: sha} mapping."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<manifest>"]
    for proj, sha in pins.items():
        lines.append(f'  <project path="{proj}" revision="{sha}"/>')
    lines.append("</manifest>")
    p = tmp_path / name
    p.write_text("\n".join(lines))
    return p


@pytest.mark.unit
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(pins=_pin_dicts)
def test_identity_diff_is_all_zero(tmp_path, pins: dict[str, str]) -> None:
    """Diffing a manifest against itself yields commit_count=0 for every layer."""
    manifest = _write_manifest(tmp_path, "manifest.xml", pins)

    diffs = diff_manifests(manifest, manifest)

    assert diffs, "non-empty manifest must produce at least one diff entry"
    for d in diffs:
        assert d.old_sha == d.new_sha
        assert d.commit_count == 0


@pytest.mark.unit
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(pins=_pin_dicts, new_sha=_shas)
def test_single_layer_change_detected(tmp_path, pins: dict[str, str], new_sha: str) -> None:
    """Changing exactly one layer's SHA surfaces exactly one differing entry."""
    target = next(iter(pins))
    # Force a genuinely different SHA so the change is observable.
    if new_sha == pins[target]:
        new_sha = ("1" if pins[target][0] != "1" else "2") + pins[target][1:]

    new_pins = dict(pins)
    new_pins[target] = new_sha

    old_manifest = _write_manifest(tmp_path, "old.xml", pins)
    new_manifest = _write_manifest(tmp_path, "new.xml", new_pins)

    diffs = diff_manifests(old_manifest, new_manifest)

    changed = [d for d in diffs if d.old_sha != d.new_sha]
    assert len(changed) == 1
    assert changed[0].layer == target


@pytest.mark.unit
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(old_pins=_pin_dicts, new_pins=_pin_dicts)
def test_all_layers_present(tmp_path, old_pins: dict[str, str], new_pins: dict[str, str]) -> None:
    """The set of diffed layers equals the union of both manifests' layers."""
    old_manifest = _write_manifest(tmp_path, "old.xml", old_pins)
    new_manifest = _write_manifest(tmp_path, "new.xml", new_pins)

    diffs = diff_manifests(old_manifest, new_manifest)

    assert {d.layer for d in diffs} == set(old_pins) | set(new_pins)
