"""A layer repository declared as a subdirectory of the checkout holding the kas entry.

Real entries (``meta-avocado-zedx``) declare their own layer as
``repos: {meta-avocado-zedx: {path: meta-avocado-zedx}}`` while the git checkout
root, which holds the kas file, is not itself a bitbake layer. The wrappers must
include the entry from the checkout root yet pin the layer repository at the
subdirectory. Checked against the real ``kas`` on fixtures under ``tmp_path``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest
import yaml

from bakar import bsp_detect
from bakar.steps import kas_pins

if TYPE_CHECKING:
    from pathlib import Path

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env={**os.environ, **_GIT_ENV}
    ).stdout.strip()


def _require_kas() -> None:
    if shutil.which("kas") is None:
        if os.environ.get("BAKAR_REQUIRE_KAS"):
            pytest.fail("kas is not on PATH but BAKAR_REQUIRE_KAS is set")
        pytest.skip("kas is not on PATH")


def _fixture(tmp_path: Path, layer_path: str | None) -> tuple[bsp_detect.LayerRepo, Path]:
    """Return (layer, dependency clone). ``layer_path`` is the entry's ``path`` for its layer repo."""
    clone = tmp_path / "ws" / "meta-avocado"
    (clone / "kas").mkdir(parents=True)
    _git(clone, "init", "-q", "-b", "main")
    (clone / "kas" / "base.yml").write_text("header:\n  version: 16\ndistro: pinned\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-q", "-m", "pinned")
    pin = _git(clone, "rev-parse", "HEAD")

    checkout = tmp_path / "meta-board-checkout"
    (checkout / "kas" / "machine").mkdir(parents=True)
    layer_dir = checkout if layer_path in (None, ".") else checkout / layer_path
    (layer_dir / "conf").mkdir(parents=True, exist_ok=True)
    (layer_dir / "conf" / "layer.conf").write_text("# layer\n")
    own: dict = {} if layer_path is None else {"path": layer_path}
    entry = checkout / "kas" / "machine" / "m.yml"
    entry.write_text(
        yaml.safe_dump(
            {
                "header": {"version": 16, "includes": [{"repo": "meta-avocado", "file": "kas/base.yml"}]},
                "machine": "m",
                "repos": {
                    "meta-board": own or None,
                    "meta-avocado": {"url": f"file://{clone}", "branch": "main", "commit": pin},
                },
            }
        )
    )
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-q", "-m", "entry")
    layer = bsp_detect.layer_repo_of(entry)
    assert layer is not None
    return layer, clone


def test_subdirectory_layer_repo_path_is_the_subdirectory(tmp_path: Path) -> None:
    layer, _clone = _fixture(tmp_path, "meta-board")
    assert layer.repo_path == (layer.checkout / "meta-board").resolve()


@pytest.mark.parametrize("layer_path", [None, "."])
def test_checkout_root_layer_repo_path_is_the_checkout(tmp_path: Path, layer_path: str | None) -> None:
    layer, _clone = _fixture(tmp_path, layer_path)
    assert layer.repo_path == layer.checkout


def test_root_layer_wrapper_is_unchanged(tmp_path: Path) -> None:
    layer, _clone = _fixture(tmp_path, ".")
    export = tmp_path / "export"
    probe = yaml.safe_load(bsp_detect.layer_probe_wrapper_text(layer, export))
    assert probe["header"]["includes"] == [{"repo": "meta-board", "file": "kas/machine/m.yml"}]
    assert probe["repos"]["meta-board"] == {"path": str(layer.checkout)}
    assert bsp_detect.LAYER_CHECKOUT_REPO not in probe["repos"]


def test_subdirectory_wrapper_includes_via_helper_and_pins_the_layer(tmp_path: Path) -> None:
    layer, _clone = _fixture(tmp_path, "meta-board")
    export = tmp_path / "export"
    helper = bsp_detect.LAYER_CHECKOUT_REPO
    for text in (bsp_detect.layer_probe_wrapper_text(layer, export), bsp_detect.layer_wrapper_text(layer, export)):
        doc = yaml.safe_load(text)
        assert doc["header"]["includes"] == [{"repo": helper, "file": "kas/machine/m.yml"}]
        assert doc["repos"]["meta-board"] == {"path": str(layer.checkout / "meta-board")}
        assert doc["repos"][helper]["path"] == str(layer.checkout)
        assert "layers" in doc["repos"][helper]
    pin = yaml.safe_load(bsp_detect.layer_pin_text(layer))
    assert pin["repos"] == {"meta-board": {"path": str(layer.checkout / "meta-board")}}


def test_real_kas_subdirectory_layer_is_the_layer_not_the_checkout(tmp_path: Path) -> None:
    _require_kas()
    layer, clone = _fixture(tmp_path, "meta-board")
    merged = kas_pins.read_merged_config_layer(layer, clone)
    repos = merged["repos"]
    assert repos["meta-board"]["path"] == str(layer.checkout / "meta-board")
    assert merged["distro"] == "pinned"
    # The checkout root is not a layer, so it must contribute no bblayers entry.
    helper = repos[bsp_detect.LAYER_CHECKOUT_REPO]
    assert helper["path"] == str(layer.checkout)
    assert helper["layers"] and all(v in ("excluded", "disabled", False) for v in helper["layers"].values())
    # The layer itself keeps kas's default layer (its repo root): no layers key.
    assert "layers" not in repos["meta-board"] or repos["meta-board"]["layers"] in (None, {})


def test_real_kas_root_layer_unchanged(tmp_path: Path) -> None:
    _require_kas()
    layer, clone = _fixture(tmp_path, ".")
    merged = kas_pins.read_merged_config_layer(layer, clone)
    assert merged["repos"]["meta-board"]["path"] == str(layer.checkout)
    assert bsp_detect.LAYER_CHECKOUT_REPO not in merged["repos"]
