"""The build description document a remote host prints for ``bakar feed describe``.

The document is untrusted input: it comes from another machine over ssh. Parsing
is therefore strict. It rejects anything but one JSON object of exactly the
expected keys and types, so a later step can rely on the shapes it gets back.

Foundation module: standard library and ``bakar.feed_delta`` only. ``files`` paths
are relative to the deploy root (the directory holding ``rpm`` and ``images``).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, fields
from typing import Any

from bakar.feed_delta import RecipeEntry

SCHEMA_VERSION = 1
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
STATES = ("succeeded", "failed", "running")

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


class DescriptionError(ValueError):
    """The document is not a valid build description; the message names the key."""


@dataclass(frozen=True)
class DescribedFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class LayerCommit:
    repo: str
    commit: str | None


@dataclass(frozen=True)
class BuildDescription:
    schema_version: int
    run_id: str
    state: str
    exit_code: int | None
    machine: str
    release: str
    channel: str
    tmpdir: str
    deploy_dir: str
    live: bool
    live_reason: str | None
    files: tuple[DescribedFile, ...]
    entries: tuple[RecipeEntry, ...]
    layers: tuple[LayerCommit, ...]
    bitbake_commit: str | None
    warnings: tuple[str, ...]


def encode(description: BuildDescription) -> str:
    """One JSON document with sorted keys."""
    return json.dumps(asdict(description), sort_keys=True)


def parse(text: str) -> BuildDescription:
    """Parse and validate a description; raise ``DescriptionError`` naming the key."""
    if len(text.encode("utf-8", "surrogatepass")) > MAX_DOCUMENT_BYTES:
        raise DescriptionError("document is larger than 4 MiB")
    doc = _load(text)
    top = _object(doc, "document", [f.name for f in fields(BuildDescription)])
    if type(top["schema_version"]) is not int or top["schema_version"] != SCHEMA_VERSION:
        raise DescriptionError(f"schema_version must be {SCHEMA_VERSION}, got {top['schema_version']!r}")
    state = _str(top, "state")
    if state not in STATES:
        raise DescriptionError(f"state must be one of {', '.join(STATES)}, got {state!r}")
    exit_code = top["exit_code"]
    if exit_code is not None and type(exit_code) is not int:
        raise DescriptionError(f"exit_code must be an integer or null, got {exit_code!r}")
    live = top["live"]
    if type(live) is not bool:
        raise DescriptionError(f"live must be a boolean, got {live!r}")
    return BuildDescription(
        schema_version=SCHEMA_VERSION,
        run_id=_str(top, "run_id"),
        state=state,
        exit_code=exit_code,
        machine=_str(top, "machine"),
        release=_str(top, "release"),
        channel=_str(top, "channel"),
        tmpdir=_str(top, "tmpdir"),
        deploy_dir=_str(top, "deploy_dir"),
        live=live,
        live_reason=_str_or_none(top, "live_reason"),
        files=tuple(_file(item) for item in _list(top, "files")),
        entries=tuple(_entry(item) for item in _list(top, "entries")),
        layers=tuple(_layer(item) for item in _list(top, "layers")),
        bitbake_commit=_commit(top, "bitbake_commit"),
        warnings=tuple(_str_items(top, "warnings")),
    )


def _load(text: str) -> object:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise DescriptionError(f"duplicate key {key!r} in document")
            out[key] = value
        return out

    def no_constant(name: str) -> Any:
        raise DescriptionError(f"document holds the non-JSON constant {name}")

    decoder = json.JSONDecoder(object_pairs_hook=no_duplicates, parse_constant=no_constant)
    stripped = text.lstrip()
    try:
        doc, end = decoder.raw_decode(stripped)
    except DescriptionError:
        raise
    # ValueError covers JSONDecodeError and an integer past the str-to-int digit
    # limit; RecursionError is nesting deeper than the decoder's stack.
    except (ValueError, RecursionError) as exc:
        raise DescriptionError(f"document is not valid JSON: {exc}") from exc
    if stripped[end:].strip():
        raise DescriptionError("document has text after the document")
    return doc


def _object(value: object, where: str, keys: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DescriptionError(f"{where} must be a JSON object")
    missing = [k for k in keys if k not in value]
    if missing:
        raise DescriptionError(f"{where} is missing key {missing[0]!r}")
    unknown = sorted(k for k in value if k not in keys)
    if unknown:
        raise DescriptionError(f"{where} has unknown key {unknown[0]!r}")
    return value


def _str(obj: dict[str, Any], key: str) -> str:
    value = obj[key]
    if not isinstance(value, str):
        raise DescriptionError(f"{key} must be a string, got {value!r}")
    return value


def _str_or_none(obj: dict[str, Any], key: str) -> str | None:
    return None if obj[key] is None else _str(obj, key)


def _list(obj: dict[str, Any], key: str) -> list[Any]:
    value = obj[key]
    if not isinstance(value, list):
        raise DescriptionError(f"{key} must be a list, got {value!r}")
    return value


def _str_items(obj: dict[str, Any], key: str) -> list[str]:
    items = _list(obj, key)
    for item in items:
        if not isinstance(item, str):
            raise DescriptionError(f"{key} must hold strings, got {item!r}")
    return items


def _commit(obj: dict[str, Any], key: str) -> str | None:
    value = obj[key]
    if value is None:
        return None
    if not isinstance(value, str) or not _COMMIT_RE.fullmatch(value):
        raise DescriptionError(f"{key} must be null or 40 lowercase hex characters, got {value!r}")
    return value


def _file(item: object) -> DescribedFile:
    obj = _object(item, "files entry", ["path", "size", "sha256"])
    size = obj["size"]
    if type(size) is not int or size < 0:
        raise DescriptionError(f"size must be a non-negative integer, got {size!r}")
    sha = obj["sha256"]
    if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
        raise DescriptionError(f"sha256 must be 64 lowercase hex characters, got {sha!r}")
    return DescribedFile(path=_str(obj, "path"), size=size, sha256=sha)


def _layer(item: object) -> LayerCommit:
    obj = _object(item, "layers entry", ["repo", "commit"])
    return LayerCommit(repo=_str(obj, "repo"), commit=_commit(obj, "commit"))


def _entry(item: object) -> RecipeEntry:
    obj = _object(item, "entries entry", [f.name for f in fields(RecipeEntry)])
    epoch = obj["epoch"]
    if type(epoch) is not int:
        raise DescriptionError(f"epoch must be an integer, got {epoch!r}")
    return RecipeEntry(
        recipe=_str(obj, "recipe"),
        packages=_str_items(obj, "packages"),
        layer=_str(obj, "layer"),
        epoch=epoch,
        pv=_str(obj, "pv"),
        pr=_str(obj, "pr"),
        srcrev=_str_or_none(obj, "srcrev"),
        layer_commit=_str_or_none(obj, "layer_commit"),
        machine=_str(obj, "machine"),
        release=_str(obj, "release"),
        task_hash=_str_or_none(obj, "task_hash"),
    )
