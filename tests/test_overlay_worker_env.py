"""Overlay expressions that bitbake-worker expands must read the datastore, not os.environ.

bitbake-worker empties each task's os.environ and refills it from the exported
datastore variables only. A passthrough variable (one named in the overlay's
``env:`` block) lives in the datastore but is not exported, so ``os.environ`` holds
it in the cooker, where ``bakar getvar`` evaluates, and is empty in the task. The
hub PREMIRRORS entry was missing from every do_fetch for that reason, with the
unit tests green, because they stubbed os.environ with the URL.
"""

from __future__ import annotations

import importlib.resources
import re
import types
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_OVERLAY_DIR = Path(str(importlib.resources.files("bakar") / "overlays"))
# Variables the fetcher expands inside a worker task: do_fetch reads PREMIRRORS and
# MIRRORS, and the do_*_setscene task reads SSTATE_MIRRORS.
_WORKER_EXPANDED = ("PREMIRRORS", "MIRRORS", "SSTATE_MIRRORS")
_ASSIGN = re.compile(r"^\s*(?P<var>[A-Z_]+)(?::[a-z_]+)?\s*(?:\?\?=|\?=|:=|\+=|=\+|\.=|=\.|=)\s*\"")
_INLINE = re.compile(r"\$\{@(.*?)\}")


class _Datastore:
    def __init__(self, variables: dict[str, str]) -> None:
        self._variables = variables

    def getVar(self, name: str) -> str | None:  # noqa: N802 - BitBake's spelling
        return self._variables.get(name)


def _assignment_lines(path: Path, var: str) -> list[str]:
    data = yaml.safe_load(path.read_text())
    lines: list[str] = []
    for block in (data.get("local_conf_header") or {}).values():
        for line in str(block).splitlines():
            m = _ASSIGN.match(line)
            if m and m.group("var") == var:
                lines.append(line.strip())
    return lines


def test_no_overlay_reads_os_environ_for_a_worker_expanded_variable() -> None:
    offenders = [
        f"{path.name}: {line}"
        for path in sorted(_OVERLAY_DIR.glob("*.yml"))
        for var in _WORKER_EXPANDED
        for line in _assignment_lines(path, var)
        if "os.environ" in line
    ]
    assert offenders == []


def test_the_guard_finds_the_assignments_it_is_meant_to_check() -> None:
    # A guard over an empty set passes for the wrong reason.
    assert _assignment_lines(_OVERLAY_DIR / "bakar-tuning-hub-cache.yml", "PREMIRRORS")
    assert _assignment_lines(_OVERLAY_DIR / "bakar-tuning-shared-cache.yml", "SSTATE_MIRRORS")


def _sstate_mirrors(variables: dict[str, str], environ: dict[str, str] | None = None) -> str:
    (line,) = _assignment_lines(_OVERLAY_DIR / "bakar-tuning-shared-cache.yml", "SSTATE_MIRRORS")
    value = line.split("=", 1)[1].strip().strip('"')
    stub_os = types.SimpleNamespace(environ=environ or {})

    def run(match: re.Match[str]) -> str:
        # nosemgrep: python.lang.security.audit.eval-detected.eval-detected
        return str(eval(match.group(1), {"os": stub_os, "d": _Datastore(variables)}))

    return _INLINE.sub(run, value)


def test_sstate_mirror_url_survives_the_task_environment() -> None:
    # os.environ is empty here, as it is in a do_*_setscene task.
    got = _sstate_mirrors({"BAKAR_SSTATE_MIRROR_URL": "http://mirror.test"}, environ={})
    assert got == "file://.* http://mirror.test/all/PATH;downloadfilename=PATH"


def test_sstate_mirror_url_is_a_datastore_decision() -> None:
    got = _sstate_mirrors({}, environ={"BAKAR_SSTATE_MIRROR_URL": "http://mirror.test"})
    assert "mirror.test" not in got
