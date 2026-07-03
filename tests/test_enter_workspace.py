"""Tests for the ``-w``/``--workspace`` chdir helper in ``_helpers``."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from bakar.commands._helpers import _bsp_from_cwd, _enter_workspace


@pytest.mark.unit
def test_valid_dir_chdirs_and_returns_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path / "outside"
    ws = tmp_path / "ws"
    outside.mkdir()
    ws.mkdir()
    monkeypatch.chdir(outside)

    result = _enter_workspace(ws)

    assert result == ws.resolve()
    assert result.is_absolute()
    assert Path.cwd() == ws.resolve()


@pytest.mark.unit
def test_none_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    before = Path.cwd()

    assert _enter_workspace(None) is None
    assert Path.cwd() == before


@pytest.mark.unit
def test_missing_dir_raises_bad_parameter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(typer.BadParameter):
        _enter_workspace(tmp_path / "does-not-exist")


@pytest.mark.unit
def test_regular_file_raises_bad_parameter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    a_file = tmp_path / "file.txt"
    a_file.write_text("not a dir\n")

    with pytest.raises(typer.BadParameter):
        _enter_workspace(a_file)


@pytest.mark.unit
def test_relative_path_resolves_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(tmp_path)

    result = _enter_workspace(Path("./ws"))

    assert result == tmp_path.resolve() / "ws"
    assert Path.cwd() == tmp_path.resolve() / "ws"


@pytest.mark.unit
def test_bsp_from_cwd_uses_invoking_cwd_after_chdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With -w, family auto-detection must reflect where the user stood (``<ws>/ti``),
    not the post-chdir workspace root. Regression: the eager chdir made
    ``_bsp_from_cwd`` see cwd == workspace and lose the family."""
    ws = tmp_path / "ws"
    (ws / "ti").mkdir(parents=True)
    monkeypatch.chdir(ws / "ti")

    _enter_workspace(ws)

    assert Path.cwd() == ws.resolve()
    assert _bsp_from_cwd(ws) == "ti"


@pytest.mark.unit
def test_bsp_from_cwd_without_w_uses_live_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without -w, the callback fires with None and resets the capture, so
    ``_bsp_from_cwd`` reflects the live cwd exactly as before."""
    ws = tmp_path / "ws"
    (ws / "nxp").mkdir(parents=True)
    _enter_workspace(ws)  # a prior -w invocation captured a cwd
    _enter_workspace(None)  # a later -w-less command must reset it
    monkeypatch.chdir(ws / "nxp")

    assert _bsp_from_cwd(ws) == "nxp"
