"""The remote build description document: encoding and strict parsing."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from bakar.feed_delta import RecipeEntry
from bakar.feed_describe import (
    BuildDescription,
    DescribedFile,
    DescriptionError,
    LayerCommit,
    encode,
    parse,
)

SHA = "a" * 64
COMMIT = "b" * 40


def _entry() -> RecipeEntry:
    return RecipeEntry(
        recipe="hello",
        packages=["hello", "hello-dev"],
        layer="meta-avocado",
        epoch=1,
        pv="1.2.3",
        pr="r0.1",
        srcrev="c" * 40,
        layer_commit=COMMIT,
        machine="qemux86-64",
        release="2025.1",
        task_hash="d" * 64,
    )


def _desc(**over: Any) -> BuildDescription:
    base: dict[str, Any] = {
        "schema_version": 1,
        "run_id": "20261112-101500-4242",
        "state": "succeeded",
        "exit_code": 0,
        "machine": "qemux86-64",
        "release": "2025.1",
        "channel": "dev",
        "tmpdir": "/ws/build/tmp",
        "deploy_dir": "/ws/build/tmp/deploy",
        "live": False,
        "live_reason": None,
        "files": (DescribedFile("rpm/core2_64/hello-1.2.3-r0.1.rpm", 12, SHA),),
        "entries": (_entry(),),
        "layers": (LayerCommit("meta-avocado", COMMIT), LayerCommit("meta-x", None)),
        "bitbake_commit": COMMIT,
        "warnings": ("two layers provide hello",),
    }
    base.update(over)
    return BuildDescription(**base)


def _doc(**over: Any) -> dict[str, Any]:
    return json.loads(encode(_desc())) | over


def _text(doc: dict[str, Any]) -> str:
    return json.dumps(doc)


def _err(text: str) -> str:
    with pytest.raises(DescriptionError) as exc:
        parse(text)
    return str(exc.value)


def test_round_trip_equals_original() -> None:
    original = _desc()
    assert parse(encode(original)) == original


def test_round_trip_preserves_every_entry_field() -> None:
    parsed = parse(encode(_desc()))
    assert parsed.entries == (_entry(),)
    assert parsed.entries[0].packages == ["hello", "hello-dev"]
    assert parsed.entries[0].srcrev == "c" * 40
    assert parsed.entries[0].task_hash == "d" * 64


def test_empty_description_round_trips() -> None:
    failed = _desc(
        state="failed",
        exit_code=1,
        files=(),
        entries=(),
        layers=(),
        bitbake_commit=None,
        warnings=(),
        live_reason=None,
    )
    parsed = parse(encode(failed))
    assert parsed == failed
    assert parsed.files == () and parsed.entries == () and parsed.layers == ()


def test_encode_is_one_sorted_json_document() -> None:
    text = encode(_desc())
    doc = json.loads(text)
    assert list(doc) == sorted(doc)
    assert text == json.dumps(doc, sort_keys=True)
    assert "\n" not in text.strip()


def test_dataclasses_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        _desc().state = "failed"  # type: ignore[misc]


def test_invalid_json_names_document() -> None:
    assert "document" in _err("{not json")


def test_text_after_document_is_rejected() -> None:
    msg = _err(encode(_desc()) + " trailing")
    assert "after the document" in msg


def test_document_over_four_mib_is_rejected() -> None:
    msg = _err(_text(_doc(warnings=["x" * (4 * 1024 * 1024)])))
    assert "4 MiB" in msg


@pytest.mark.parametrize("version", [2, 0, "1", True, None])
def test_schema_version_other_than_one(version: Any) -> None:
    assert "schema_version" in _err(_text(_doc(schema_version=version)))


def test_top_level_not_an_object() -> None:
    assert "document" in _err("[]")


@pytest.mark.parametrize(
    "key",
    [
        "run_id",
        "state",
        "exit_code",
        "machine",
        "release",
        "channel",
        "tmpdir",
        "deploy_dir",
        "live",
        "live_reason",
        "files",
        "entries",
        "layers",
        "bitbake_commit",
        "warnings",
    ],
)
def test_missing_top_level_key(key: str) -> None:
    doc = _doc()
    del doc[key]
    assert key in _err(_text(doc))


def test_unknown_top_level_key() -> None:
    assert "surprise" in _err(_text(_doc(surprise=1)))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("run_id", 5),
        ("machine", None),
        ("release", 1),
        ("channel", []),
        ("tmpdir", 3),
        ("deploy_dir", None),
        ("live_reason", 3),
        ("files", {}),
        ("entries", "x"),
        ("layers", 1),
        ("warnings", "x"),
        ("warnings", [1]),
    ],
)
def test_mistyped_top_level_key(key: str, value: Any) -> None:
    assert key in _err(_text(_doc(**{key: value})))


def test_state_outside_vocabulary() -> None:
    assert "state" in _err(_text(_doc(state="done")))


@pytest.mark.parametrize("code", ["0", 1.5, True])
def test_exit_code_not_int_or_null(code: Any) -> None:
    assert "exit_code" in _err(_text(_doc(exit_code=code)))


def test_exit_code_null_is_accepted() -> None:
    assert parse(_text(_doc(exit_code=None, state="running"))).exit_code is None


@pytest.mark.parametrize("live", [1, "true", None])
def test_live_not_bool(live: Any) -> None:
    assert "live" in _err(_text(_doc(live=live)))


@pytest.mark.parametrize("commit", ["b" * 39, "B" * 40, "g" * 40, 5, ""])
def test_bitbake_commit_neither_null_nor_hex40(commit: Any) -> None:
    assert "bitbake_commit" in _err(_text(_doc(bitbake_commit=commit)))


def _file(**over: Any) -> dict[str, Any]:
    return {"path": "rpm/avocado-repo.map", "size": 1, "sha256": SHA} | over


@pytest.mark.parametrize("key", ["path", "size", "sha256"])
def test_missing_file_key(key: str) -> None:
    f = _file()
    del f[key]
    assert key in _err(_text(_doc(files=[f])))


def test_unknown_file_key() -> None:
    assert "mode" in _err(_text(_doc(files=[_file(mode=420)])))


def test_file_not_an_object() -> None:
    assert "files" in _err(_text(_doc(files=["rpm/x"])))


def test_mistyped_file_path() -> None:
    assert "path" in _err(_text(_doc(files=[_file(path=3)])))


@pytest.mark.parametrize("size", [-1, 1.5, "12", True, False, None])
def test_size_not_non_negative_int(size: Any) -> None:
    assert "size" in _err(_text(_doc(files=[_file(size=size)])))


def test_size_zero_is_accepted() -> None:
    assert parse(_text(_doc(files=[_file(size=0)]))).files[0].size == 0


@pytest.mark.parametrize("sha", ["a" * 63, "a" * 65, "A" * 64, "g" * 64, 5, None, ""])
def test_sha256_not_64_lowercase_hex(sha: Any) -> None:
    assert "sha256" in _err(_text(_doc(files=[_file(sha256=sha)])))


@pytest.mark.parametrize("key", ["repo", "commit"])
def test_missing_layer_key(key: str) -> None:
    layer = {"repo": "meta-x", "commit": COMMIT}
    del layer[key]
    assert key in _err(_text(_doc(layers=[layer])))


def test_unknown_layer_key() -> None:
    layer = {"repo": "meta-x", "commit": COMMIT, "branch": "main"}
    assert "branch" in _err(_text(_doc(layers=[layer])))


def test_mistyped_layer_repo() -> None:
    assert "repo" in _err(_text(_doc(layers=[{"repo": 1, "commit": COMMIT}])))


@pytest.mark.parametrize("commit", ["b" * 39, "B" * 40, 5])
def test_layer_commit_neither_null_nor_hex40(commit: Any) -> None:
    layer = {"repo": "meta-x", "commit": commit}
    assert "commit" in _err(_text(_doc(layers=[layer])))


def _entry_doc(**over: Any) -> dict[str, Any]:
    return dataclasses.asdict(_entry()) | over


@pytest.mark.parametrize("key", [f.name for f in dataclasses.fields(RecipeEntry)], ids=str)
def test_missing_entry_key(key: str) -> None:
    e = _entry_doc()
    del e[key]
    assert key in _err(_text(_doc(entries=[e])))


def test_unknown_entry_key() -> None:
    assert "extra" in _err(_text(_doc(entries=[_entry_doc(extra=1)])))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("recipe", 1),
        ("packages", "hello"),
        ("packages", [1]),
        ("layer", None),
        ("epoch", "1"),
        ("epoch", True),
        ("pv", 1),
        ("pr", None),
        ("srcrev", 5),
        ("layer_commit", 5),
        ("machine", None),
        ("release", 1),
        ("task_hash", 5),
    ],
)
def test_mistyped_entry_key(key: str, value: Any) -> None:
    assert key in _err(_text(_doc(entries=[_entry_doc(**{key: value})])))


def test_entry_not_an_object() -> None:
    assert "entries" in _err(_text(_doc(entries=["hello"])))


def test_entry_nullable_fields_accept_null() -> None:
    e = _entry_doc(srcrev=None, layer_commit=None, task_hash=None)
    parsed = parse(_text(_doc(entries=[e]))).entries[0]
    assert (parsed.srcrev, parsed.layer_commit, parsed.task_hash) == (None, None, None)
