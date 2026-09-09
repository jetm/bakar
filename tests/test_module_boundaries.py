"""Executable inventory of the module boundaries this repository relies on.

Six kinds of fact live here, and each is asserted rather than documented so a
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

The fourth is the governance contract with ``.arch-rules.toml``: every module
an extraction created must be matched by some group, and every declared group
must still match a file. Both halves fail open rather than loud - an unmatched
module and a group whose paths match nothing each leave the fitness check
reporting "no violations" over something it never examined.

The fifth is that each extracted module imports standalone in a fresh
interpreter. The rest of the suite imports these modules into one already
populated ``sys.modules``, which is exactly the condition under which an
import cycle does not fire; the CLI reaches them through ``cli.py``'s
registration path instead. Only an interpreter that imports one of them FIRST
observes the module-level import order an extraction actually changed.

The sixth is that ``bakar.diagnostics`` still DEFINES all 48 ``check_*``
functions. Only helpers were extracted, so the check surface `bakar doctor`
walks is meant to be untouched - and that claim is what makes a doctor run
against a real workspace unnecessary as evidence. The pinned set below was
taken by diffing the sorted ``^def check_`` lists of ``diagnostics.py`` at
``e62f601`` (the pre-split commit) and at the end of the split: 48 names on
both sides, no difference.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
import tomllib
from importlib import import_module
from pathlib import Path, PurePosixPath
from typing import get_type_hints
from unittest import mock

import pytest

from bakar import diagnostics
from bakar.bsp_model import get_model
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
    # Dropped as surplus: each had no production importer and no reader left in
    # ``diagnostics``, so a stub aimed at the origin would have reached nothing
    # while reading as though it had taken.
    ("bakar.diagnostics", "_VERNEED_HEADER"),
    ("bakar.diagnostics", "ClusterCapacity"),
    ("bakar.diagnostics", "_parse_cluster_status"),
    ("bakar.diagnostics", "_build_daemon_report_from_stats"),
    # The probes took the last module-level reader of build_stop with them, so
    # diagnostics dropped the binding. Pinned because a re-export would silently
    # revive the stale patch path: tests/test_diagnostics.py patches
    # bakar.probes.build_stop.detect_runtime, and the bakar.diagnostics spelling
    # it replaced would start resolving again while intercepting nothing.
    ("bakar.diagnostics", "build_stop"),
]

# Same contract, for names that have moved off ``bakar.commands.build``. Not
# empty any more: the post-review pass dropped two names from that module's
# re-export block, and a drop without a pin is exactly the surplus re-export it
# removed, free to come back unopposed.
RELOCATED_BUILD_SYMBOLS: list[tuple[str, str]] = [
    # Re-exported for a build_mod attribute access that never existed; its
    # re-export and the assertion pinning it landed in the same commit.
    ("bakar.commands.build", "_CVE_REPORT_TARGET"),
    # Retained so build_mod.subprocess resolved for three sbom patch sites. They
    # patch _post_build.subprocess now - the module that actually calls it.
    ("bakar.commands.build", "subprocess"),
]

# The INVERSE contract: names that moved off ``bakar.commands.build`` but must
# stay reachable through it. Tests reach them as module attributes
# (``build_mod._CveRequest``) and ``_finish_build``/``build()`` still read them
# as bare names, so dropping the re-export breaks callers rather than tidying
# the surface. Extend this list - not the test body - when a move keeps a name.
#
# A name meeting NEITHER criterion does not belong here. ``_CVE_REPORT_TARGET``
# was listed once: its re-export and this pin landed in the same commit, so the
# pin was the only thing keeping it alive and could never have failed for the
# reason this test gives.
BUILD_RE_EXPORTED_NAMES: tuple[str, ...] = (
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
    # Not moved symbols but module objects the cve/qcom/layers tests patch
    # THROUGH ``bakar.commands.build`` to reach the step modules. ``subprocess``
    # is deliberately NOT here: the sbom tests now patch it through
    # ``_post_build``, the module that actually calls it.
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

# Modules this change extracted. Each must be matched by some group in
# ``.arch-rules.toml``: a module matching no group has its imports silently
# ungoverned, which is the failure mode that shipped once already and is
# invisible to the fitness check itself (it reports "no violations" because it
# never looked at the file). Extend this list - not the test body - when an
# extraction adds a module.
EXTRACTED_MODULES: tuple[str, ...] = (
    "src/bakar/mounts.py",
    "src/bakar/buildtools.py",
    "src/bakar/probes.py",
    "src/bakar/elfscan.py",
    "src/bakar/commands/_post_build.py",
    "src/bakar/commands/_build_flavors.py",
    "src/bakar/commands/_build_options.py",
)

# The ``check_*`` surface ``bakar doctor`` walks, pinned as it stood at
# ``e62f601`` - the commit before the first extraction. Nothing in this split
# was supposed to move a check, only the helpers underneath them. Adding a
# genuinely new check means extending this list in the same commit.
DIAGNOSTICS_CHECK_NAMES: frozenset[str] = frozenset(
    {
        "check_bbsetup_config_sources",
        "check_bbsetup_initialized",
        "check_bitbake_locks",
        "check_bitbake_override",
        "check_cache_dirs",
        "check_ccache_health",
        "check_central_hashserv",
        "check_central_prserv",
        "check_cgroup_v2",
        "check_container_bitbake",
        "check_container_image",
        "check_disk_free",
        "check_docker_daemon",
        "check_docker_storage_driver",
        "check_docker_ulimits",
        "check_docker_version",
        "check_forks_linux_imx",
        "check_forks_ti_linux_kernel",
        "check_forks_ti_u_boot",
        "check_git_global_config",
        "check_git_object_cache",
        "check_hashserv",
        "check_host_preflight",
        "check_host_tools",
        "check_kas_yaml_syntax",
        "check_manifest_consistency",
        "check_memory",
        "check_mold_compiler",
        "check_nfs_delegations",
        "check_nproc",
        "check_override_syntax",
        "check_psi_support",
        "check_sccache_dist",
        "check_scope_controller_weights",
        "check_shared_cache_mounts",
        "check_sstate_hash_leak",
        "check_sysctl",
        "check_systemd_scope",
        "check_ti_layertool_config_consistency",
        "check_ti_layertool_present",
        "check_uninative_checksum",
        "check_uninative_cluster_consistency",
        "check_uninative_dldir_links",
        "check_uninative_fragment",
        "check_uninative_glibc",
        "check_uninative_leak",
        "check_uninative_mirror_hit",
        "check_workspace_filesystem",
    }
)

_ARCH_RULES = Path(__file__).resolve().parent.parent / ".arch-rules.toml"

# A name no module defines, used to prove each patch form rejects an absent
# attribute rather than creating one.
_ABSENT_ATTR = "_bakar_definitely_absent_attribute"


def _arch_groups() -> list[dict]:
    """Return the ``[[modules]]`` blocks declared by the committed rules file."""
    return tomllib.loads(_ARCH_RULES.read_text(encoding="utf-8"))["modules"]


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


def test_build_flavors_reaches_steps_through_the_module_object() -> None:
    """``_build_flavors`` must import step MODULES, never their members.

    Several tests patch ``bakar.commands.build.step_override.apply`` and expect
    the dispatchers in ``_build_flavors`` to see it. That works because the
    attribute write lands on the shared ``bakar.steps.*`` module object, which
    both modules merely alias - so the patch is visible wherever the call is
    made from.

    A member import breaks it silently. ``from bakar.steps.bitbake_override
    import apply`` binds the FUNCTION at import time, and the dispatcher then
    calls that binding: the patch still writes to the module attribute, still
    resolves, and reaches nothing. The real step runs against the test's
    workspace and the assertion passes.

    Asserting the import FORM is what makes this falsifiable. An earlier version
    of this test compared ``build.step_override is _build_flavors.step_override``
    and could not fail: both names come from ``sys.modules`` by construction, so
    the identity holds even after a dispatcher has been rewritten to bypass them
    entirely. Applying exactly that rewrite left all three of its cases green.
    """
    source = inspect.getsource(import_module("bakar.commands._build_flavors"))

    member_imports = [
        line.strip() for line in source.splitlines() if line.startswith("from bakar.steps.") and " import " in line
    ]

    assert not member_imports, (
        "_build_flavors must alias step MODULES (from bakar.steps import x as step_x), "
        f"not import their members - these bind at import time and defeat the "
        f"attribute-patch seam: {member_imports}"
    )


def test_patching_an_absent_attribute_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """All three patch forms used here must reject a name that is gone."""
    assert not hasattr(diagnostics, _ABSENT_ATTR)

    with pytest.raises(AttributeError):
        monkeypatch.setattr(diagnostics, _ABSENT_ATTR, object())

    with pytest.raises(AttributeError):
        monkeypatch.setattr(f"bakar.diagnostics.{_ABSENT_ATTR}", object())

    with pytest.raises(AttributeError):
        mock.patch(f"bakar.diagnostics.{_ABSENT_ATTR}").start()


@pytest.mark.parametrize("module_path", EXTRACTED_MODULES)
def test_extracted_module_is_matched_by_exactly_one_arch_group(module_path: str) -> None:
    """An extracted module must land in exactly one ``.arch-rules.toml`` group.

    Zero and two are both failures, for opposite reasons.

    A module matching NO group is not a violation - it is worse. The fitness
    check simply never evaluates its imports and still reports "no violations",
    so the boundary erodes with nothing failing. Four modules sat in exactly
    that state between their extraction and this assertion.

    A module matching TWO groups has no answer to "which layer owns this",
    so which rule applies depends on iteration order rather than on the rules
    file. Asserting non-emptiness alone leaves that case green: a later glob
    widened to overlap an explicit entry passes this test while making
    ownership ambiguous, which is why the count is compared rather than the
    truthiness.
    """
    target = PurePosixPath(module_path)

    matched = [group["name"] for group in _arch_groups() for pattern in group["paths"] if target.full_match(pattern)]

    assert len(matched) == 1, (
        f"{module_path} must match exactly one group in .arch-rules.toml, matched {len(matched)}: {matched or 'none'}"
    )


def test_every_arch_group_matches_at_least_one_file() -> None:
    """A group whose patterns match nothing governs nothing.

    ``mold`` outlived the modules it named: ``src/bakar/mold_*.py`` matched zero
    files while the group, its layers rule and its ``independent`` entry all
    stayed behind, so the rule set described a boundary that no longer existed.
    """
    repo_root = _ARCH_RULES.parent

    empty = [
        group["name"] for group in _arch_groups() if not any(any(repo_root.glob(pattern)) for pattern in group["paths"])
    ]

    assert not empty, f"groups matching no file: {empty}"


def test_build_annotations_resolve_at_runtime() -> None:
    """Every ``build()`` annotation must be resolvable outside TYPE_CHECKING.

    Typer reads a command signature through ``inspect.signature(eval_str=True)``,
    so an alias hidden behind ``if TYPE_CHECKING:`` fails when the command is
    built. The error is ``NameError: name '<alias>' is not defined``, raised
    while resolving the signature - NOT the ``RuntimeError: Type not yet
    supported`` that a dataclass-typed parameter produces further in. Measured on
    typer 0.25.1; the two are different failures and only the NameError is
    reachable from this rule.
    """
    build = import_module("bakar.commands.build").build

    hints = get_type_hints(build, include_extras=True)

    assert set(inspect.signature(build).parameters) <= set(hints)


def test_post_build_request_annotations_resolve_at_runtime() -> None:
    """The moved request dataclasses must resolve as they did before the split.

    ``build.py`` imported ``KasBuildContext``, ``_FeedRequest`` and
    ``_SbomRequest`` unguarded, so ``get_type_hints`` on the dataclasses
    annotated with them succeeded. Re-guarding any of them during a later move
    would narrow that silently, since nothing in the CLI path resolves these
    hints - only a serializer or an introspection helper would notice.

    ``_BuildCtx`` is resolved with ``BspModel`` supplied, because THAT guard is
    load-bearing and pre-dates the split: ``_build_flavors`` has no runtime
    ``bakar.bsp_model`` import and the old ``build.py`` did not either, so
    ``_BuildCtx`` raised ``NameError`` on it before this change too. Supplying it
    isolates the request types, which are the names the split moved.
    """
    bsp_model = import_module("bakar.bsp_model")
    post_build = import_module("bakar.commands._post_build")
    flavors = import_module("bakar.commands._build_flavors")

    assert "kas_ctx" in get_type_hints(post_build._CveRequest)

    ctx_hints = get_type_hints(flavors._BuildCtx, localns={"BspModel": bsp_model.BspModel})
    assert {"feed", "sbom"} <= set(ctx_hints)


def _dotted(module_path: str) -> str:
    """Map ``src/bakar/commands/_x.py`` to the importable ``bakar.commands._x``."""
    return PurePosixPath(module_path).with_suffix("").as_posix().removeprefix("src/").replace("/", ".")


@pytest.mark.parametrize("module_path", EXTRACTED_MODULES)
def test_extracted_module_imports_first_in_a_fresh_interpreter(module_path: str) -> None:
    """Each extracted module must import standalone, as the FIRST bakar import.

    A plain ``import`` from inside the suite proves nothing here: by the time it
    runs, ``sys.modules`` already holds every module in the package, so a cycle
    that would deadlock a cold interpreter resolves from cache instead. The
    subprocess is the whole test - it is the only context in which the
    module-level import order an extraction rearranged is actually exercised.

    Two couplings are deliberate and must survive this, not be excluded from it.
    ``probes`` imports ``build_stop`` at module level while ``build_stop`` reaches
    ``diagnostics`` through a function-body deferred import, and
    ``_build_flavors`` binds ``bakar.commands.build`` as a module object on its
    last line because the moved dispatchers call back into helpers that stayed on
    ``build.py``. Both are cycles held open by placement alone, which is precisely
    the kind of arrangement a later edit breaks without noticing.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import {_dotted(module_path)}"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def _registered_checks() -> set[str]:
    """Assemble the check surface ``run_all`` walks, mirroring how it builds it.

    ``run_all`` unions ``SHARED_CHECKS`` with the dispatched model's
    ``doctor_extras``, appends the two bbsetup checks inline for that family,
    and keeps ``_POST_BUILD_CHECKS`` when asked. Its later steps only ever
    FILTER that list (host mode, cluster, post-build), so the union across
    every family is the full set any run can reach.
    """
    registered = set(diagnostics.SHARED_CHECKS)
    for family in ("nxp", "ti", "qcom"):
        registered |= set(get_model(family).doctor_extras)
    # bbsetup carries no BspModel, so run_all appends these two directly.
    registered |= {diagnostics.check_bbsetup_initialized, diagnostics.check_bbsetup_config_sources}
    registered |= set(diagnostics._POST_BUILD_CHECKS)
    return {check.__name__ for check in registered}


def test_diagnostics_still_registers_every_check() -> None:
    """No ``check_*`` function left ``bakar doctor``'s reach during the split.

    This is the executable form of the doctor-output question. Re-running
    ``bakar doctor -f <manifest>`` to compare PASS/WARN/BLOCK verdicts needs a
    real workspace and reports on machine state as much as on this code;
    comparing the assembled surface against the set pinned at ``e62f601``
    answers the same question with no manifest and no host dependency.

    Comparing DEFINITIONS alone does not answer it, which an earlier version of
    this test got wrong. Dropping a function from ``SHARED_CHECKS``, from a
    model's ``doctor_extras``, or from run_all's bbsetup pair leaves it defined
    and importable while ``bakar doctor`` silently stops running it - the exact
    regression this file exists to catch, and the one the definition-set
    comparison stayed green through.
    """
    assert _registered_checks() == DIAGNOSTICS_CHECK_NAMES


def test_diagnostics_still_defines_every_check() -> None:
    """Every registered check is still DEFINED here, not re-exported into here.

    Kept beside the registration assertion above rather than folded into it,
    because the two fail on different edits: this one catches a check whose body
    moved to an extracted module and got imported back, which leaves the
    assembled surface identical and so is invisible to the test above.
    """
    defined = {
        name
        for name, value in vars(diagnostics).items()
        if name.startswith("check_") and inspect.isfunction(value) and value.__module__ == "bakar.diagnostics"
    }

    assert defined == DIAGNOSTICS_CHECK_NAMES
