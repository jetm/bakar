"""Guards for :class:`bakar.config.ResolveRequest`, the packed ``resolve()`` argument.

Three things are proven here, none of which any pre-existing test covered:

1. The four-level precedence stack still ranks the way it always did, with
   every level supplying the SAME field at once and a different value at each.
   ``tests/test_env_precedence.py`` tests the tiers pairwise; a pairwise suite
   passes even when the middle of a stack is reordered, so the ladder below
   loads every tier simultaneously and then removes them one at a time.
2. The same ladder driven through a real ``bakar build`` invocation, so the
   PRODUCER side - the call site that builds the request - is exercised, not
   just the consumer. A direct-call test cannot see a transposition made where
   the request is constructed.
3. Every request field reaches its own destination, with a DISTINCT value per
   field so a swap between two fields is visible.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import bakar.commands._app as cli_module
from bakar.cli import app
from bakar.config import (
    DEFAULT_NXP_MACHINE,
    BSPSpec,
    BuildConfig,
    ResolveRequest,
    resolve,
)
from bakar.preset_config import PresetEntry
from bakar.user_config import UserConfig
from bakar.workspace_config import WorkspaceConfig, write_workspace_config

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.unit

_MACHINE_VAR = "BAKAR_MACHINE"

# One distinct value per tier, so a transposition between two tiers is visible.
_CLI_MACHINE = "cli-board"
_ENV_MACHINE = "env-board"
_WS_MACHINE = "workspace-board"
_USER_MACHINE = "user-config-board"


def _nxp_workspace(tmp_path: Path) -> Path:
    """Return a workspace path with the nxp subdir present."""
    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. The laddered precedence stack, direct call
# ---------------------------------------------------------------------------

# Highest tier first. Each entry names the tiers still present and the value
# that must win once every tier above it has been removed.
_LADDER = [
    ("cli+env+workspace+user", {"cli", "env", "workspace", "user"}, _CLI_MACHINE),
    ("env+workspace+user", {"env", "workspace", "user"}, _ENV_MACHINE),
    ("workspace+user", {"workspace", "user"}, _WS_MACHINE),
    ("user", {"user"}, _USER_MACHINE),
    ("none", set(), DEFAULT_NXP_MACHINE),
]


@pytest.mark.parametrize("case", _LADDER, ids=lambda c: c[0])
def test_machine_precedence_ladder(case, tmp_path, monkeypatch) -> None:
    """Walk the stack down one tier at a time; the highest present tier must win."""
    _label, tiers, expected = case

    if "env" in tiers:
        monkeypatch.setenv(_MACHINE_VAR, _ENV_MACHINE)
    else:
        monkeypatch.delenv(_MACHINE_VAR, raising=False)
    if "workspace" in tiers:
        write_workspace_config(tmp_path, "nxp", {"machine": _WS_MACHINE})
    user_config = UserConfig(nxp_machine=_USER_MACHINE) if "user" in tiers else UserConfig()
    spec = BSPSpec(machine=_CLI_MACHINE) if "cli" in tiers else BSPSpec()

    cfg = resolve(
        ResolveRequest(
            workspace=_nxp_workspace(tmp_path),
            bsp_family="nxp",
            spec=spec,
            user_config=user_config,
        )
    )

    assert cfg.machine == expected


def test_every_tier_uses_a_distinct_machine_value() -> None:
    """The ladder is only meaningful while no two tiers share a value."""
    values = [_CLI_MACHINE, _ENV_MACHINE, _WS_MACHINE, _USER_MACHINE, DEFAULT_NXP_MACHINE]
    assert len(set(values)) == len(values), "two tiers share a value; a swap between them is invisible"


# ---------------------------------------------------------------------------
# 2. The same ladder through a real CLI invocation (producer side)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_vendors() -> Iterator[None]:
    """Vendor cache leaks across tests; reset it around each run."""
    cli_module._VENDORS = None
    yield
    cli_module._VENDORS = None


def _byo_yaml(tmp_path: Path) -> Path:
    """A BYO kas YAML that declares NO machine.

    ``build`` folds ``machine_from_yaml()`` into the request when ``--machine``
    is absent, so a yaml carrying a machine would outrank every tier below the
    CLI and flatten the ladder.
    """
    pilot = tmp_path / "pilot"
    pilot.mkdir()
    kas_yaml = pilot / "kas.yml"
    kas_yaml.write_text("header:\n  version: 14\nrepos:\n  poky:\n")
    return kas_yaml


def _capturing_resolve(captured: list[BuildConfig]):
    """Wrap the real ``resolve`` so the produced config is observable."""

    def _wrapper(request: ResolveRequest) -> BuildConfig:
        cfg = resolve(request)
        captured.append(cfg)
        return cfg

    return _wrapper


# The generic family has no user-config machine tier (see _family_defaults), so
# the CLI ladder covers CLI > env > workspace .bakar.toml > built-in default.
_CLI_LADDER = [
    ("cli+env+workspace", {"cli", "env", "workspace"}, _CLI_MACHINE),
    ("env+workspace", {"env", "workspace"}, _ENV_MACHINE),
    ("workspace", {"workspace"}, _WS_MACHINE),
    ("none", set(), "generic"),
]


@pytest.mark.parametrize("case", _CLI_LADDER, ids=lambda c: c[0])
def test_machine_precedence_ladder_through_the_cli(case, tmp_path, monkeypatch) -> None:
    """`bakar build` must rank the tiers exactly as a direct resolve() call does."""
    _label, tiers, expected = case
    kas_yaml = _byo_yaml(tmp_path)

    if "env" in tiers:
        monkeypatch.setenv(_MACHINE_VAR, _ENV_MACHINE)
    else:
        monkeypatch.delenv(_MACHINE_VAR, raising=False)
    if "workspace" in tiers:
        write_workspace_config(kas_yaml.parent, "generic", {"machine": _WS_MACHINE})

    argv = ["build", str(kas_yaml), "--dry-run"]
    if "cli" in tiers:
        argv += ["--machine", _CLI_MACHINE]

    captured: list[BuildConfig] = []
    with (
        patch("bakar.commands._app.load_vendors", return_value=[]),
        # The doctor gate is host-state dependent and would abort the run
        # before resolve()'s result is observable; it is not what is under test.
        patch("bakar.commands._helpers.run_all", return_value=[]),
        patch("bakar.commands.build.resolve", side_effect=_capturing_resolve(captured)),
        patch("bakar.commands._build_flavors.resolve", side_effect=_capturing_resolve(captured)),
    ):
        result = CliRunner().invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert captured, "build() never reached resolve()"
    assert captured[0].machine == expected


# ---------------------------------------------------------------------------
# 3. Field-level integrity of the packed request
# ---------------------------------------------------------------------------

_EXPECTED_FIELDS = (
    "workspace",
    "bsp_family",
    "spec",
    "kas_yaml",
    "user_config",
    "workspace_config",
    "preset",
    "family_is_explicit",
    "sccache_dist_override",
)


def test_resolve_request_carries_the_nine_former_parameters() -> None:
    """The repack must neither drop nor rename one of the nine inputs."""
    assert tuple(f.name for f in dataclasses.fields(ResolveRequest)) == _EXPECTED_FIELDS


def test_resolve_request_stays_keyword_only() -> None:
    """``resolve`` was ``def resolve(*, ...)``; the request must keep that guard.

    Without it a positional ``ResolveRequest(ws, spec)`` would silently land
    ``spec`` in ``bsp_family`` - the exact transposition the repack prevents.
    """
    kinds = {p.kind for p in inspect.signature(ResolveRequest).parameters.values()}
    assert kinds, "derived no parameters - the signature walk is vacuous"
    assert kinds == {inspect.Parameter.KEYWORD_ONLY}, (
        f"ResolveRequest accepts non-keyword arguments ({kinds}); a positional "
        "transposition between two fields would construct silently"
    )


def test_resolve_request_has_no_type_default_collision() -> None:
    """No two fields share a (type, default) pair.

    Where two fields DO share both, a one-at-a-time test is needed because an
    all-fields test cannot see a swap between them - both hold the same kind of
    value. ``ResolveRequest`` has no such pair, so the all-fields test below is
    sufficient; this guard fails if a later edit introduces one.
    """
    groups = [(f.type, f.default) for f in dataclasses.fields(ResolveRequest)]
    assert groups, "derived no fields - the annotation walk is vacuous"
    duplicates = {g for g in groups if groups.count(g) > 1}
    assert not duplicates, (
        f"fields share a (type, default): {duplicates}. Add a one-at-a-time test over that group; "
        "the all-fields test cannot distinguish a swap between them."
    )


def test_resolve_reads_every_request_field(tmp_path, monkeypatch) -> None:
    """Every field, set to a distinct non-default value, reaches its destination."""
    monkeypatch.delenv(_MACHINE_VAR, raising=False)
    monkeypatch.delenv("BAKAR_SCCACHE_DIST", raising=False)
    workspace = _nxp_workspace(tmp_path)
    (tmp_path / "ti").mkdir(exist_ok=True)
    kas_yaml = tmp_path / "byo.yml"
    kas_yaml.write_text("header:\n  version: 14\n")

    cfg = resolve(
        ResolveRequest(
            workspace=workspace,
            bsp_family="ti",
            spec=BSPSpec(distro="spec-distro"),
            kas_yaml=kas_yaml,
            user_config=UserConfig(dl_dir="/user-config/downloads", sccache_dist=False),
            workspace_config=WorkspaceConfig(rm_work=True),
            preset=PresetEntry(name="p", family="ti", machine="preset-machine", manifest="preset.xml"),
            family_is_explicit=True,
            sccache_dist_override=True,
        )
    )

    assert cfg.workspace == workspace.resolve(), "workspace"
    assert cfg.bsp_family == "ti", "bsp_family"
    assert cfg.distro == "spec-distro", "spec"
    assert cfg.kas_yaml_override == kas_yaml.resolve(), "kas_yaml"
    assert cfg.dl_dir == "/user-config/downloads", "user_config"
    assert cfg.rm_work is True, "workspace_config"
    assert cfg.machine == "preset-machine", "preset"
    assert cfg.sccache_dist is True, "sccache_dist_override"


def test_family_is_explicit_false_defers_to_the_preset_family(tmp_path) -> None:
    """``family_is_explicit`` is the only field with no value-carrying destination.

    It is read only by the preset/family conflict branch, so it is proven by
    the branch it selects: False defers, True raises.
    """
    preset = PresetEntry(name="p", family="ti", manifest="preset.xml")
    request = ResolveRequest(
        workspace=_nxp_workspace(tmp_path),
        bsp_family="nxp",
        preset=preset,
        family_is_explicit=False,
    )

    assert resolve(request).bsp_family == "ti"

    with pytest.raises(ValueError, match="conflicts with preset"):
        resolve(dataclasses.replace(request, family_is_explicit=True))
