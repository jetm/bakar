"""Tests for the parametrized layer materializer and the mold link-log injector (task 4.2).

Covers ``materialize_layer`` (the meta-avocado base-dir branch and the
destination-directory return contract), the ``cfg.mold`` gate wired into
``_build_kas_arg``, and ``_inject_literal_mold``'s exported ``BAKAR_MOLD_LINKLOG``
literal (host absolute path vs the container ``/work`` bind-mount path).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.config import BuildConfig
from bakar.steps.kas_build import (
    _build_kas_arg,
    _inject_literal_mold,
    materialize_layer,
)

if TYPE_CHECKING:
    from pathlib import Path


def _cfg(
    root: Path,
    *,
    kas_yaml: Path | None = None,
    mold: bool = False,
    mold_mode: str = "list",
    host_mode: bool = False,
) -> BuildConfig:
    return BuildConfig(
        workspace=root,
        bsp_family="generic",  # type: ignore[arg-type]
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        kas_container_image="img:latest",
        kas_yaml_override=kas_yaml if kas_yaml is not None else root / "my.yml",
        host_mode=host_mode,
        mold=mold,
        mold_mode=mold_mode,  # type: ignore[arg-type]
    )


@pytest.mark.unit
def test_materialize_layer_returns_dest_dir_under_bsp_root(tmp_path: Path) -> None:
    """materialize_layer returns the absolute destination directory, not a relative path.

    The destination-dir return contract is the point of the function: each tuning
    overlay references the layer by the relative repos path ``.bakar/<name>``, and
    materialization must place the real layer there. Off meta-avocado, base ==
    bsp_root, so the layer lands under <bsp_root>/.bakar/<name>.
    """
    cfg = _cfg(tmp_path)

    dest = materialize_layer(cfg, "meta-bakar-mold")

    assert dest == cfg.bsp_root / ".bakar" / "meta-bakar-mold"
    assert dest.is_dir()
    assert (dest / "conf" / "layer.conf").is_file()


@pytest.mark.unit
def test_materialize_layer_targets_workspace_for_meta_avocado(tmp_path: Path) -> None:
    """For meta-avocado the layer lands under <workspace>/.bakar, not <bsp_root>/.bakar.

    meta-avocado runs kas with KAS_WORK_DIR = workspace while bsp_root is the
    nested build-<stem> dir; the ``base = cfg.workspace if cfg.is_meta_avocado
    else cfg.bsp_root`` branch must be preserved so kas can resolve the overlay's
    relative ``.bakar/meta-bakar-mold`` repos path.
    """
    avocado_yaml = tmp_path / "meta-avocado" / "kas" / "machine" / "qemux86-64.yml"
    cfg = _cfg(tmp_path, kas_yaml=avocado_yaml)
    assert cfg.is_meta_avocado is True
    assert cfg.bsp_root != cfg.workspace

    dest = materialize_layer(cfg, "meta-bakar-mold")

    assert dest == cfg.workspace / ".bakar" / "meta-bakar-mold"
    assert dest != cfg.bsp_root / ".bakar" / "meta-bakar-mold"
    assert (dest / "conf" / "layer.conf").is_file()


@pytest.mark.unit
def test_build_kas_arg_gates_mold_layer_on_cfg_mold(tmp_path: Path) -> None:
    """_build_kas_arg materializes the mold layer iff cfg.mold, mirroring the sccache gate."""
    kas_yaml = tmp_path / "my.yml"
    kas_yaml.write_text("header:\n  version: 18\n", encoding="utf-8")
    overlay_source = tmp_path / "overlay.yml"
    overlay_source.write_text("header:\n  version: 18\n", encoding="utf-8")

    off = _cfg(tmp_path, mold=False)
    _build_kas_arg(off, kas_yaml, overlay_source)
    assert not (off.bsp_root / ".bakar" / "meta-bakar-mold").exists()

    on = _cfg(tmp_path, mold=True)
    _build_kas_arg(on, kas_yaml, overlay_source)
    dest = on.bsp_root / ".bakar" / "meta-bakar-mold"
    assert dest.is_dir()
    assert (dest / "conf" / "layer.conf").is_file()


@pytest.mark.unit
def test_inject_literal_mold_container_path(tmp_path: Path) -> None:
    """In container mode the exported literal points at the /work bind mount."""
    cfg = _cfg(tmp_path, mold=True, host_mode=False)
    text = 'local_conf_header:\n  zz-bakar-60-mold: |\n    INHERIT += "mold"\n'

    injected = _inject_literal_mold(cfg, text)

    assert 'export BAKAR_MOLD_LINKLOG = "/work/mold-linklog.jsonl"' in injected
    # The original INHERIT line is preserved.
    assert 'INHERIT += "mold"' in injected


@pytest.mark.unit
def test_inject_literal_mold_host_path(tmp_path: Path) -> None:
    """In host mode the exported literal is the absolute host path under KAS_WORK_DIR."""
    cfg = _cfg(tmp_path, mold=True, host_mode=True)
    text = 'local_conf_header:\n  zz-bakar-60-mold: |\n    INHERIT += "mold"\n'

    injected = _inject_literal_mold(cfg, text)

    expected = str(cfg.bsp_root / "mold-linklog.jsonl")
    assert f'export BAKAR_MOLD_LINKLOG = "{expected}"' in injected
    assert "/work/" not in injected.split("BAKAR_MOLD_LINKLOG")[1]


@pytest.mark.unit
def test_inject_literal_mold_idempotent(tmp_path: Path) -> None:
    """Re-running the injector does not append a second literal."""
    cfg = _cfg(tmp_path, mold=True, host_mode=False)
    text = 'local_conf_header:\n  zz-bakar-60-mold: |\n    INHERIT += "mold"\n'

    once = _inject_literal_mold(cfg, text)
    twice = _inject_literal_mold(cfg, once)

    assert once == twice
    assert twice.count("BAKAR_MOLD_LINKLOG") == 1


@pytest.mark.unit
def test_inject_literal_mold_emits_mode_for_baseline(tmp_path: Path) -> None:
    """A non-list mode (baseline) is written into local.conf so the arm is reachable."""
    cfg = _cfg(tmp_path, mold=True, mold_mode="baseline", host_mode=False)
    text = 'local_conf_header:\n  zz-bakar-60-mold: |\n    INHERIT += "mold"\n'

    injected = _inject_literal_mold(cfg, text)

    assert 'MOLD_MODE = "baseline"' in injected
    # The link-log literal is still emitted alongside the mode.
    assert "BAKAR_MOLD_LINKLOG" in injected
    # Idempotent: neither line is duplicated on a second pass.
    twice = _inject_literal_mold(cfg, injected)
    assert twice == injected
    assert twice.count("MOLD_MODE") == 1


@pytest.mark.unit
def test_inject_literal_mold_omits_mode_for_list(tmp_path: Path) -> None:
    """List is the bbclass default (MOLD_MODE ??= "list"), so no line is written for it."""
    cfg = _cfg(tmp_path, mold=True, mold_mode="list", host_mode=False)
    text = 'local_conf_header:\n  zz-bakar-60-mold: |\n    INHERIT += "mold"\n'

    injected = _inject_literal_mold(cfg, text)

    assert "MOLD_MODE" not in injected
