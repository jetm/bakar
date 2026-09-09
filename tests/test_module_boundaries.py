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
never as logic - by each extraction. ``BUILD_RE_EXPORTED_NAMES`` records the
inverse for ``bakar.commands.build``: names that moved but must stay reachable
through it, because callers and tests still read them there.

The third is the guard the two lists depend on: patching an absent attribute
must raise. If any patch form this repository uses tolerated a missing name, a
stale ``monkeypatch``/``mock.patch`` left behind on a moved symbol would
install a mock nothing reads and the test around it would pass while testing
nothing.
"""

from __future__ import annotations

import inspect
from importlib import import_module
from typing import get_type_hints
from unittest import mock

import pytest

from bakar import diagnostics
from bakar.elfscan import _resolve_needed, _resolve_roots

pytestmark = pytest.mark.unit

# Names that have MOVED off ``bakar.diagnostics`` and must not be re-exported
# from it. Each entry is a ``(module_path, symbol_name)`` pair. Extend this
# list - not the test body - when an extraction removes a name.
RELOCATED_SYMBOLS: list[tuple[str, str]] = [
    ("bakar.diagnostics", "_NFS_BOUNDED_LOOKUP_OPTS"),
    ("bakar.diagnostics", "_NFS_LOW_ACTIMEO_SECONDS"),
    ("bakar.diagnostics", "_NFS_ACTIMEO_OPTS"),
    ("bakar.diagnostics", "_BUILDTOOLS_ENV_SCRIPT_GLOB"),
    ("bakar.diagnostics", "load_user_config"),
    ("bakar.diagnostics", "_format_capacity"),
    ("bakar.diagnostics", "_query_sccache_daemon"),
    ("bakar.diagnostics", "_probe_host_uds_daemon"),
    ("bakar.diagnostics", "_DYNAMIC_HEADER"),
    ("bakar.diagnostics", "_READER_ENV"),
    ("bakar.diagnostics", "_GLIBC_NODE_RE"),
    ("bakar.diagnostics", "_ELF_NEEDED_RE"),
    ("bakar.diagnostics", "_ELF_RUNPATH_RE"),
    ("bakar.diagnostics", "_required_glibc_nodes"),
    ("bakar.diagnostics", "_LD_CONF_MAX_DEPTH"),
    ("bakar.diagnostics", "_LD_SO_CONF"),
    ("bakar.diagnostics", "_ld_so_conf_dirs"),
    ("bakar.diagnostics", "_unique"),
    ("bakar.diagnostics", "_HOST_LIB_DIRS"),
    ("bakar.diagnostics", "_LEAK_REPORT_LIMIT"),
    ("bakar.diagnostics", "_CONTROL_RE"),
    ("bakar.diagnostics", "_ARTIFACT_TEXT_LIMIT"),
    ("bakar.diagnostics", "_ELISION"),
    ("bakar.diagnostics", "_ENTRY_SEPARATOR"),
    ("bakar.diagnostics", "_NativeLeak"),
    ("bakar.diagnostics", "_ElfInfo"),
    ("bakar.diagnostics", "_is_elf"),
    ("bakar.diagnostics", "_read_elf"),
    ("bakar.diagnostics", "_runpath_dirs"),
    ("bakar.diagnostics", "_normalized"),
    ("bakar.diagnostics", "_lexically_within"),
    ("bakar.diagnostics", "_resolve_roots"),
    ("bakar.diagnostics", "_resolve_needed"),
    ("bakar.diagnostics", "_within_any"),
    ("bakar.diagnostics", "_nodes_above"),
    ("bakar.diagnostics", "_producing_recipe"),
    ("bakar.diagnostics", "_HOST_ELF_MACHINE"),
    ("bakar.diagnostics", "_HOST_ELF_OSABI"),
    ("bakar.diagnostics", "_UNCHECKED_PROVIDED"),
    ("bakar.diagnostics", "_UNCHECKED_FOREIGN"),
    ("bakar.diagnostics", "_UNCHECKED_NO_GLIBC"),
    ("bakar.diagnostics", "_host_platform_elf"),
    ("bakar.diagnostics", "_Unclassified"),
    ("bakar.diagnostics", "_unchecked_reason"),
]

# Same contract, for names that have moved off ``bakar.commands.build``.
RELOCATED_BUILD_SYMBOLS: list[tuple[str, str]] = []

# The INVERSE contract: names that moved off ``bakar.commands.build`` but must
# stay reachable through it. Tests reach them as module attributes
# (``build_mod._CveRequest``) and ``_finish_build``/``build()`` still read them
# as bare names, so dropping the re-export breaks callers rather than tidying
# the surface. Extend this list - not the test body - when a move keeps a name.
BUILD_RE_EXPORTED_NAMES: tuple[str, ...] = (
    "_CVE_REPORT_TARGET",
    "_CveRequest",
    "_resolve_cve_request",
    "_generate_cve_report",
    "_SbomRequest",
    "_resolve_sbom_request",
    "_filter_image_sbom",
    "_FeedRequest",
    "_resolve_feed_request",
    "_sync_feed",
    # The flavor dispatchers and the two frozen contexts they read, moved to
    # ``bakar.commands._build_flavors``. ``build()`` calls every one as a bare
    # name and tests patch ``build_mod._run_single_preset_release`` to count
    # dispatches, so the origin path has to keep resolving.
    "_preset_completer",
    "_BbsetupCtx",
    "_run_bbsetup_build",
    "_BuildCtx",
    "_run_byo_build",
    "_run_manifest_build",
    "_is_multi_release",
    "_run_single_preset_release",
    # Not moved symbols but module objects the sbom/cve/qcom tests patch THROUGH
    # ``bakar.commands.build`` to reach the post-build and step modules.
    "subprocess",
    "step_kas",
    "step_override",
    "step_qcom_build",
)

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


@pytest.mark.parametrize("name", BUILD_RE_EXPORTED_NAMES)
def test_build_re_exported_surface_is_intact(name: str) -> None:
    assert hasattr(import_module("bakar.commands.build"), name), f"bakar.commands.build lost {name}"


def test_patching_an_absent_attribute_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """All three patch forms used here must reject a name that is gone."""
    assert not hasattr(diagnostics, _ABSENT_ATTR)

    with pytest.raises(AttributeError):
        monkeypatch.setattr(diagnostics, _ABSENT_ATTR, object())

    with pytest.raises(AttributeError):
        monkeypatch.setattr(f"bakar.diagnostics.{_ABSENT_ATTR}", object())

    with pytest.raises(AttributeError):
        mock.patch(f"bakar.diagnostics.{_ABSENT_ATTR}").start()


def test_build_annotations_resolve_at_runtime() -> None:
    """Every ``build()`` annotation must be resolvable outside TYPE_CHECKING.

    Typer reads a command signature through ``inspect.signature(eval_str=True)``,
    so an alias hidden behind ``if TYPE_CHECKING:`` raises
    ``RuntimeError: Type not yet supported`` when the command is built. Scoped to
    ``build`` on purpose: ``_post_build._CveRequest`` is annotated with a
    deliberately TYPE_CHECKING-guarded ``KasBuildContext`` and would fail a
    package-wide sweep for an unrelated reason.
    """
    build = import_module("bakar.commands.build").build

    hints = get_type_hints(build, include_extras=True)

    assert set(inspect.signature(build).parameters) <= set(hints)
