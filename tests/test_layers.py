"""Unit tests for the helpers in :mod:`bakar.layers`.

Targets the under-covered helpers around ``collect_layer_hashes`` -
``_resolve_bblayers_paths``, ``_find_bitbake_dir``, ``_read_bitbake_version``,
``_git_short_hash``, ``_git_branch``, ``discover_source_repos`` - plus the
BYO/generic branch of ``collect_layer_hashes`` that exercises Strategy 2 path
resolution. Companion to ``test_layer_hashes.py`` which covers the
NXP ``/sources/<repo>`` (Strategy 1) branch.

Every git invocation is patched at ``bakar.layers.subprocess.run`` so the
suite runs hermetically (no real ``git`` calls).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bakar.config import resolve
from bakar.layers import (
    LayerHash,
    _find_bitbake_dir,
    _git_branch,
    _git_short_hash,
    _read_bitbake_version,
    _resolve_bblayers_paths,
    collect_layer_hashes,
    discover_source_repos,
)

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Completed:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def _nxp_cfg(tmp_path: Path):
    """Resolve an nxp BuildConfig rooted at a tmp_path workspace."""
    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    return resolve(workspace=tmp_path, bsp_family="nxp")


def _generic_cfg(tmp_path: Path, kas_yaml: Path):
    """Resolve a generic BuildConfig anchored at a tmp_path kas yaml.

    Generic mode's ``bsp_root`` derives from the kas yaml's parent dir,
    which is where ``_resolve_bblayers_paths`` walks ``${TOPDIR}`` from.
    """
    return resolve(workspace=tmp_path, bsp_family="generic", kas_yaml=kas_yaml)


# ---------------------------------------------------------------------------
# _resolve_bblayers_paths
# ---------------------------------------------------------------------------


def test_resolve_bblayers_paths_two_topdir_layers(tmp_path: Path) -> None:
    """Two ``${TOPDIR}``-relative paths resolve to two distinct repo entries."""
    build = tmp_path / "build"
    conf = build / "conf"
    conf.mkdir(parents=True)
    # Two layers, each living in a sibling of build/: layers/poky/meta and
    # layers/meta-imx/meta-imx-bsp. _resolve_bblayers_paths replaces
    # ${TOPDIR} with str(build), then resolves each token via Path.resolve().
    layers = tmp_path / "layers"
    (layers / "poky" / "meta").mkdir(parents=True)
    (layers / "meta-imx" / "meta-imx-bsp").mkdir(parents=True)
    bblayers = conf / "bblayers.conf"
    bblayers.write_text(
        'BBLAYERS ?= " \\\n  ${TOPDIR}/../layers/poky/meta \\\n  ${TOPDIR}/../layers/meta-imx/meta-imx-bsp \\\n"\n'
    )

    # git rev-parse --show-toplevel returns each layer token's parent dir
    # (the repo root, by convention). We key on the -C argument.
    def fake_run(argv, **kwargs):
        target = argv[2]
        if target.endswith("/poky/meta"):
            return _Completed(0, str(layers / "poky") + "\n")
        if target.endswith("/meta-imx/meta-imx-bsp"):
            return _Completed(0, str(layers / "meta-imx") + "\n")
        return _Completed(128, "")

    with patch("bakar.layers.subprocess.run", side_effect=fake_run):
        result = _resolve_bblayers_paths(bblayers)

    assert set(result.keys()) == {"poky", "meta-imx"}
    assert result["poky"] == layers / "poky"
    assert result["meta-imx"] == layers / "meta-imx"


def test_resolve_bblayers_paths_empty_bblayers(tmp_path: Path) -> None:
    """A bblayers.conf with no BBLAYERS assignment yields an empty dict."""
    build = tmp_path / "build"
    conf = build / "conf"
    conf.mkdir(parents=True)
    bblayers = conf / "bblayers.conf"
    bblayers.write_text("# no BBLAYERS here\n")

    with patch("bakar.layers.subprocess.run") as run:
        result = _resolve_bblayers_paths(bblayers)

    assert result == {}
    run.assert_not_called()


def test_resolve_bblayers_paths_dedupes_shared_git_root(tmp_path: Path) -> None:
    """Two sublayers under one git root collapse to a single entry."""
    build = tmp_path / "build"
    conf = build / "conf"
    conf.mkdir(parents=True)
    poky = tmp_path / "layers" / "poky"
    (poky / "meta").mkdir(parents=True)
    (poky / "meta-poky").mkdir(parents=True)
    bblayers = conf / "bblayers.conf"
    bblayers.write_text(
        'BBLAYERS ?= " \\\n  ${TOPDIR}/../layers/poky/meta \\\n  ${TOPDIR}/../layers/poky/meta-poky \\\n"\n'
    )

    def fake_run(argv, **kwargs):
        # Both sublayers resolve to the same git root.
        return _Completed(0, str(poky) + "\n")

    with patch("bakar.layers.subprocess.run", side_effect=fake_run):
        result = _resolve_bblayers_paths(bblayers)

    assert list(result.keys()) == ["poky"]
    assert result["poky"] == poky


# ---------------------------------------------------------------------------
# _read_bitbake_version
# ---------------------------------------------------------------------------


def test_read_bitbake_version_returns_string(tmp_path: Path) -> None:
    """``__version__ = "2.8.0"`` in lib/bb/__init__.py is extracted verbatim."""
    bb_dir = tmp_path / "bitbake"
    init_py = bb_dir / "lib" / "bb" / "__init__.py"
    init_py.parent.mkdir(parents=True)
    init_py.write_text('# header\n__version__ = "2.8.0"\n# trailer\n')

    assert _read_bitbake_version(bb_dir) == "2.8.0"


def test_read_bitbake_version_missing_init_returns_none(tmp_path: Path) -> None:
    """A bitbake dir without ``lib/bb/__init__.py`` returns ``None``."""
    bb_dir = tmp_path / "bitbake"
    bb_dir.mkdir()

    assert _read_bitbake_version(bb_dir) is None


def test_read_bitbake_version_no_version_pragma_returns_none(tmp_path: Path) -> None:
    """An ``__init__.py`` without a ``__version__`` line returns ``None``."""
    bb_dir = tmp_path / "bitbake"
    init_py = bb_dir / "lib" / "bb" / "__init__.py"
    init_py.parent.mkdir(parents=True)
    init_py.write_text("# no version here\n")

    assert _read_bitbake_version(bb_dir) is None


# ---------------------------------------------------------------------------
# _find_bitbake_dir
# ---------------------------------------------------------------------------


def test_find_bitbake_dir_uses_bsp_bitbake_path(tmp_path: Path) -> None:
    """The BSP-bundled bitbake path is preferred when populated."""
    cfg = _nxp_cfg(tmp_path)
    bb = cfg.bsp_bitbake_path
    (bb / "lib" / "bb").mkdir(parents=True)
    (bb / "lib" / "bb" / "__init__.py").write_text('__version__ = "2.0.0"\n')

    assert _find_bitbake_dir(cfg, []) == bb


def test_find_bitbake_dir_falls_back_to_layer_sibling(tmp_path: Path) -> None:
    """No BSP bitbake: look for a ``bitbake/`` sibling of layer roots."""
    # Generic config: no BSP-bundled bitbake exists.
    yaml = tmp_path / "kas.yml"
    yaml.write_text("")
    cfg = _generic_cfg(tmp_path, yaml)
    layer_root = tmp_path / "layers" / "poky"
    layer_root.mkdir(parents=True)
    sibling_bitbake = tmp_path / "layers" / "bitbake"
    (sibling_bitbake / "lib" / "bb").mkdir(parents=True)
    (sibling_bitbake / "lib" / "bb" / "__init__.py").write_text('__version__ = "2.8.0"\n')

    assert _find_bitbake_dir(cfg, [layer_root]) == sibling_bitbake


def test_find_bitbake_dir_returns_none_when_absent(tmp_path: Path) -> None:
    """No BSP bitbake and no sibling bitbake -> ``None``."""
    yaml = tmp_path / "kas.yml"
    yaml.write_text("")
    cfg = _generic_cfg(tmp_path, yaml)
    layer_root = tmp_path / "layers" / "poky"
    layer_root.mkdir(parents=True)

    assert _find_bitbake_dir(cfg, [layer_root]) is None


# ---------------------------------------------------------------------------
# _git_short_hash / _git_branch
# ---------------------------------------------------------------------------


def test_git_short_hash_returns_none_on_non_git_path(tmp_path: Path) -> None:
    """A plain (non-git) directory yields ``None`` (rev-parse exit non-zero)."""

    def fake_run(argv, **kwargs):
        return _Completed(128, "")

    with patch("bakar.layers.subprocess.run", side_effect=fake_run):
        assert _git_short_hash(tmp_path) is None


def test_git_short_hash_returns_stripped_stdout(tmp_path: Path) -> None:
    """Successful rev-parse returns the trailing-newline-stripped short hash."""

    def fake_run(argv, **kwargs):
        return _Completed(0, "abc1234\n")

    with patch("bakar.layers.subprocess.run", side_effect=fake_run):
        assert _git_short_hash(tmp_path) == "abc1234"


def test_git_short_hash_oserror_returns_none(tmp_path: Path) -> None:
    """``OSError`` raised by subprocess (e.g. git not installed) -> ``None``."""
    with patch("bakar.layers.subprocess.run", side_effect=OSError("no git")):
        assert _git_short_hash(tmp_path) is None


def test_git_branch_returns_empty_on_non_git_path(tmp_path: Path) -> None:
    """A non-git directory yields the empty string (detached-HEAD fallback)."""

    def fake_run(argv, **kwargs):
        return _Completed(128, "")

    with patch("bakar.layers.subprocess.run", side_effect=fake_run):
        assert _git_branch(tmp_path) == ""


def test_git_branch_returns_stripped_branch_name(tmp_path: Path) -> None:
    """Successful ``branch --show-current`` returns the stripped branch."""

    def fake_run(argv, **kwargs):
        return _Completed(0, "main\n")

    with patch("bakar.layers.subprocess.run", side_effect=fake_run):
        assert _git_branch(tmp_path) == "main"


def test_git_branch_oserror_returns_empty(tmp_path: Path) -> None:
    """``OSError`` from subprocess returns the empty string."""
    with patch("bakar.layers.subprocess.run", side_effect=OSError("no git")):
        assert _git_branch(tmp_path) == ""


# ---------------------------------------------------------------------------
# discover_source_repos
# ---------------------------------------------------------------------------


def test_discover_source_repos_finds_two_git_dirs(tmp_path: Path) -> None:
    """Two ``sources/<repo>/.git`` dirs both appear in the result, sorted."""
    cfg = _nxp_cfg(tmp_path)
    sources = cfg.bsp_root / "sources"
    (sources / "poky" / ".git").mkdir(parents=True)
    (sources / "meta-imx" / ".git").mkdir(parents=True)

    result = discover_source_repos(cfg)

    assert [name for name, _ in result] == ["meta-imx", "poky"]
    paths = {name: path for name, path in result}
    assert paths["poky"] == (sources / "poky").resolve()
    assert paths["meta-imx"] == (sources / "meta-imx").resolve()


def test_discover_source_repos_empty_when_no_sources(tmp_path: Path) -> None:
    """No ``sources/`` and no ``layers/`` directory -> empty list."""
    cfg = _nxp_cfg(tmp_path)

    assert discover_source_repos(cfg) == []


def test_discover_source_repos_dedupes_across_sources_and_layers(tmp_path: Path) -> None:
    """A repo present in both ``sources/`` and ``layers/`` appears once."""
    cfg = _nxp_cfg(tmp_path)
    (cfg.bsp_root / "sources" / "poky" / ".git").mkdir(parents=True)
    (cfg.bsp_root / "layers" / "poky" / ".git").mkdir(parents=True)
    (cfg.bsp_root / "layers" / "meta-extra" / ".git").mkdir(parents=True)

    result = discover_source_repos(cfg)
    names = [name for name, _ in result]
    assert names == ["meta-extra", "poky"]


def test_discover_source_repos_skips_non_git_subdirs(tmp_path: Path) -> None:
    """A subdir without a ``.git`` entry is not reported."""
    cfg = _nxp_cfg(tmp_path)
    (cfg.bsp_root / "sources" / "poky" / ".git").mkdir(parents=True)
    (cfg.bsp_root / "sources" / "not-a-repo").mkdir(parents=True)

    result = discover_source_repos(cfg)
    assert [name for name, _ in result] == ["poky"]


# ---------------------------------------------------------------------------
# collect_layer_hashes (BYO/generic Strategy-2 path)
# ---------------------------------------------------------------------------


def test_collect_layer_hashes_byo_strategy_with_bitbake(tmp_path: Path) -> None:
    """A BYO bblayers.conf (no ``/sources/``) routes through Strategy 2.

    Patches ``_git_short_hash`` and ``_git_branch`` to fixed values so the
    test exercises the ``_resolve_bblayers_paths`` -> ``LayerHash`` plumbing
    plus the appended ``bitbake`` entry without depending on real git.
    """
    # Build a generic workspace whose bsp_root holds the build/ tree and
    # a sibling layers/ tree (Strategy-2 layout).
    yaml = tmp_path / "kas.yml"
    yaml.write_text("")
    cfg = _generic_cfg(tmp_path, yaml)
    conf = cfg.bblayers_conf
    conf.parent.mkdir(parents=True)
    layers = tmp_path / "layers"
    poky = layers / "poky"
    (poky / "meta").mkdir(parents=True)
    bitbake_root = layers / "bitbake"
    (bitbake_root / "lib" / "bb").mkdir(parents=True)
    (bitbake_root / "lib" / "bb" / "__init__.py").write_text('__version__ = "2.8.0"\n')

    conf.write_text('BBLAYERS ?= " \\\n  ${TOPDIR}/../layers/poky/meta \\\n"\n')

    # _resolve_bblayers_paths runs git rev-parse --show-toplevel for each
    # token; return the layer root. _git_short_hash / _git_branch are
    # patched separately so their subprocess.run is never reached.
    def fake_rev_parse(argv, **kwargs):
        return _Completed(0, str(poky) + "\n")

    with (
        patch("bakar.layers.subprocess.run", side_effect=fake_rev_parse),
        patch("bakar.layers._git_short_hash", return_value="abc1234"),
        patch("bakar.layers._git_branch", return_value="main"),
    ):
        result = collect_layer_hashes(cfg)

    # Resolved layers are sorted by repo name first; the bitbake entry is
    # appended last (carrying the version read from lib/bb/__init__.py).
    assert result == [
        LayerHash(repo="poky", short_hash="abc1234", branch="main", version=None),
        LayerHash(repo="bitbake", short_hash="abc1234", branch="main", version="2.8.0"),
    ]
