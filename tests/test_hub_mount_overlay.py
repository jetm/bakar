"""Hub download-directory mount overlay: content, ordering, packaging and evaluation."""

from __future__ import annotations

import importlib.resources
import types
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_OVERLAY_DIR = Path(str(importlib.resources.files("bakar") / "overlays"))
_KEY = "zz-bakar-46-hub-mount"
_HUB_KEY = "zz-bakar-45-hub-cache"
_HUB_URL = "http://hub.test:8080"
_SCHEMES = ("https?", "ftp", "crate", "gomod")
_EXCLUDED_SCHEMES = ("git", "gitsm")
_PREFIX = "(https?|ftp|crate|gomod)://.*/.* "


def _load(name: str) -> dict:
    return yaml.safe_load((_OVERLAY_DIR / name).read_text())


def _expr_of(name: str, key: str) -> str:
    body = _load(name)["local_conf_header"][key]
    line = next(ln for ln in body.splitlines() if ln.startswith("PREMIRRORS:prepend"))
    value = line.split("=", 1)[1].strip()
    assert value.startswith('"${@')
    assert value.endswith('}"')
    return value[len('"${@') : -len('}"')]


def _premirrors_expr() -> str:
    return _expr_of("bakar-tuning-hub-mount.yml", _KEY)


class _Datastore:
    """The slice of BitBake's datastore the overlay's inline python touches."""

    def __init__(self, variables: dict[str, str]) -> None:
        self._variables = variables

    def getVar(self, name: str) -> str | None:  # noqa: N802 - BitBake's spelling
        return self._variables.get(name)


def _eval(expr: str, variables: dict[str, str], environ: dict[str, str] | None = None) -> str:
    stub_os = types.SimpleNamespace(environ=environ or {})
    # nosemgrep: python.lang.security.audit.eval-detected.eval-detected
    return eval(expr, {"os": stub_os, "d": _Datastore(variables)})


def _eval_premirrors(variables: dict[str, str], environ: dict[str, str] | None = None) -> str:
    return _eval(_premirrors_expr(), variables, environ)


def test_overlay_holds_one_premirrors_prepend() -> None:
    data = _load("bakar-tuning-hub-mount.yml")
    assert list(data["env"]) == ["BAKAR_DL_MIRROR_PATHS"]
    blocks = data["local_conf_header"]
    assert list(blocks) == [_KEY]
    body = blocks[_KEY]
    lines = [line for line in body.splitlines() if line.startswith("PREMIRRORS:prepend")]
    assert len(lines) == 1
    assert "BAKAR_DL_MIRROR_PATHS" in lines[0]
    for scheme in _SCHEMES:
        assert scheme in lines[0]
    for scheme in _EXCLUDED_SCHEMES:
        assert scheme not in lines[0]
    assert "SSTATE_MIRRORS" not in body
    assert "BB_HASHSERVE_UPSTREAM" not in body


def test_block_key_sorts_after_hub_cache_and_generic() -> None:
    generic = _load("bakar-tuning-generic.yml")["local_conf_header"]
    assert _KEY > _HUB_KEY
    assert all(other < _KEY for other in generic)


def test_overlay_ships_as_package_data() -> None:
    assert importlib.resources.files("bakar").joinpath("overlays/bakar-tuning-hub-mount.yml").is_file()


def test_two_directories_keep_their_order() -> None:
    value = _eval_premirrors({"BAKAR_DL_MIRROR_PATHS": "/a /b"})
    first = _PREFIX + "file:///a/ \\n "
    second = _PREFIX + "file:///b/ \\n "
    assert value == first + second
    assert value.index("file:///a/") < value.index("file:///b/")


def test_trailing_slash_is_not_doubled() -> None:
    value = _eval_premirrors({"BAKAR_DL_MIRROR_PATHS": "/a/ /b"})
    assert "file:///a/ " in value
    assert "//a//" not in value
    assert "file:///a// " not in value


@pytest.mark.parametrize("variables", [{}, {"BAKAR_DL_MIRROR_PATHS": ""}, {"BAKAR_DL_MIRROR_PATHS": "   "}])
def test_empty_or_absent_paths_yield_no_premirror(variables: dict[str, str]) -> None:
    assert _eval_premirrors(variables) == ""


def test_expression_reads_the_datastore_not_os_environ() -> None:
    assert "os.environ" not in _premirrors_expr()
    assert "d.getVar('BAKAR_DL_MIRROR_PATHS')" in _premirrors_expr()


def test_the_environment_alone_does_not_add_an_entry() -> None:
    assert _eval_premirrors({}, environ={"BAKAR_DL_MIRROR_PATHS": "/a"}) == ""


def test_entries_survive_the_task_environment() -> None:
    value = _eval_premirrors({"BAKAR_DL_MIRROR_PATHS": "/a"}, environ={})
    assert value.startswith(_PREFIX + "file:///a/ ")


def _generic_premirrors_value() -> str:
    for body in _load("bakar-tuning-generic.yml")["local_conf_header"].values():
        marker = 'PREMIRRORS:prepend = "'
        if marker in body:
            raw = body.split(marker, 1)[1].split('"', 1)[0]
            return raw.replace("\\\n", "")
    msg = "generic overlay has no PREMIRRORS:prepend"
    raise AssertionError(msg)


def test_combined_prepend_order_puts_directories_first() -> None:
    # BitBake applies local_conf_header blocks in sorted key order and each
    # :prepend lands in front of what came before.
    pieces = {
        _HUB_KEY: _eval(
            _expr_of("bakar-tuning-hub-cache.yml", _HUB_KEY),
            {"BAKAR_DL_MIRROR_URL": _HUB_URL},
        ),
        _KEY: _eval_premirrors({"BAKAR_DL_MIRROR_PATHS": "/a /b"}),
    }
    generic = _generic_premirrors_value()
    value = ""
    # The generic overlay's keys all sort before the hub keys.
    value = generic + value
    for key in sorted(pieces):
        value = pieces[key] + value
    pos_a = value.index("file:///a/")
    pos_b = value.index("file:///b/")
    pos_hub = value.index(_HUB_URL)
    pos_generic = value.index(generic.split()[0])
    assert pos_a < pos_b < pos_hub < pos_generic
