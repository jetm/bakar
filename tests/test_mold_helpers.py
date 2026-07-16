"""Tests for the mold overlay selection and CLI-override helpers.

Covers ``_mold_extra_overlays`` / ``_tuning_extra_overlays`` mold gating, the
default-off byte-identity of the tuning stack, and ``apply_mold_overrides``
reflecting the ``_app`` module globals set by the ``--mold`` callback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar.config import BuildConfig


def _mold_cfg(*, mold: bool = False) -> BuildConfig:
    """Return a minimal BuildConfig for the mold overlay helper tests."""
    return BuildConfig(
        workspace=Path("/tmp"),
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        kas_container_image="img:latest",
        mold=mold,
    )


@pytest.mark.unit
def test_mold_extra_overlays_returns_path_when_enabled() -> None:
    """cfg.mold True yields the mold overlay path, and the file exists."""
    from bakar.commands._helpers import _mold_extra_overlays

    result = _mold_extra_overlays(_mold_cfg(mold=True))

    assert len(result) == 1
    assert result[0].name == "bakar-tuning-mold.yml"
    assert result[0].is_file(), "overlay file must exist in the installed overlays/ dir"


@pytest.mark.unit
def test_mold_extra_overlays_empty_when_disabled() -> None:
    """cfg.mold False yields an empty list (falsifier)."""
    from bakar.commands._helpers import _mold_extra_overlays

    assert _mold_extra_overlays(_mold_cfg(mold=False)) == []


@pytest.mark.unit
def test_mold_overlay_in_tuning_stack_when_enabled() -> None:
    """_tuning_extra_overlays includes the mold overlay when cfg.mold is on."""
    from bakar.commands._helpers import _tuning_extra_overlays

    names = [p.name for p in _tuning_extra_overlays(_mold_cfg(mold=True))]

    assert "bakar-tuning-mold.yml" in names


@pytest.mark.unit
def test_mold_overlay_absent_from_tuning_stack_when_disabled() -> None:
    """_tuning_extra_overlays omits the mold overlay when cfg.mold is off."""
    from bakar.commands._helpers import _tuning_extra_overlays

    names = [p.name for p in _tuning_extra_overlays(_mold_cfg(mold=False))]

    assert "bakar-tuning-mold.yml" not in names


@pytest.mark.unit
def test_tuning_stack_byte_identical_when_mold_off() -> None:
    """Default-off leaves the tuning stack unchanged from the pre-mold baseline.

    The baseline is reconstructed from the non-mold sub-helpers exactly as they
    were composed before mold was registered; with mold off the full stack must
    be byte-identical to it, proving the mold registration adds nothing when the
    toggle is off.
    """
    from bakar.commands import _helpers
    from bakar.commands._helpers import _overlay_dir, _tuning_extra_overlays

    cfg = _mold_cfg(mold=False)
    baseline = [
        _overlay_dir() / "bakar-tuning-cache-classify.yml",
        *_helpers._host_extra_overlays(cfg),
        *_helpers._ccache_extra_overlays(cfg),
        *_helpers._hashequiv_extra_overlays(cfg),
        *_helpers._shared_cache_extra_overlays(cfg),
        *_helpers._sccache_extra_overlays(cfg),
    ]

    assert _tuning_extra_overlays(cfg) == baseline


@pytest.mark.unit
def test_apply_mold_overrides_noop_when_globals_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither global set leaves cfg unchanged."""
    import bakar.commands._app as _state
    from bakar.commands._helpers import apply_mold_overrides

    monkeypatch.setattr(_state, "_MOLD", False)
    monkeypatch.setattr(_state, "_MOLD_BASELINE", False)
    monkeypatch.setattr(_state, "_MOLD_GLOBAL", False)

    cfg = _mold_cfg(mold=False)
    result = apply_mold_overrides(cfg)

    assert result.mold is False
    assert result.mold_mode == "list"


@pytest.mark.unit
def test_apply_mold_overrides_enables_list_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The --mold global enables mold in list mode."""
    import bakar.commands._app as _state
    from bakar.commands._helpers import apply_mold_overrides

    monkeypatch.setattr(_state, "_MOLD", True)
    monkeypatch.setattr(_state, "_MOLD_BASELINE", False)
    monkeypatch.setattr(_state, "_MOLD_GLOBAL", False)

    result = apply_mold_overrides(_mold_cfg(mold=False))

    assert result.mold is True
    assert result.mold_mode == "list"


@pytest.mark.unit
def test_apply_mold_overrides_baseline_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """The --mold-baseline global enables mold in baseline mode."""
    import bakar.commands._app as _state
    from bakar.commands._helpers import apply_mold_overrides

    monkeypatch.setattr(_state, "_MOLD", False)
    monkeypatch.setattr(_state, "_MOLD_BASELINE", True)
    monkeypatch.setattr(_state, "_MOLD_GLOBAL", False)

    result = apply_mold_overrides(_mold_cfg(mold=False))

    assert result.mold is True
    assert result.mold_mode == "baseline"


@pytest.mark.unit
def test_apply_mold_overrides_global_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The --mold-global flag enables mold in the deny-list (global) mode."""
    import bakar.commands._app as _state
    from bakar.commands._helpers import apply_mold_overrides

    monkeypatch.setattr(_state, "_MOLD", False)
    monkeypatch.setattr(_state, "_MOLD_BASELINE", False)
    monkeypatch.setattr(_state, "_MOLD_GLOBAL", True)

    result = apply_mold_overrides(_mold_cfg(mold=False))

    assert result.mold is True
    assert result.mold_mode == "global"


@pytest.mark.unit
def test_apply_mold_overrides_baseline_global_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """--mold-global with --mold-baseline selects the deny-list bfd baseline arm."""
    import bakar.commands._app as _state
    from bakar.commands._helpers import apply_mold_overrides

    monkeypatch.setattr(_state, "_MOLD", False)
    monkeypatch.setattr(_state, "_MOLD_BASELINE", True)
    monkeypatch.setattr(_state, "_MOLD_GLOBAL", True)

    result = apply_mold_overrides(_mold_cfg(mold=False))

    assert result.mold is True
    assert result.mold_mode == "baseline-global"
