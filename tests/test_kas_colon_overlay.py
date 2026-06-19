"""Tests for kas colon-overlay (machine.yml:overlay.yml) syntax.

Covers three layers:
1. split_kas_yaml_arg - pure unit tests for the helper.
2. _build_kas_arg with extra_overlays - verifies the BYO colon string is assembled
   correctly.
3. bakar bitbake with a colon arg - CliRunner wiring test confirming the extra
   lands in KasBuildContext.extra_overlays.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def nxp_workspace(tmp_path: Path) -> Path:
    (tmp_path / "nxp").mkdir()
    return tmp_path


@pytest.fixture
def machine_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "machine.yml"
    p.write_text("header:\n  version: 14\nmachine: imx8mp-var-dart\n", encoding="utf-8")
    return p


@pytest.fixture
def overlay_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "bringup.yml"
    p.write_text("header:\n  version: 14\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# split_kas_yaml_arg
# ---------------------------------------------------------------------------


def test_split_kas_yaml_arg_none_returns_none_empty() -> None:
    """split_kas_yaml_arg(None) returns (None, []) with no validation."""
    from bakar.commands._helpers import split_kas_yaml_arg

    head, extras = split_kas_yaml_arg(None)
    assert head is None
    assert extras == []


def test_split_kas_yaml_arg_single_file(machine_yaml: Path) -> None:
    """A bare single YAML path returns (resolved_path, []) with no colon."""
    from bakar.commands._helpers import split_kas_yaml_arg

    head, extras = split_kas_yaml_arg(str(machine_yaml))
    assert head is not None
    assert head.resolve() == machine_yaml.resolve()
    assert extras == []


def test_split_kas_yaml_arg_colon_two_files(machine_yaml: Path, overlay_yaml: Path) -> None:
    """Two colon-joined files: head is first, extra is second, order preserved."""
    from bakar.commands._helpers import split_kas_yaml_arg

    raw = f"{machine_yaml}:{overlay_yaml}"
    head, extras = split_kas_yaml_arg(raw)
    assert head is not None
    assert head.resolve() == machine_yaml.resolve()
    assert len(extras) == 1
    assert extras[0].resolve() == overlay_yaml.resolve()


def test_split_kas_yaml_arg_three_files_order_preserved(tmp_path: Path) -> None:
    """Three colon-joined files: order is preserved (FIRST drives dispatch)."""
    from bakar.commands._helpers import split_kas_yaml_arg

    a = tmp_path / "a.yml"
    b = tmp_path / "b.yml"
    c = tmp_path / "c.yml"
    for f in (a, b, c):
        f.write_text("header:\n  version: 14\n", encoding="utf-8")

    head, extras = split_kas_yaml_arg(f"{a}:{b}:{c}")
    assert head is not None
    assert head.resolve() == a.resolve()
    assert len(extras) == 2
    assert extras[0].resolve() == b.resolve()
    assert extras[1].resolve() == c.resolve()


def test_split_kas_yaml_arg_bad_head_exits(tmp_path: Path) -> None:
    """A missing head segment raises typer.Exit(2) and names the offending file."""
    import typer

    from bakar.commands._helpers import split_kas_yaml_arg

    overlay = tmp_path / "overlay.yml"
    overlay.write_text("header:\n  version: 14\n", encoding="utf-8")
    nonexistent = str(tmp_path / "missing.yml")

    with pytest.raises(typer.Exit) as exc_info:
        split_kas_yaml_arg(nonexistent)
    assert exc_info.value.exit_code == 2


def test_split_kas_yaml_arg_bad_extra_exits_naming_file(machine_yaml: Path, tmp_path: Path) -> None:
    """A missing extra segment raises typer.Exit(2) (any typo in the overlay chain is caught)."""
    import typer

    from bakar.commands._helpers import split_kas_yaml_arg

    nonexistent_overlay = str(tmp_path / "does-not-exist.yml")
    raw = f"{machine_yaml}:{nonexistent_overlay}"

    with pytest.raises(typer.Exit) as exc_info:
        split_kas_yaml_arg(raw)
    assert exc_info.value.exit_code == 2


def test_split_kas_yaml_arg_path_input(machine_yaml: Path) -> None:
    """Accepts a Path object in addition to a str (for callers that have a Path)."""
    from bakar.commands._helpers import split_kas_yaml_arg

    head, extras = split_kas_yaml_arg(machine_yaml)
    assert head is not None
    assert head.resolve() == machine_yaml.resolve()
    assert extras == []


# ---------------------------------------------------------------------------
# _build_kas_arg BYO path: extra_overlays appended after tuning overlay
# ---------------------------------------------------------------------------


def test_build_kas_arg_byo_extra_overlays_appended(tmp_path: Path) -> None:
    """_build_kas_arg appends extra_overlays after the main overlay in the BYO path."""
    from bakar.config import BuildConfig
    from bakar.steps.kas_build import _build_kas_arg

    cfg = BuildConfig(
        workspace=tmp_path,
        bsp_family="generic",
        machine="qemux86-64",
        distro="poky",
        image="core-image-minimal",
        manifest="",
        repo_url="",
        repo_branch="",
        container_image="kas-build-env:latest",
    )
    bsp_root = cfg.bsp_root
    bsp_root.mkdir(parents=True, exist_ok=True)

    kas_yaml = bsp_root / "machine.yml"
    kas_yaml.write_text("header:\n  version: 14\n", encoding="utf-8")
    overlay_src = bsp_root / "bakar-tuning-generic.yml"
    overlay_src.write_text("header:\n  version: 14\n", encoding="utf-8")
    extra_src = bsp_root / "bringup.yml"
    extra_src.write_text("header:\n  version: 14\n", encoding="utf-8")

    kas_arg = _build_kas_arg(cfg, kas_yaml, overlay_src, [extra_src])

    parts = kas_arg.split(":")
    assert len(parts) == 3, f"expected 3 colon-parts, got {parts}"
    # First part is the user's machine YAML (relative to bsp_root)
    assert "machine.yml" in parts[0]
    # Second part is the materialized tuning overlay
    assert "bakar-tuning-generic.yml" in parts[1]
    # Third part is the materialized extra overlay
    assert "bringup.yml" in parts[2]


def test_build_kas_arg_byo_no_extras_unchanged(tmp_path: Path) -> None:
    """_build_kas_arg with no extra_overlays returns the original two-part colon arg."""
    from bakar.config import BuildConfig
    from bakar.steps.kas_build import _build_kas_arg

    cfg = BuildConfig(
        workspace=tmp_path,
        bsp_family="generic",
        machine="qemux86-64",
        distro="poky",
        image="core-image-minimal",
        manifest="",
        repo_url="",
        repo_branch="",
        container_image="kas-build-env:latest",
    )
    bsp_root = cfg.bsp_root
    bsp_root.mkdir(parents=True, exist_ok=True)

    kas_yaml = bsp_root / "machine.yml"
    kas_yaml.write_text("header:\n  version: 14\n", encoding="utf-8")
    overlay_src = bsp_root / "bakar-tuning-generic.yml"
    overlay_src.write_text("header:\n  version: 14\n", encoding="utf-8")

    kas_arg = _build_kas_arg(cfg, kas_yaml, overlay_src)
    parts = kas_arg.split(":")
    assert len(parts) == 2, f"no-extras arg must be two-part, got {parts}"


def test_build_kas_arg_meta_avocado_threads_extras_to_kas_dump(tmp_path: Path) -> None:
    """The meta-avocado branch must pass extra_overlays to _run_kas_dump.

    Regression: _build_kas_arg fixed the BYO path but left the meta-avocado
    branch calling _run_kas_dump without the extras, so colon overlays were
    silently dropped for every meta-avocado shell-function path (run_shell_live
    powers bakar bitbake / dump).
    """
    from bakar.config import BuildConfig
    from bakar.steps.kas_build import _build_kas_arg

    meta = tmp_path / "sources" / "meta-avocado"
    kas_dir = meta / "kas" / "machine"
    kas_dir.mkdir(parents=True, exist_ok=True)
    kas_yaml = kas_dir / "qemux86-64.yml"
    kas_yaml.write_text("header:\n  version: 16\n", encoding="utf-8")
    cfg = BuildConfig(
        workspace=tmp_path,
        bsp_family="generic",  # type: ignore[arg-type]
        machine="generic",
        distro="generic",
        image="generic",
        manifest="",
        repo_url="",
        repo_branch="",
        container_image="jetm/kas-build-env:latest",
        kas_yaml_override=kas_yaml,
    )
    cfg.bsp_root.mkdir(parents=True, exist_ok=True)
    assert cfg.is_meta_avocado
    overlay_src = meta / "bakar-tuning-generic.yml"
    overlay_src.write_text("header:\n  version: 16\n", encoding="utf-8")
    extra_src = meta / "kas" / "target" / "bringup.yml"
    extra_src.parent.mkdir(parents=True, exist_ok=True)
    extra_src.write_text("header:\n  version: 16\n", encoding="utf-8")

    captured: dict = {}

    def fake_run_kas_dump(cfg, wrapper, overlay_rel, extra_overlay_rels=None):
        captured["extra_overlay_rels"] = list(extra_overlay_rels or [])
        dump = cfg.bsp_root / "avocado-bakar.yml"
        dump.write_text("header:\n  version: 16\n", encoding="utf-8")
        return dump

    with patch("bakar.steps.kas_build._run_kas_dump", fake_run_kas_dump):
        _build_kas_arg(cfg, kas_yaml, overlay_src, [extra_src])

    assert len(captured["extra_overlay_rels"]) == 1, f"meta-avocado branch dropped extras: {captured}"
    assert "bringup.yml" in captured["extra_overlay_rels"][0].name


def test_friendly_overlay_lines_is_markup_safe_and_shortened(tmp_path: Path) -> None:
    """``friendly_overlay_lines`` renders a markup-safe, shortened vertical list.

    Two regressions guarded here:

    1. Markup: the RunLogger console handler renders with ``markup=True``, so a
       message wrapping absolute paths in ``[...]`` made Rich parse ``[/home/...]``
       as a closing tag and raised MarkupError, crashing the build right after
       ``kas dump``. The rendered list must contain no ``[`` and must round-trip
       through ``Text.from_markup`` (the exact call that raised).
    2. Readability: each overlay sits on its own line, shortened workspace-relative
       (in-workspace files) or to a basename (bakar's bundled overlays), so a long
       merge chain no longer soft-wraps mid-path.
    """
    from pathlib import Path as _Path

    from rich.text import Text

    from bakar.steps.kas_build import friendly_overlay_lines

    workspace = tmp_path
    machine = workspace / "meta-avocado" / "kas" / "machine" / "rzv2h-rdk.yml"
    user = workspace / "meta-avocado" / "kas" / "target" / "bringup.yml"
    # A bakar bundled overlay lives OUTSIDE the workspace -> rendered as basename.
    bakar_overlay = _Path("/opt/bakar/overlays/bakar-tuning-generic.yml")

    msg = friendly_overlay_lines([machine, bakar_overlay, user], workspace)

    assert "[" not in msg, f"data-bearing log line must not contain '[': {msg!r}"
    Text.from_markup(msg)  # must not raise rich.errors.MarkupError

    # One overlay per line, shortened, order preserved.
    lines = msg.splitlines()
    assert lines == [
        "    - meta-avocado/kas/machine/rzv2h-rdk.yml",
        "    - bakar-tuning-generic.yml",  # outside workspace -> basename
        "    - meta-avocado/kas/target/bringup.yml",
    ]
    # No absolute bakar path leaks into the rendered list.
    assert "/opt/bakar" not in msg


# ---------------------------------------------------------------------------
# bakar bitbake with colon arg: CliRunner wiring
# ---------------------------------------------------------------------------


def _make_fake_live_ctx(calls: list[dict]) -> object:
    """Return a fake run_shell_live that captures the ctx.extra_overlays."""

    def fake_live(ctx, command):
        calls.append({"extra_overlays": list(ctx.extra_overlays)})
        return 0

    return fake_live


@pytest.mark.unit
def test_bitbake_colon_arg_extra_overlay_in_ctx(
    runner: _CliRunner,
    nxp_workspace: Path,
    machine_yaml: Path,
    overlay_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bakar bitbake with 'machine.yml:overlay.yml' parses the overlay into ctx.extra_overlays."""
    import bakar.commands.bitbake  # noqa: F401 - register commands
    from bakar.cli import app
    from bakar.user_config import UserConfig

    # Pin a clean config so the ambient (sccache/hashequiv-enabled) user config
    # does not add tuning overlays the bitbake passthrough would otherwise pull.
    monkeypatch.setattr("bakar.commands._app._load_user_config_safe", UserConfig)

    calls: list[dict] = []
    fake = _make_fake_live_ctx(calls)
    kas_arg = f"{machine_yaml}:{overlay_yaml}"

    with patch("bakar.commands.bitbake.run_shell_live", fake):
        result = runner.invoke(
            app,
            ["bitbake", "busybox", kas_arg, "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    extras = calls[0]["extra_overlays"]
    assert len(extras) == 1
    assert extras[0].resolve() == overlay_yaml.resolve()


@pytest.mark.unit
def test_bitbake_single_yaml_no_extras(
    runner: _CliRunner,
    nxp_workspace: Path,
    machine_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bakar bitbake with a bare single YAML has empty extra_overlays (no behavior change)."""
    import bakar.commands.bitbake  # noqa: F401
    from bakar.cli import app
    from bakar.user_config import UserConfig

    monkeypatch.setattr("bakar.commands._app._load_user_config_safe", UserConfig)

    calls: list[dict] = []
    fake = _make_fake_live_ctx(calls)

    with patch("bakar.commands.bitbake.run_shell_live", fake):
        result = runner.invoke(
            app,
            ["bitbake", "busybox", str(machine_yaml), "--workspace", str(nxp_workspace)],
        )

    assert result.exit_code == 0, result.output
    assert calls[0]["extra_overlays"] == []


@pytest.mark.unit
def test_bitbake_missing_overlay_exits(
    runner: _CliRunner,
    nxp_workspace: Path,
    machine_yaml: Path,
    tmp_path: Path,
) -> None:
    """A colon arg with a missing overlay exits non-zero (not a silent ignore)."""
    import bakar.commands.bitbake  # noqa: F401
    from bakar.cli import app

    missing = str(tmp_path / "missing-overlay.yml")
    kas_arg = f"{machine_yaml}:{missing}"

    result = runner.invoke(
        app,
        ["bitbake", "busybox", kas_arg, "--workspace", str(nxp_workspace)],
    )

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# run_build BYO path: extra_overlays appear in the kas-container command
# ---------------------------------------------------------------------------


def test_run_build_byo_extra_overlays_in_dry_run(tmp_path: Path) -> None:
    """run_build dry-run includes extra_overlays in the kas_arg after the tuning overlay."""
    from bakar.config import BuildConfig
    from bakar.steps import kas_build

    cfg = BuildConfig(
        workspace=tmp_path,
        bsp_family="generic",
        machine="qemux86-64",
        distro="poky",
        image="core-image-minimal",
        manifest="",
        repo_url="",
        repo_branch="",
        container_image="kas-build-env:latest",
    )
    bsp_root = cfg.bsp_root
    bsp_root.mkdir(parents=True, exist_ok=True)

    kas_yaml = bsp_root / "machine.yml"
    kas_yaml.write_text("header:\n  version: 14\n", encoding="utf-8")
    overlay = tmp_path / "bakar-tuning-generic.yml"
    overlay.write_text("header:\n  version: 14\n", encoding="utf-8")
    extra = tmp_path / "bringup.yml"
    extra.write_text("header:\n  version: 14\n", encoding="utf-8")

    lines = kas_build.dry_run_preview_lines(cfg, kas_yaml, overlay, [extra])

    command_line = next(ln for ln in lines if ln.startswith("command:"))
    assert "bringup.yml" in command_line, command_line
