"""``resolve()`` derives the machine from a BYO kas YAML when none is supplied.

Without this, every command except ``build`` resolved a different machine than
``build`` did for the same kas spec, because ``build`` alone threaded
``machine_from_yaml()`` into its ``BSPSpec``. For the generic family the machine
then degenerated to the literal "generic" (see ``_FamilyDefaults`` d_machine),
and since ``local_tmpdir`` is keyed on ``<bsp_root.name>-<machine>-<digest>``,
``bakar bitbake`` operated in a *different TMPDIR* than the build it was meant to
follow up on.

The observed failure: `bakar bitbake avocado-stone -c stone_provision <spec>`
ran in build-imx93-frdm-generic-<digest> while the image had been built in
build-imx93-frdm-avocado-imx93-frdm-<digest>, so do_stone_bundle aborted with
"OS release file ... not found" against a deploy tree that had never held a full
image. `-c clean`/`-c unpack` in the same tmpdir silently reported "0 will build"
for the same reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.config import BSPSpec, ResolveRequest, resolve

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


@pytest.fixture
def avocado_yaml(tmp_path: Path) -> Path:
    """A meta-avocado-style machine YAML declaring avocado-imx93-frdm."""
    kas = tmp_path / "meta-avocado" / "kas" / "machine"
    kas.mkdir(parents=True)
    y = kas / "imx93-frdm.yml"
    y.write_text("header:\n  version: 16\n\nmachine: avocado-imx93-frdm\n")
    return y


def test_machine_derived_from_kas_yaml_when_unset(avocado_yaml: Path, tmp_path: Path) -> None:
    """With no explicit machine, resolve() reads it from the kas YAML."""
    cfg = resolve(ResolveRequest(workspace=tmp_path, bsp_family="generic", spec=BSPSpec(), kas_yaml=avocado_yaml))

    assert cfg.machine == "avocado-imx93-frdm"


def test_explicit_machine_still_wins_over_yaml(avocado_yaml: Path, tmp_path: Path) -> None:
    """An explicit machine (the -m flag) outranks the YAML-derived one."""
    cfg = resolve(
        ResolveRequest(
            workspace=tmp_path,
            bsp_family="generic",
            spec=BSPSpec(machine="avocado-qemuarm64"),
            kas_yaml=avocado_yaml,
        )
    )

    assert cfg.machine == "avocado-qemuarm64"


def test_machine_still_degenerates_without_a_yaml(tmp_path: Path) -> None:
    """No kas YAML and no explicit machine keeps the old generic default.

    Pinned so the derivation cannot quietly change the non-BYO path, where there
    is no YAML to read a machine out of.
    """
    cfg = resolve(ResolveRequest(workspace=tmp_path, bsp_family="generic", spec=BSPSpec(), kas_yaml=None))

    assert cfg.machine == "generic"


def test_bitbake_and_build_agree_on_resolved_tmpdir(avocado_yaml: Path, tmp_path: Path) -> None:
    """The two call shapes land on the same resolved_tmpdir.

    This is the property that actually broke: build threaded the YAML machine in
    itself, every other command did not, and the tmpdir is keyed on the machine -
    so the two disagreed about which tree held the artifacts.
    """
    build_shape = resolve(
        ResolveRequest(
            workspace=tmp_path,
            bsp_family="generic",
            spec=BSPSpec(machine="avocado-imx93-frdm"),
            kas_yaml=avocado_yaml,
        )
    )
    bitbake_shape = resolve(
        ResolveRequest(
            workspace=tmp_path,
            bsp_family="generic",
            spec=BSPSpec(),
            kas_yaml=avocado_yaml,
        )
    )

    assert bitbake_shape.machine == build_shape.machine
    assert bitbake_shape.resolved_tmpdir == build_shape.resolved_tmpdir
