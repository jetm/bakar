"""Boundary checks for the signatures repacked by improve-high-arity-signatures.

Two properties nothing else in the suite observes:

1. A repacked callable and the context it delegates to both still live in the
   same module, and the impl takes exactly that context.
2. A Typer command's parameter count never drops. Typer rejects a
   dataclass-annotated parameter (``RuntimeError: Type not yet supported``), so
   each shimmed command keeps its N annotated parameters and packs them in the
   body.

The non-Typer callables this change repacked are in the table too, private
(``_graceful_wait``, ``_render_sstate_lines``, ``_run_pty_with_ui``,
``_run_single_preset_release``) and public (``steps.stress_parse.run``,
``config.resolve``). Extend ``REPACKED`` with a case rather than adding a
parallel test body. A non-Typer callable has no CLI surface, so its
``typer_params`` is None and the count check is skipped for it - such a
signature is free to shrink, which is the whole point of the repack.

That skip is the only one this module is allowed to take, and
``test_only_skip_is_the_typer_gate`` holds it to that: a skip introduced for any
other reason turns a silently-unrun assertion into a failure.

A callable with no shim has ``callable_name`` and ``impl_name`` set to the same
name.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import typing
from pathlib import Path
from typing import NamedTuple

import pytest


class Case(NamedTuple):
    """One repacked callable: where it lives and what it must still look like."""

    module: str
    callable_name: str
    impl_name: str
    ctx_name: str
    typer_params: int | None
    ctx_param: str
    # Parameters the impl carries besides the context. Non-zero only where a
    # value was deliberately left out of the pack.
    extra_params: int = 0


# Parameter counts measured at the pre-change baseline f2aa66a. A drop here is a
# removed user-facing CLI option, not a refactor.
REPACKED: list[Case] = [
    Case("bakar.commands._app", "_main", "_main_impl", "_GlobalOptions", 12, "opts"),
    Case("bakar.commands.clean_cache", "clean_cache", "_clean_cache_impl", "_CleanCacheCtx", 10, "ctx"),
    Case("bakar.commands.getvar", "getvar", "_getvar_impl", "_GetvarCtx", 10, "ctx"),
    Case("bakar.commands.sync", "sync", "_sync_impl", "_SyncCtx", 10, "ctx"),
    Case("bakar.commands.stress_parse", "stress_parse", "_stress_parse_impl", "_StressParseCtx", 11, "ctx"),
    # Private helpers: no shim, no Typer surface, so the signature is free to
    # shrink. ``_render_sstate_lines`` keeps ``console`` positional.
    Case("bakar.build_stop", "_graceful_wait", "_graceful_wait", "_WaitCtx", None, "ctx"),
    Case(
        "bakar.commands._helpers",
        "_render_sstate_lines",
        "_render_sstate_lines",
        "_SstateRender",
        None,
        "render",
        extra_params=1,
    ),
    Case("bakar.steps.kas_build", "_run_pty_with_ui", "_run_pty_with_ui", "_PtyCtx", None, "ctx"),
    # ``_run_single_preset_release`` keeps ``active_preset`` and ``spec_index``
    # outside the pack: they vary per iteration of the caller's loop while the
    # context does not.
    Case(
        "bakar.commands._build_flavors",
        "_run_single_preset_release",
        "_run_single_preset_release",
        "_ReleaseCtx",
        None,
        "ctx",
        extra_params=2,
    ),
    # Public, but reached through Python callers rather than Typer.
    Case("bakar.steps.stress_parse", "run", "run", "StressParseContext", None, "ctx"),
    Case("bakar.config", "resolve", "resolve", "ResolveRequest", None, "request"),
]

NOT_A_TYPER_COMMAND = "{} is not a Typer command"

CASE_IDS = [c.callable_name for c in REPACKED]


@pytest.mark.unit
@pytest.mark.parametrize("case", REPACKED, ids=CASE_IDS)
def test_command_impl_and_context_share_a_module(case: Case) -> None:
    """The impl and its context stay beside the command that packs them.

    Tests patch bare names in the command module's namespace, and such a patch
    only reaches a reader resolving that name in that module.
    """
    mod = importlib.import_module(case.module)

    for attr in (case.callable_name, case.impl_name, case.ctx_name):
        assert hasattr(mod, attr), f"{case.module} does not define {attr}"

    assert callable(getattr(mod, case.callable_name))
    assert callable(getattr(mod, case.impl_name))

    ctx = getattr(mod, case.ctx_name)
    assert dataclasses.is_dataclass(ctx), f"{case.ctx_name} is not a dataclass"
    assert ctx.__dataclass_params__.frozen, f"{case.ctx_name} is not frozen"
    assert dataclasses.fields(ctx), f"{case.ctx_name} has no fields"
    # Every signature these contexts replaced was keyword-only, so dropping
    # kw_only would make the packed fields positionally interchangeable - the
    # transposition this whole change exists to prevent, reintroduced at the
    # construction site. Ten of the eleven shipped without it.
    assert ctx.__dataclass_params__.kw_only, f"{case.ctx_name} is not kw_only"
    # A field annotated with a TYPE_CHECKING-only import leaves this raising
    # NameError, so the dataclass cannot be introspected by anything that walks
    # it at runtime. Four of the eleven shipped that way.
    typing.get_type_hints(ctx)


@pytest.mark.unit
@pytest.mark.parametrize("case", REPACKED, ids=CASE_IDS)
def test_impl_takes_exactly_its_context(case: Case) -> None:
    """The impl reads its arguments from the context and nothing else."""
    mod = importlib.import_module(case.module)
    params = inspect.signature(getattr(mod, case.impl_name)).parameters

    expected = 1 + case.extra_params
    assert len(params) == expected, f"{case.impl_name} takes {len(params)} parameters, expected {expected}"
    assert case.ctx_param in params, f"{case.impl_name} has no {case.ctx_param!r} parameter, takes {list(params)}"
    # PEP 563: annotations are strings here, so compare against the class name.
    annotation = params[case.ctx_param].annotation
    assert annotation in (case.ctx_name, getattr(mod, case.ctx_name)), (
        f"{case.impl_name}'s {case.ctx_param!r} is annotated {annotation!r}, expected {case.ctx_name}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("case", REPACKED, ids=CASE_IDS)
def test_typer_parameter_count_did_not_drop(case: Case) -> None:
    """Every parameter of a Typer command is a user-facing option."""
    if case.typer_params is None:
        pytest.skip(NOT_A_TYPER_COMMAND.format(case.callable_name))

    mod = importlib.import_module(case.module)
    params = inspect.signature(getattr(mod, case.callable_name)).parameters

    assert len(params) == case.typer_params, (
        f"{case.module}.{case.callable_name} has {len(params)} parameters, "
        f"baseline is {case.typer_params} - a dropped parameter is a removed CLI option"
    )


@pytest.mark.unit
def test_only_skip_is_the_typer_gate() -> None:
    """A skip added here for any other reason would hide an unrun assertion.

    The change's skipped count rose because every non-Typer row skips the
    parameter-count check by design. That is the one licensed skip; anything
    else in this module is an assertion that stopped running.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    skips = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "skip"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
    ]
    assert skips, "the typer gate's skip disappeared - it is what keeps the non-Typer rows honest"

    for node in skips:
        reason = node.args[0] if node.args else None
        assert (
            isinstance(reason, ast.Call)
            and isinstance(reason.func, ast.Attribute)
            and reason.func.attr == "format"
            and isinstance(reason.func.value, ast.Name)
            and reason.func.value.id == "NOT_A_TYPER_COMMAND"
        ), f"pytest.skip on line {node.lineno} does not use NOT_A_TYPER_COMMAND"


@pytest.mark.unit
def test_every_repacked_callable_is_covered() -> None:
    """The table is the change's inventory, so a missing row is a missing gate."""
    covered = {(c.module, c.callable_name) for c in REPACKED}
    expected = {
        ("bakar.commands._app", "_main"),
        ("bakar.commands.clean_cache", "clean_cache"),
        ("bakar.commands.getvar", "getvar"),
        ("bakar.commands.sync", "sync"),
        ("bakar.commands.stress_parse", "stress_parse"),
        ("bakar.commands._build_flavors", "_run_single_preset_release"),
        ("bakar.commands._helpers", "_render_sstate_lines"),
        ("bakar.steps.stress_parse", "run"),
        ("bakar.steps.kas_build", "_run_pty_with_ui"),
        ("bakar.build_stop", "_graceful_wait"),
        ("bakar.config", "resolve"),
    }
    assert expected <= covered, f"repacked but ungated: {sorted(expected - covered)}"
