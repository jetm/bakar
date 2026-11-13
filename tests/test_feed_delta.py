"""Delta writer tests.

``pr`` here is the RPM release as named in the package file (PKGR, e.g.
``r0.1``), not the recipe's PR.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bakar import feed_delta

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _entry(**over) -> feed_delta.RecipeEntry:
    kw = {
        "recipe": "htop",
        "package_files": ["htop-3.5.0-r0.1.core2_64.rpm", "htop-dev-3.5.0-r0.1.core2_64.rpm"],
        "packages": ["htop", "htop-dev"],
        "layer": "meta-openembedded",
        "epoch": 0,
        "srcrev": None,
        "layer_commit": "f28a144aa0a0",
        "machine": "qemux86-64",
        "release": "wrynose",
        "task_hash": "abc123",
    }
    kw.update(over)
    return feed_delta.entry_from_package_files(**kw)


def test_parse_nevra_release_with_dot_and_dashed_name() -> None:
    n = feed_delta.parse_nevra("lib-foo-bar-1.2.3-r0.1.core2_64.rpm")
    assert (n.name, n.version, n.release, n.arch) == ("lib-foo-bar", "1.2.3", "r0.1", "core2_64")


@pytest.mark.parametrize("bad", ["htop.rpm", "htop-3.5.0.rpm", "x.rpm", ""])
def test_parse_nevra_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        feed_delta.parse_nevra(bad)


def test_entry_takes_pv_and_package_release_from_file_names() -> None:
    e = _entry()
    assert (e.pv, e.pr) == ("3.5.0", "r0.1")


def test_entry_rejects_disagreeing_package_files() -> None:
    with pytest.raises(ValueError, match="disagree"):
        _entry(package_files=["a-1.0-r0.1.noarch.rpm", "b-1.0-r0.2.noarch.rpm"])


def test_null_task_hash_is_explicit(tmp_path: Path) -> None:
    e = _entry(task_hash=None, srcrev="deadbeef", epoch=2)
    path = feed_delta.write_recipe_map(tmp_path, [e])
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "task_hash" in raw["entries"][0]
    assert raw["entries"][0]["task_hash"] is None
    assert e.delta_key() == ("2", "3.5.0", "r0.1", "deadbeef", "f28a144aa0a0")


def test_task_hash_key_ignores_other_fields() -> None:
    a = _entry(task_hash="h1", layer_commit="aaa")
    b = _entry(task_hash="h1", layer_commit="bbb")
    assert a.delta_key() == b.delta_key()
    assert a.delta_key() != _entry(task_hash="h2").delta_key()


def test_recipe_map_shape(tmp_path: Path) -> None:
    path = feed_delta.write_recipe_map(tmp_path, [_entry()])
    assert path == tmp_path / "recipe-map.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw == {
        "schema_version": 1,
        "entries": [
            {
                "recipe": "htop",
                "packages": ["htop", "htop-dev"],
                "layer": "meta-openembedded",
                "epoch": 0,
                "pv": "3.5.0",
                "pr": "r0.1",
                "srcrev": None,
                "layer_commit": "f28a144aa0a0",
                "machine": "qemux86-64",
                "release": "wrynose",
                "task_hash": "abc123",
            }
        ],
    }


def test_delta_shape_snapshot_id_null(tmp_path: Path) -> None:
    path = feed_delta.write_delta(
        tmp_path,
        base_release="wrynose",
        base_channel="stable",
        built_recipes=["htop"],
        bakar_run_id="20261111-204408-533300",
        delta_path="/srv/feed/delta",
    )
    assert path == tmp_path / "delta.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "base": {"release": "wrynose", "channel": "stable", "snapshot_id": None},
        "built_recipes": ["htop"],
        "bakar_run_id": "20261111-204408-533300",
        "delta_path": "/srv/feed/delta",
    }


def test_write_leaves_no_temp_files(tmp_path: Path) -> None:
    feed_delta.write_recipe_map(tmp_path, [])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["recipe-map.json"]


_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _source(**over) -> dict[str, object]:
    src: dict[str, object] = {
        "host": "pc2",
        "run_id": "20261111-204408-533300",
        "pulled_at": "2026-11-12T10:15:00+00:00",
        "observed_layers": {"meta-avocado": _COMMIT, "meta-openembedded": None},
        "pin_override": False,
    }
    src.update(over)
    return src


def _write(tmp_path: Path, **over) -> Path:
    return feed_delta.write_delta(
        tmp_path,
        base_release="wrynose",
        base_channel="stable",
        built_recipes=["htop"],
        bakar_run_id="20261111-204408-533300",
        delta_path="/srv/feed/delta",
        **over,
    )


def test_delta_without_source_is_byte_identical_to_previous_output(tmp_path: Path) -> None:
    path = _write(tmp_path)
    expected = {
        "schema_version": 1,
        "base": {"release": "wrynose", "channel": "stable", "snapshot_id": None},
        "built_recipes": ["htop"],
        "bakar_run_id": "20261111-204408-533300",
        "delta_path": "/srv/feed/delta",
    }
    assert path.read_text(encoding="utf-8") == json.dumps(expected, indent=2, sort_keys=True) + "\n"
    assert "source" not in json.loads(path.read_text(encoding="utf-8"))


def test_delta_with_source_records_exactly_the_source_keys(tmp_path: Path) -> None:
    raw = json.loads(_write(tmp_path, source=_source(pin_override=True)).read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert raw["source"] == {
        "host": "pc2",
        "run_id": "20261111-204408-533300",
        "pulled_at": "2026-11-12T10:15:00+00:00",
        "observed_layers": {"meta-avocado": _COMMIT, "meta-openembedded": None},
        "pin_override": True,
    }


@pytest.mark.parametrize(
    ("over", "key"),
    [
        ({"host": ""}, "host"),
        ({"run_id": ""}, "run_id"),
        ({"pulled_at": "yesterday"}, "pulled_at"),
        ({"pulled_at": "2026-11-12T10:15:00-06:00"}, "pulled_at"),
        ({"pin_override": 1}, "pin_override"),
        ({"observed_layers": {"meta-avocado": "abc123"}}, "observed_layers"),
        ({"observed_layers": {"meta-avocado": _COMMIT.upper()}}, "observed_layers"),
        ({"observed_layers": ["meta-avocado"]}, "observed_layers"),
    ],
)
def test_invalid_source_field_is_rejected_and_nothing_is_written(tmp_path: Path, over: dict, key: str) -> None:
    with pytest.raises(ValueError, match=key):
        _write(tmp_path, source=_source(**over))
    assert list(tmp_path.iterdir()) == []


def test_source_missing_key_is_rejected(tmp_path: Path) -> None:
    src = _source()
    del src["pin_override"]
    with pytest.raises(ValueError, match="pin_override"):
        _write(tmp_path, source=src)
    assert list(tmp_path.iterdir()) == []


def test_source_unknown_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="extra"):
        _write(tmp_path, source=_source(extra="x"))
    assert list(tmp_path.iterdir()) == []
