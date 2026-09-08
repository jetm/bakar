"""Executable inventory of the module boundaries this repository relies on.

Three kinds of fact live here, and each is asserted rather than documented so a
later extraction that violates it fails instead of drifting quietly.

The first is a security invariant: ``_resolve_roots`` is the only sanctioned
producer of the ``permitted`` allowlist that ``_resolve_needed`` consumes, so
the two must stay in one module. Split them across a boundary and a third
caller could hand ``_resolve_needed`` an allowlist nothing canonicalised.

The second is the set of names ``bakar.diagnostics`` still exposes, and the
two ``RELOCATED_*`` lists that record names deliberately NOT re-exported from
the module a symbol left. Those lists start empty and are extended - as data,
never as logic - by each extraction.

The third is the guard the two lists depend on: patching an absent attribute
must raise. If any patch form this repository uses tolerated a missing name, a
stale ``monkeypatch``/``mock.patch`` left behind on a moved symbol would
install a mock nothing reads and the test around it would pass while testing
nothing.
"""

from __future__ import annotations

from importlib import import_module
from unittest import mock

import pytest

from bakar import diagnostics
from bakar.diagnostics import _resolve_needed, _resolve_roots

pytestmark = pytest.mark.unit

# Names that have MOVED off ``bakar.diagnostics`` and must not be re-exported
# from it. Each entry is a ``(module_path, symbol_name)`` pair. Extend this
# list - not the test body - when an extraction removes a name.
RELOCATED_SYMBOLS: list[tuple[str, str]] = [
    ("bakar.diagnostics", "_NFS_BOUNDED_LOOKUP_OPTS"),
    ("bakar.diagnostics", "_NFS_LOW_ACTIMEO_SECONDS"),
    ("bakar.diagnostics", "_NFS_ACTIMEO_OPTS"),
]

# Same contract, for names that have moved off ``bakar.commands.build``.
RELOCATED_BUILD_SYMBOLS: list[tuple[str, str]] = []

# Public surface ``bakar.diagnostics`` must keep exposing regardless of what
# moves out of it.
DIAGNOSTICS_PUBLIC_NAMES: tuple[str, ...] = (
    "is_path_on_nfs",
    "detect_buildtools",
    "resolve_buildtools_dir",
    "probe_cluster",
    "probe_build_daemon",
    "probe_ccache",
    "split_host_port",
    "CheckResult",
    "Severity",
    "Status",
    "run_all",
    "any_blocking_failure",
    "group_results",
    "SHARED_CHECKS",
)

# A name no module defines, used to prove each patch form rejects an absent
# attribute rather than creating one.
_ABSENT_ATTR = "_bakar_definitely_absent_attribute"


def test_resolve_roots_and_needed_share_a_module() -> None:
    """The allowlist producer and its consumer must live in the same module."""
    assert _resolve_roots.__module__ == _resolve_needed.__module__


@pytest.mark.parametrize("name", DIAGNOSTICS_PUBLIC_NAMES)
def test_diagnostics_public_surface_is_intact(name: str) -> None:
    assert hasattr(diagnostics, name), f"bakar.diagnostics lost {name}"


def test_relocated_symbols_are_not_re_exported() -> None:
    still_present = [
        f"{module_path}.{symbol}"
        for module_path, symbol in RELOCATED_SYMBOLS
        if hasattr(import_module(module_path), symbol)
    ]
    assert not still_present, f"relocated names re-exported from their origin: {still_present}"


def test_relocated_build_symbols_are_not_re_exported() -> None:
    still_present = [
        f"{module_path}.{symbol}"
        for module_path, symbol in RELOCATED_BUILD_SYMBOLS
        if hasattr(import_module(module_path), symbol)
    ]
    assert not still_present, f"relocated names re-exported from their origin: {still_present}"


def test_patching_an_absent_attribute_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """All three patch forms used here must reject a name that is gone."""
    assert not hasattr(diagnostics, _ABSENT_ATTR)

    with pytest.raises(AttributeError):
        monkeypatch.setattr(diagnostics, _ABSENT_ATTR, object())

    with pytest.raises(AttributeError):
        monkeypatch.setattr(f"bakar.diagnostics.{_ABSENT_ATTR}", object())

    with pytest.raises(AttributeError):
        mock.patch(f"bakar.diagnostics.{_ABSENT_ATTR}").start()
