"""Tests for the container-to-host recipe-log path translation in triage.

``_translate_container_path`` is the shared helper extracted from
``_find_recipe_log``; both are exercised against synthetic ``tmp_path``
fixtures so the tests never touch the real host filesystem. The kas.log
sample mirrors the ``Logfile of failure stored in: /work/...`` shape that
``_RECIPE_LOG_RE`` matches (see ``tests/conftest.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar.triage import _find_recipe_log, _translate_container_path


@pytest.mark.unit
def test_translate_container_path_rewrites_work_prefix(tmp_path: Path) -> None:
    """A ``/work/``-prefixed path maps to the workspace-rooted host path."""
    workspace = tmp_path / "nxp"
    container_path = "/work/build/tmp/deploy/linux-imx/temp/log.do_compile"

    result = _translate_container_path(container_path, workspace)

    assert result == f"{workspace}/build/tmp/deploy/linux-imx/temp/log.do_compile"
    assert result.startswith(str(workspace) + "/")
    # Only the leading prefix is rewritten, so the result no longer begins
    # with the container "/work/" mount root.
    assert not result.startswith("/work/")


@pytest.mark.unit
def test_translate_container_path_passes_non_work_unchanged(tmp_path: Path) -> None:
    """A path not under ``/work/`` is already a host path and passes through."""
    workspace = tmp_path / "nxp"
    host_path = str(tmp_path / "some" / "host" / "log.do_compile")

    assert _translate_container_path(host_path, workspace) == host_path
    # A leading "/workspace" must not be mistaken for the "/work/" prefix.
    assert _translate_container_path("/workspace/foo", workspace) == "/workspace/foo"


@pytest.mark.unit
def test_find_recipe_log_returns_translated_host_path(tmp_path: Path) -> None:
    """``_find_recipe_log`` resolves the Logfile hint to the host path.

    The kas.log names an in-container ``/work/...`` path; the target file is
    created at the translated host location so the ``host_path.is_file()``
    gate passes and the host path is returned.
    """
    workspace = tmp_path / "nxp"
    container_path = "/work/build/tmp/deploy/linux-imx/temp/log.do_compile"
    expected = Path(_translate_container_path(container_path, workspace))
    expected.parent.mkdir(parents=True)
    expected.write_text("do_compile failed\n")

    kas_log = tmp_path / "kas.log"
    kas_log.write_text(f"NOTE: Executing Tasks\nERROR: Logfile of failure stored in: {container_path}\n")

    result = _find_recipe_log(kas_log, workspace)

    assert result == expected
    assert result is not None
    assert str(result).startswith(str(workspace) + "/")
    assert not str(result).startswith("/work/")
