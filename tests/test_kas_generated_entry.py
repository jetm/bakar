"""A generated ``avocado-bakar.yml`` re-used as the kas entry must not carry bakar's own sections.

``kas dump`` writes its result to ``<bsp_root>/avocado-bakar.yml``, and that same file
is a supported entry YAML for the next run (``bsp_root`` keeps its parent). Without
intervention each dump therefore feeds back into itself: an accelerator switched on
once (mold, sccache, ...) leaves its ``zz-bakar-*`` block and ``meta-bakar-*`` layer in
the file, and switching it off in config changes nothing because the stale block is
inherited from the entry rather than produced by an overlay.
"""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
import yaml

from bakar.config import _overlay_dir

if TYPE_CHECKING:
    from pathlib import Path

    from bakar.config import BuildConfig

pytestmark = pytest.mark.unit

_STALE_GENERATED = """\
header:
  version: 16
env:
  BB_HASHSERVE: auto
  PATH: /usr/bin
  BAKAR_SCCACHE_DIR: /stale/sccache
  BAKAR_SSTATE_MIRROR_URL: http://stale.example/sstate
repos:
  meta-avocado:
    path: meta-avocado
    layers:
      meta-avocado-distro:
  meta-bakar-mold:
    path: .bakar/meta-bakar-mold
    layers:
      .:
  meta-bakar-sccache:
    path: .bakar/meta-bakar-sccache
    layers:
      .:
  meta-erlang:
    path: meta-erlang
    layers:
      .:
local_conf_header:
  machine-avocado-qemux86-64: |
    DISTRO_FEATURES_EXTRA:append = " seccomp"
  zz-bakar-10-base: |
    BB_NUMBER_THREADS = "8"
  zz-bakar-50-sccache: |
    INHERIT += "sccache"
  zz-bakar-60-mold: |
    INHERIT += "mold"
    MOLD_MODE = "global"
  zz-local-buildpaths-qa-skip: |
    INSANE_SKIP:go-runtime-dev:append = " buildpaths"
"""

# The main tuning overlays are applied on every run and re-supply their own env and
# layers, so only their local_conf_header key is bound by the naming convention.
_MAIN_TUNING = {"bakar-tuning-generic.yml", "bakar-tuning-nxp.yml", "bakar-tuning-ti.yml"}
_SECTIONS = ("local_conf_header", "repos", "env")
_OVERLAYS = sorted(_overlay_dir().glob("bakar-tuning-*.yml"))


def _cfg(tmp_path: Path, kas_yaml: Path) -> BuildConfig:
    from bakar.config import BuildConfig

    return BuildConfig(
        workspace=tmp_path,
        bsp_family="generic",  # type: ignore[arg-type]
        machine="generic",
        distro="generic",
        image="generic",
        manifest="",
        repo_url="",
        repo_branch="",
        kas_container_image="jetm/kas-build-env:latest",
        kas_yaml_override=kas_yaml,
    )


def _drive(cfg: BuildConfig, kas_yaml: Path, overlay_src: Path, *, dump_fails: bool = False) -> dict[str, Any]:
    """Run ``_build_kas_arg`` with a fake dump; return the entry as ``kas dump`` saw it."""
    from bakar.steps.kas_build import _build_kas_arg

    seen: dict[str, Any] = {}

    def fake_run_kas_dump(cfg: BuildConfig, wrapper: Path, overlay_rel: Path, extra_overlay_rels: Any = None) -> Path:
        wrapper_data = yaml.safe_load(wrapper.read_text(encoding="utf-8"))
        include = wrapper_data["header"]["includes"][0]
        repo_dir = cfg.workspace / wrapper_data["repos"][include["repo"]]["path"]
        entry = repo_dir / include["file"]
        seen["entry_path"] = entry
        seen["entry_text"] = entry.read_text(encoding="utf-8")
        if dump_fails:
            raise RuntimeError("kas dump failed")
        return cfg.bsp_root / "avocado-bakar.yml"

    expectation = pytest.raises(RuntimeError) if dump_fails else nullcontext()
    with (
        patch("bakar.steps.kas_build._run_kas_dump", fake_run_kas_dump),
        patch("bakar.steps.kas_build._setup_meta_avocado_build_dir"),
        expectation,
    ):
        _build_kas_arg(cfg, kas_yaml, overlay_src)
    return seen


def _generated_workspace(tmp_path: Path, content: str = _STALE_GENERATED) -> tuple[Path, Path]:
    (tmp_path / "meta-avocado").mkdir()
    build = tmp_path / "build-qemux86-64"
    build.mkdir()
    generated = build / "avocado-bakar.yml"
    generated.write_text(content, encoding="utf-8")
    overlay_src = tmp_path / "bakar-tuning-generic.yml"
    overlay_src.write_text("header:\n  version: 16\n", encoding="utf-8")
    return generated, overlay_src


def test_generated_entry_reaches_kas_dump_without_bakar_owned_sections(tmp_path: Path) -> None:
    generated, overlay_src = _generated_workspace(tmp_path)
    cfg = _cfg(tmp_path, generated)
    assert cfg.is_meta_avocado
    assert cfg.mold is False

    entry = yaml.safe_load(_drive(cfg, generated, overlay_src)["entry_text"])

    assert entry == {
        "header": {"version": 16},
        "env": {"BB_HASHSERVE": "auto", "PATH": "/usr/bin"},
        "repos": {
            "meta-avocado": {"path": "meta-avocado", "layers": {"meta-avocado-distro": None}},
            "meta-erlang": {"path": "meta-erlang", "layers": {".": None}},
        },
        "local_conf_header": {
            "machine-avocado-qemux86-64": 'DISTRO_FEATURES_EXTRA:append = " seccomp"\n',
            "zz-local-buildpaths-qa-skip": 'INSANE_SKIP:go-runtime-dev:append = " buildpaths"\n',
        },
    }


def test_generated_entry_original_is_untouched_when_the_dump_never_runs(tmp_path: Path) -> None:
    """Filtering happens on a copy: a failed ``kas dump`` must not corrupt the generated file."""
    generated, overlay_src = _generated_workspace(tmp_path)
    cfg = _cfg(tmp_path, generated)

    seen = _drive(cfg, generated, overlay_src, dump_fails=True)

    assert generated.read_text(encoding="utf-8") == _STALE_GENERATED
    assert seen["entry_path"] != generated


@pytest.mark.parametrize("dump_fails", [False, True], ids=["dump-ok", "dump-fails"])
def test_filtered_copy_does_not_outlive_the_dump(tmp_path: Path, dump_fails: bool) -> None:
    """The copy is a valid entry beside the generated file; leaving it behind invites re-use as one."""
    generated, overlay_src = _generated_workspace(tmp_path)
    cfg = _cfg(tmp_path, generated)

    seen = _drive(cfg, generated, overlay_src, dump_fails=dump_fails)

    assert seen["entry_path"].name == ".avocado-entry.yml"
    assert not seen["entry_path"].exists()


@pytest.mark.parametrize("content", ["header: [unclosed\n", "- just\n- a list\n", ""])
def test_unusable_generated_entry_is_left_for_kas_to_report(tmp_path: Path, content: str) -> None:
    """A hand-edited or empty generated file reaches kas as-is, so kas gives the parse error."""
    generated, overlay_src = _generated_workspace(tmp_path, content)
    cfg = _cfg(tmp_path, generated)

    seen = _drive(cfg, generated, overlay_src)

    assert seen["entry_path"] == generated
    assert seen["entry_text"] == content
    assert generated.read_text(encoding="utf-8") == content


def test_source_entry_is_passed_through_unchanged(tmp_path: Path) -> None:
    """Only the generated file is sanitised; a source machine YAML is included verbatim."""
    meta = tmp_path / "meta-avocado"
    machine = meta / "kas" / "machine" / "qemux86-64.yml"
    machine.parent.mkdir(parents=True)
    machine.write_text(_STALE_GENERATED, encoding="utf-8")
    overlay_src = tmp_path / "bakar-tuning-generic.yml"
    overlay_src.write_text("header:\n  version: 16\n", encoding="utf-8")
    cfg = _cfg(tmp_path, machine)

    seen = _drive(cfg, machine, overlay_src)

    assert seen["entry_path"] == machine
    assert seen["entry_text"] == _STALE_GENERATED
    assert machine.exists()


@pytest.mark.parametrize("overlay", _OVERLAYS, ids=lambda p: p.name)
def test_bundled_overlays_only_use_the_prefixes_the_filter_owns(overlay: Path) -> None:
    """A key outside the filter's prefixes would go sticky again; new overlays must follow the naming."""
    data = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    owned = {"local_conf_header": "zz-bakar-", "repos": "meta-bakar-", "env": "BAKAR_"}
    sections = ("local_conf_header",) if overlay.name in _MAIN_TUNING else _SECTIONS

    for section in sections:
        strays = [key for key in data.get(section) or {} if not str(key).startswith(owned[section])]
        assert not strays, f"{overlay.name} [{section}] has keys outside '{owned[section]}*': {strays}"


def test_stale_dump_built_from_every_bundled_overlay_is_fully_filtered(tmp_path: Path) -> None:
    """Whatever the accelerator overlays can contribute, none of it survives into the next entry."""
    stale: dict[str, Any] = {
        "header": {"version": 16},
        "env": {"BB_HASHSERVE": "auto", "PATH": "/usr/bin"},
        "repos": {"meta-avocado": {"path": "meta-avocado"}, "meta-erlang": {"path": "meta-erlang"}},
        "local_conf_header": {"machine-avocado-qemux86-64": "X = 1\n", "zz-local-buildpaths-qa-skip": "Y = 1\n"},
    }
    for overlay in _OVERLAYS:
        data = yaml.safe_load(overlay.read_text(encoding="utf-8"))
        for section in ("local_conf_header",) if overlay.name in _MAIN_TUNING else _SECTIONS:
            for key in data.get(section) or {}:
                stale[section][key] = {"path": f".bakar/{key}"} if section == "repos" else "stale"
    generated, overlay_src = _generated_workspace(tmp_path, yaml.dump(stale))
    cfg = _cfg(tmp_path, generated)

    entry = yaml.safe_load(_drive(cfg, generated, overlay_src)["entry_text"])

    assert set(entry["local_conf_header"]) == {"machine-avocado-qemux86-64", "zz-local-buildpaths-qa-skip"}
    assert set(entry["repos"]) == {"meta-avocado", "meta-erlang"}
    assert set(entry["env"]) == {"BB_HASHSERVE", "PATH"}
