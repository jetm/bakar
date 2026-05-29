"""Extended tests for ``bakar clean`` and its extracted helper.

Covers ``commands.clean._resolve_family`` directly across all four branches
of its flag ladder (explicit ``--bsp``, ``--manifest`` alias, cwd
auto-detect, and the not-resolvable path), plus a CliRunner test of the
``clean --all`` invocation with ``_clean_build_dir`` and ``hashserv.stop``
mocked so the test stays hermetic.

The cwd auto-detect cases lean on ``monkeypatch.chdir`` into a synthetic
``workspace/nxp`` / ``workspace/ti`` subdirectory so the helper resolves
``Path.cwd().relative_to(workspace)`` to the right BSP family.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import typer

from bakar.cli import app
from bakar.commands.clean import _resolve_family

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _resolve_family direct tests
# ---------------------------------------------------------------------------


def test_resolve_family_explicit_bsp_nxp_returns_nxp(tmp_path: Path) -> None:
    """Explicit ``--bsp nxp`` short-circuits and returns ``"nxp"``.

    Neither the manifest dispatcher nor the cwd walk is consulted, so the
    workspace can be a bare tmp_path with no markers.
    """
    assert _resolve_family(bsp="nxp", manifest=None, ws=tmp_path) == "nxp"


def test_resolve_family_explicit_bsp_ti_returns_ti(tmp_path: Path) -> None:
    """Explicit ``--bsp ti`` short-circuits and returns ``"ti"``."""
    assert _resolve_family(bsp="ti", manifest=None, ws=tmp_path) == "ti"


def test_resolve_family_invalid_bsp_value_raises_typer_exit(tmp_path: Path) -> None:
    """``--bsp garbage`` is rejected before any fall-through with ``exit(2)``."""
    with pytest.raises(typer.Exit) as exc:
        _resolve_family(bsp="garbage", manifest=None, ws=tmp_path)

    assert exc.value.exit_code == 2


def test_resolve_family_manifest_alias_routes_through_dispatch_bsp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--manifest <imx-...xml>`` routes through ``_dispatch_bsp`` and resolves NXP.

    Asserts that ``_dispatch_bsp`` is the resolution path (not the cwd walk)
    by patching it to return a sentinel family and confirming the value is
    forwarded. The workspace passed in is unused on this branch, so a bare
    tmp_path suffices.
    """
    seen: list[str] = []

    def fake_dispatch(manifest_arg: str | None) -> tuple[str, object]:
        seen.append(manifest_arg or "")
        return ("ti", object())

    monkeypatch.setattr("bakar.commands.clean._dispatch_bsp", fake_dispatch)

    family = _resolve_family(bsp=None, manifest="processor-sdk-foo.txt", ws=tmp_path)

    assert family == "ti"
    assert seen == ["processor-sdk-foo.txt"], f"_dispatch_bsp must be called with the manifest value, got {seen!r}"


def test_resolve_family_manifest_alias_real_dispatch_nxp(tmp_path: Path) -> None:
    """End-to-end NXP manifest filename flows through the real ``_dispatch_bsp``.

    Confirms the production wiring resolves a known-good NXP manifest name
    to ``"nxp"`` without monkeypatching the dispatcher.
    """
    assert _resolve_family(bsp=None, manifest="imx-6.6.52-2.2.2.xml", ws=tmp_path) == "nxp"


def test_resolve_family_cwd_autodetect_nxp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No flags: cwd inside ``workspace/nxp/`` is auto-detected as NXP."""
    (tmp_path / "nxp").mkdir()
    monkeypatch.chdir(tmp_path / "nxp")

    assert _resolve_family(bsp=None, manifest=None, ws=tmp_path) == "nxp"


def test_resolve_family_cwd_autodetect_ti(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No flags: cwd inside ``workspace/ti/`` is auto-detected as TI."""
    (tmp_path / "ti").mkdir()
    monkeypatch.chdir(tmp_path / "ti")

    assert _resolve_family(bsp=None, manifest=None, ws=tmp_path) == "ti"


def test_resolve_family_unresolvable_raises_typer_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No flags + cwd at workspace root (no nxp/ti subdir match) exits(2)."""
    monkeypatch.chdir(tmp_path)

    with pytest.raises(typer.Exit) as exc:
        _resolve_family(bsp=None, manifest=None, ws=tmp_path)

    assert exc.value.exit_code == 2


# ---------------------------------------------------------------------------
# CliRunner test of ``clean --all`` with hashserv and rmtree mocked
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp workspace with a ``.bakar.toml`` marker and an ``nxp/`` subtree.

    ``_workspace_from_cwd`` walks up looking for a marker; ``nxp/build``
    materialized here makes ``_clean_build_dir`` enter its rmtree branch
    when the helper is not mocked. ``kas-nxp.yml`` is created so the
    ``--all`` branch hits its ``cfg.kas_yaml.unlink()`` path.
    """
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp" / "build").mkdir(parents=True)
    (tmp_path / "nxp" / "kas-nxp.yml").write_text("# generated kas\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_clean_all_branch_invokes_hashserv_stop_and_clean_build_dir(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``clean --all --bsp nxp`` calls ``hashserv.stop`` then ``_clean_build_dir``.

    Mocks both side-effecting helpers so the test stays hermetic. The
    ordering check (``stop`` recorded before ``clean``) protects against a
    regression that wipes the build dir while the daemon's working
    directory is still live.
    """
    import bakar.commands.clean as clean_mod
    import bakar.hashserv as hashserv_mod

    recorded: list[str] = []

    monkeypatch.setattr(
        hashserv_mod,
        "stop",
        lambda root: recorded.append(f"stop:{root}") or True,
    )
    monkeypatch.setattr(
        clean_mod,
        "_clean_build_dir",
        lambda cfg: recorded.append(f"clean:{cfg.bsp_root}"),
    )

    result = runner.invoke(app, ["clean", "--all", "--bsp", "nxp"])

    assert result.exit_code == 0, result.output
    # Both helpers must have been invoked; stop must precede clean.
    assert any(entry.startswith("stop:") for entry in recorded), f"hashserv.stop must run on --all, got {recorded!r}"
    assert any(entry.startswith("clean:") for entry in recorded), f"_clean_build_dir must run, got {recorded!r}"
    stop_idx = next(i for i, e in enumerate(recorded) if e.startswith("stop:"))
    clean_idx = next(i for i, e in enumerate(recorded) if e.startswith("clean:"))
    assert stop_idx < clean_idx, f"hashserv.stop must precede _clean_build_dir, got {recorded!r}"
    # --all also unlinks the generated kas YAML.
    assert not (workspace / "nxp" / "kas-nxp.yml").exists(), "--all must remove the generated kas YAML"


def test_clean_without_all_skips_hashserv_stop(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--all``, ``hashserv.stop`` is NOT called and the kas YAML stays.

    Pins the contract that ``hashserv.stop`` is a side-effect specific to
    the ``--all`` branch - regressions that always stop the daemon would
    leave running builds without their hashserv backend.
    """
    import bakar.commands.clean as clean_mod
    import bakar.hashserv as hashserv_mod

    stop_called: list[str] = []

    monkeypatch.setattr(
        hashserv_mod,
        "stop",
        lambda root: stop_called.append(str(root)) or True,
    )
    monkeypatch.setattr(clean_mod, "_clean_build_dir", lambda cfg: None)

    result = runner.invoke(app, ["clean", "--bsp", "nxp"])

    assert result.exit_code == 0, result.output
    assert stop_called == [], f"hashserv.stop must NOT run without --all, got {stop_called!r}"
    # The kas YAML survives a no-``--all`` clean.
    assert (workspace / "nxp" / "kas-nxp.yml").exists(), "kas YAML must be preserved when --all is absent"
