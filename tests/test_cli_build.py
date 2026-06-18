"""Tests for the ``bakar build`` auto-overlay behavior and --dry-run-script.

Cover ``_hashequiv_extra_overlays`` as a unit and the build CLI's
deduplication of the hashequiv overlay against a user-supplied
``main.yml:overlay.yml`` argument. The dedup case is load-bearing:
without it, kas would receive the overlay twice and emit duplicate
``BB_SIGNATURE_HANDLER`` assignments.

Also covers the ``--dry-run-script`` option: writing to a file and to stdout
(``-``), and asserting that ``--dry-run`` alone does NOT produce a script file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import bakar.commands._app as _state
import bakar.steps.kas_build as step_kas
from bakar.cli import app
from bakar.commands import build as build_cmd
from bakar.commands._helpers import _hashequiv_extra_overlays, _overlay_dir
from bakar.config import BuildConfig
from bakar.observability import RunLogger
from bakar.steps.kas_build import KasBuildContext, _PtyOutcome
from bakar.user_config import UserConfig

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner as _CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _stub_doctor_checks():
    """Doctor always runs now; stub ``run_all`` to an empty (all-pass) list so these
    tests stay host-independent - real checks BLOCK on disk-free / git config."""
    from unittest.mock import patch

    with patch("bakar.commands._helpers.run_all", return_value=[]):
        yield


def _make_cfg(workspace: Path, *, use_hashequiv: bool = False) -> BuildConfig:
    return BuildConfig(
        workspace=workspace,
        bsp_family="generic",
        machine="generic",
        distro="generic",
        image="generic",
        manifest="",
        repo_url="https://example.invalid/repo.git",
        repo_branch="",
        container_image="jetm/kas-build-env:latest",
        use_hashequiv=use_hashequiv,
    )


# ---------------------------------------------------------------------------
# Helper-only tests (no CLI)
# ---------------------------------------------------------------------------


def test_hashequiv_overlay_auto_appended_when_use_hashequiv_true(tmp_path: Path) -> None:
    """Helper returns the hashequiv overlay path when use_hashequiv=True.

    Pins the filename exactly so a rename of the shipped overlay (or a
    bug that returns the generic overlay) fails this test loudly.
    """
    cfg = _make_cfg(tmp_path, use_hashequiv=True)

    overlays = _hashequiv_extra_overlays(cfg)

    assert len(overlays) == 1
    assert overlays[0].name == "bakar-tuning-hashequiv.yml"


def test_hashequiv_overlay_empty_when_use_hashequiv_false(tmp_path: Path) -> None:
    """Helper returns an empty list when use_hashequiv=False (default opt-out)."""
    cfg = _make_cfg(tmp_path, use_hashequiv=False)

    assert _hashequiv_extra_overlays(cfg) == []


# ---------------------------------------------------------------------------
# CLI dedup tests
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> _CliRunner:
    from typer.testing import CliRunner

    return CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp workspace with a ``.bakar.toml`` marker; chdir into it."""
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def generic_yaml(tmp_path: Path) -> Path:
    """Write a minimal generic kas YAML (qemu machine, no NXP/TI markers)."""
    yaml_path = tmp_path / "my.yml"
    yaml_path.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    return yaml_path


def _stub_user_config_loader(monkeypatch: pytest.MonkeyPatch, *, hashserv: bool) -> None:
    """Force the Typer callback to return a UserConfig with the chosen hashserv flag.

    The build callback writes ``_state._USER_CONFIG = _load_user_config_safe()``
    on every invocation, so monkeypatching the loader is the only stable way
    to plant a fixed value before the CLI reaches the build subcommand.
    """
    monkeypatch.setattr(_state, "_load_user_config_safe", lambda: UserConfig(hashserv=hashserv))


def test_hashequiv_overlay_deduped_when_user_passes_it(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-passed hashequiv overlay must not be duplicated when hashserv=true.

    Asserts the recorded ``extra_overlays`` contains the hashequiv overlay
    EXACTLY ONCE - a >=1 assertion would let the dedup-regression bug pass.
    """
    _stub_user_config_loader(monkeypatch, hashserv=True)

    recorded: list[list] = []

    def fake_run_build(ctx, *, extra_overlays=None, show_layers=False):  # type: ignore[no-untyped-def]
        recorded.append(list(extra_overlays or []))
        return 0

    monkeypatch.setattr(build_cmd.step_kas, "run_build", fake_run_build)

    overlay_path = _overlay_dir() / "bakar-tuning-hashequiv.yml"
    arg = f"{generic_yaml}:{overlay_path}"

    result = runner.invoke(app, ["build", arg])

    assert result.exit_code == 0, result.output
    assert len(recorded) == 1, f"expected exactly one run_build call, got {recorded!r}"

    hashequiv_entries = [p for p in recorded[0] if p.name == "bakar-tuning-hashequiv.yml"]
    assert len(hashequiv_entries) == 1, f"expected hashequiv overlay to appear EXACTLY once, got {hashequiv_entries!r}"


def test_hashequiv_overlay_not_appended_when_use_hashequiv_false(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When hashserv=false, the auto-append branch is skipped entirely.

    With no user-supplied overlay suffix the recorded ``extra_overlays``
    list must be empty - confirms the helper is the sole source of the
    hashequiv overlay path.
    """
    _stub_user_config_loader(monkeypatch, hashserv=False)

    recorded: list[list] = []

    def fake_run_build(ctx, *, extra_overlays=None, show_layers=False):  # type: ignore[no-untyped-def]
        recorded.append(list(extra_overlays or []))
        return 0

    monkeypatch.setattr(build_cmd.step_kas, "run_build", fake_run_build)

    result = runner.invoke(app, ["build", str(generic_yaml)])

    assert result.exit_code == 0, result.output
    assert len(recorded) == 1, f"expected exactly one run_build call, got {recorded!r}"

    hashequiv_entries = [p for p in recorded[0] if p.name == "bakar-tuning-hashequiv.yml"]
    assert hashequiv_entries == [], f"expected no hashequiv overlay when use_hashequiv=False, got {hashequiv_entries!r}"


def test_user_overlay_named_in_build_log(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The build-start line lists the full overlay stack, including the user overlay.

    The line is the single authoritative overlay list (machine yaml + tuning
    base + user/auto overlays), so the operator can confirm their extra overlay
    is in the merge. Asserts on the structured events.jsonl message (the Rich
    console soft-wraps long paths, which would make a console substring match
    fragile). With hashserv=False the stack is exactly 3: my.yml, the tuning
    base, and bringup.yml.
    """
    _stub_user_config_loader(monkeypatch, hashserv=False)
    monkeypatch.setattr(build_cmd.step_kas, "run_build", lambda ctx, **kw: 0)

    overlay = tmp_path / "bringup.yml"
    overlay.write_text("header:\n  version: 14\n")

    result = runner.invoke(app, ["build", f"{generic_yaml}:{overlay}"])

    assert result.exit_code == 0, result.output

    events = list(tmp_path.glob("**/events.jsonl"))
    assert events, "no events.jsonl written"
    text = "\n".join(p.read_text() for p in events)
    assert "merging 3 overlays" in text
    assert "bringup.yml" in text


# ---------------------------------------------------------------------------
# --target override threading
# ---------------------------------------------------------------------------


def test_target_option_threads_into_build_context(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--target` reaches the KasBuildContext handed to run_build."""
    recorded: list[str | None] = []

    def fake_run_build(ctx, *, extra_overlays=None, show_layers=False):  # type: ignore[no-untyped-def]
        recorded.append(ctx.target)
        return 0

    monkeypatch.setattr(build_cmd.step_kas, "run_build", fake_run_build)

    result = runner.invoke(app, ["build", str(generic_yaml), "--target", "avocado-complete"])

    assert result.exit_code == 0, result.output
    assert recorded == ["avocado-complete"]


def test_target_absent_defaults_to_none_in_context(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --target, KasBuildContext.target is None - build the YAML's own target."""
    recorded: list[str | None] = []

    def fake_run_build(ctx, *, extra_overlays=None, show_layers=False):  # type: ignore[no-untyped-def]
        recorded.append(ctx.target)
        return 0

    monkeypatch.setattr(build_cmd.step_kas, "run_build", fake_run_build)

    result = runner.invoke(app, ["build", str(generic_yaml)])

    assert result.exit_code == 0, result.output
    assert recorded == [None]


# ---------------------------------------------------------------------------
# --dry-run-script tests
# ---------------------------------------------------------------------------

_FAKE_SCRIPT = "#!/usr/bin/env bash\nset -euo pipefail\n# bsp_family: generic\necho done\n"


def _stub_generate_script(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace generate_dry_run_script with a fixed stub that returns _FAKE_SCRIPT."""
    monkeypatch.setattr(build_cmd.step_kas, "generate_dry_run_script", lambda *a, **kw: _FAKE_SCRIPT)


def test_dry_run_script_writes_to_file(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """--dry-run-script PATH writes the generated script to PATH and exits 0."""
    _stub_user_config_loader(monkeypatch, hashserv=False)
    _stub_generate_script(monkeypatch)

    out_file = tmp_path / "build.sh"
    result = runner.invoke(app, ["build", str(generic_yaml), "--dry-run-script", str(out_file)])

    assert result.exit_code == 0, result.output
    assert out_file.exists(), "expected output file to be created"
    assert out_file.read_text() == _FAKE_SCRIPT


def test_dry_run_script_stdout(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--dry-run-script - writes the generated script to stdout and exits 0."""
    _stub_user_config_loader(monkeypatch, hashserv=False)
    _stub_generate_script(monkeypatch)

    result = runner.invoke(app, ["build", str(generic_yaml), "--dry-run-script", "-"])

    assert result.exit_code == 0, result.output
    assert _FAKE_SCRIPT in result.output, f"expected script in stdout, got: {result.output!r}"


def test_dry_run_does_not_write_script(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """--dry-run/-n alone must NOT produce a script file.

    Asserts the generate_dry_run_script function is never called and no
    unexpected file appears in tmp_path when only --dry-run is used.
    """
    _stub_user_config_loader(monkeypatch, hashserv=False)

    script_calls: list[tuple] = []
    monkeypatch.setattr(
        build_cmd.step_kas,
        "generate_dry_run_script",
        lambda *a, **kw: script_calls.append((a, kw)) or "",
    )
    monkeypatch.setattr(build_cmd.step_kas, "dry_run_preview_lines", lambda *a, **kw: ["command: kas-container build"])

    result = runner.invoke(app, ["build", str(generic_yaml), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert script_calls == [], "generate_dry_run_script must NOT be called for --dry-run alone"


def test_dry_run_script_does_not_invoke_run_build(
    runner: _CliRunner,
    workspace: Path,
    generic_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """--dry-run-script exits before calling run_build (no actual build)."""
    _stub_user_config_loader(monkeypatch, hashserv=False)
    _stub_generate_script(monkeypatch)

    build_calls: list[object] = []

    def fake_run_build(ctx, *, extra_overlays=None, show_layers=False):  # type: ignore[no-untyped-def]
        build_calls.append(ctx)
        return 0

    monkeypatch.setattr(build_cmd.step_kas, "run_build", fake_run_build)

    out_file = tmp_path / "script.sh"
    result = runner.invoke(app, ["build", str(generic_yaml), "--dry-run-script", str(out_file)])

    assert result.exit_code == 0, result.output
    assert build_calls == [], "run_build must NOT be called when --dry-run-script is used"


# ---------------------------------------------------------------------------
# --preset flag tests
# ---------------------------------------------------------------------------


def _make_nxp_preset() -> object:
    """Return a minimal NXP PresetEntry-like object with known fields."""
    from bakar.preset_config import PresetEntry

    return PresetEntry(
        name="imx8mp-scarthgap",
        family="nxp",
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        branch="scarthgap",
    )


def _make_generic_preset(tmp_path: Path) -> object:
    """Return a minimal generic/bbsetup PresetEntry-like object."""
    from bakar.preset_config import PresetEntry

    yaml_path = tmp_path / "qemux86-64.yml"
    yaml_path.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    return PresetEntry(
        name="avocado-qemux86-64",
        family="generic",
        machine="qemux86-64",
        image="avocado-os",
        kas_yaml=str(yaml_path),
    )


def _stub_preset_loader(monkeypatch: pytest.MonkeyPatch, presets: list) -> None:
    """Patch startup preset loading so the app callback uses our test list.

    The app callback calls ``_load_presets_safe()`` which overwrites the
    module-level ``_PRESETS``.  Patching the function prevents that overwrite
    and makes ``_state._PRESETS`` hold exactly ``presets`` when ``build()`` runs.
    """

    def fake_load_presets_safe() -> None:
        _state._PRESETS = presets  # type: ignore[assignment]

    monkeypatch.setattr(_state, "_load_presets_safe", fake_load_presets_safe)
    monkeypatch.setattr(_state, "_PRESETS", presets, raising=False)


def _stub_dispatchers(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub _dispatch_bsp, _dispatch_from_yaml, detect, and run_build.

    Prevents real filesystem / subprocess operations during preset tests.
    detect() is stubbed to return a fully-populated state so the build pipeline
    skips all sync/setup-env steps without needing --skip-sync.
    """
    from bakar.bsp_model import get_model
    from bakar.workspace import WorkspaceState

    captured: dict = {"bsp_dispatch": [], "yaml_dispatch": [], "run_build": []}

    def fake_dispatch_bsp(manifest):  # type: ignore[no-untyped-def]
        captured["bsp_dispatch"].append(manifest)
        bsp = get_model("nxp")
        return ("nxp", bsp)

    def fake_dispatch_yaml(yaml_path):  # type: ignore[no-untyped-def]
        captured["yaml_dispatch"].append(yaml_path)
        return ("generic", None)

    def fake_run_build(ctx, *, extra_overlays=None, show_layers=False):  # type: ignore[no-untyped-def]
        captured["run_build"].append(ctx)
        return 0

    def fake_detect(cfg):  # type: ignore[no-untyped-def]
        # Return a fully-initialized state so no sync/setup-env/gen-kas steps run.
        return WorkspaceState(
            bsp_family="nxp",
            repo_initialized=True,
            sources_populated=True,
            build_dir_exists=True,
            bblayers_present=True,
            kas_yaml_present=True,
            forks_linux_imx=False,
            cache_dirs_ok=True,
            repo_manifest_include=cfg.manifest,
            repo_manifests_branch=cfg.repo_branch,
            requested_manifest=cfg.manifest,
            requested_branch=cfg.repo_branch,
        )

    monkeypatch.setattr(build_cmd, "_dispatch_bsp", fake_dispatch_bsp)
    monkeypatch.setattr(build_cmd, "_dispatch_from_yaml", fake_dispatch_yaml)
    monkeypatch.setattr(build_cmd.step_kas, "run_build", fake_run_build)
    # Patch 'detect' where it's used in the build command module.
    monkeypatch.setattr("bakar.commands.build.detect", fake_detect)
    return captured


def test_preset_unknown_name_exits_nonzero(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--preset with an unknown name exits non-zero and names the missing preset."""
    _stub_user_config_loader(monkeypatch, hashserv=False)
    # No presets defined.
    _stub_preset_loader(monkeypatch, [])

    result = runner.invoke(app, ["build", "--preset", "does-not-exist"])

    assert result.exit_code != 0, f"expected non-zero exit, got 0; output: {result.output}"
    assert "does-not-exist" in result.output, f"expected preset name in output, got: {result.output!r}"


def test_preset_known_nxp_uses_preset_manifest(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--preset with a known NXP preset sets manifest and routes via _dispatch_bsp."""
    _stub_user_config_loader(monkeypatch, hashserv=False)
    nxp_preset = _make_nxp_preset()
    _stub_preset_loader(monkeypatch, [nxp_preset])
    captured = _stub_dispatchers(monkeypatch)

    # --dry-run skips the actual kas-container invocation; detect() is stubbed
    # so no real sync/setup-env steps run.
    result = runner.invoke(app, ["build", "--preset", "imx8mp-scarthgap", "--dry-run"])

    assert result.exit_code == 0, result.output
    # _dispatch_bsp must have been called with the preset manifest.
    assert captured["bsp_dispatch"] == ["imx-6.6.52-2.2.2.xml"], (
        f"expected dispatch with preset manifest, got {captured['bsp_dispatch']!r}"
    )


def test_preset_explicit_image_overrides_preset(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An explicit --image flag wins over the preset image value.

    Verifies the CLI > preset precedence: the resolved BuildConfig must
    carry the user-supplied image, not the preset image.
    """
    _stub_user_config_loader(monkeypatch, hashserv=False)
    nxp_preset = _make_nxp_preset()
    _stub_preset_loader(monkeypatch, [nxp_preset])

    resolved_configs: list = []

    import bakar.commands.build as build_mod

    original_resolve = build_mod.resolve

    def capturing_resolve(**kwargs):  # type: ignore[no-untyped-def]
        cfg = original_resolve(**kwargs)
        resolved_configs.append(cfg)
        return cfg

    monkeypatch.setattr(build_mod, "resolve", capturing_resolve)
    _stub_dispatchers(monkeypatch)

    result = runner.invoke(
        app,
        ["build", "--preset", "imx8mp-scarthgap", "--image", "custom-image", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert resolved_configs, "resolve() was not called"
    cfg = resolved_configs[0]
    assert cfg.image == "custom-image", f"expected explicit --image to win over preset, got {cfg.image!r}"


def test_preset_dispatch_bbsetup_uses_dispatch_from_yaml(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A bbsetup/generic preset must dispatch via _dispatch_from_yaml, not _dispatch_bsp.

    The preset kas_yaml is passed to _dispatch_from_yaml; _dispatch_bsp must
    not be called at all so a manifest-driven NXP/TI dispatch is never triggered
    for a kas-YAML preset.
    """
    _stub_user_config_loader(monkeypatch, hashserv=False)
    generic_preset = _make_generic_preset(tmp_path)
    _stub_preset_loader(monkeypatch, [generic_preset])

    import bakar.commands.build as build_mod

    yaml_dispatched: list = []
    bsp_dispatched: list = []

    def fake_dispatch_yaml(yaml_path):  # type: ignore[no-untyped-def]
        yaml_dispatched.append(yaml_path)
        return ("generic", None)

    def fake_dispatch_bsp(manifest):  # type: ignore[no-untyped-def]
        bsp_dispatched.append(manifest)
        from bakar.bsp_model import get_model

        return ("nxp", get_model("nxp"))

    monkeypatch.setattr(build_mod, "_dispatch_from_yaml", fake_dispatch_yaml)
    monkeypatch.setattr(build_mod, "_dispatch_bsp", fake_dispatch_bsp)

    # Also stub run_build to prevent container invocation.
    monkeypatch.setattr(build_mod.step_kas, "run_build", lambda ctx, *, extra_overlays=None, show_layers=False: 0)

    result = runner.invoke(app, ["build", "--preset", "avocado-qemux86-64", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert yaml_dispatched, "_dispatch_from_yaml was not called for a generic/bbsetup preset"
    assert not bsp_dispatched, f"_dispatch_bsp must not be called for a generic preset, got {bsp_dispatched!r}"


def test_nxp_preset_output_path_contains_manifest_version(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An NXP preset build resolves into a directory whose path contains the manifest version.

    The workspace passed to resolve() for preset builds is augmented with
    compose_preset_output_path(), so the resulting bsp_root (and runs_dir)
    embed the manifest version string (e.g. "6.6.52-2.2.2") rather than
    landing in the plain workspace/nxp/ tree that non-preset builds use.
    """
    _stub_user_config_loader(monkeypatch, hashserv=False)
    nxp_preset = _make_nxp_preset()
    _stub_preset_loader(monkeypatch, [nxp_preset])

    import bakar.commands.build as build_mod

    resolved_workspaces: list = []
    original_resolve = build_mod.resolve

    def capturing_resolve(**kwargs):  # type: ignore[no-untyped-def]
        resolved_workspaces.append(kwargs.get("workspace"))
        return original_resolve(**kwargs)

    monkeypatch.setattr(build_mod, "resolve", capturing_resolve)
    _stub_dispatchers(monkeypatch)

    result = runner.invoke(app, ["build", "--preset", "imx8mp-scarthgap", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert resolved_workspaces, "resolve() was not called"
    ws_path = resolved_workspaces[0]
    assert ws_path is not None
    assert "6.6.52-2.2.2" in str(ws_path), f"expected manifest version '6.6.52-2.2.2' in workspace path '{ws_path}'"


# ---------------------------------------------------------------------------
# Multi-release fan-out tests (task 3.3)
# ---------------------------------------------------------------------------


def _make_multi_release_nxp_preset() -> object:
    """Return a two-release NXP PresetEntry with distinct manifests."""
    from bakar.preset_config import PresetEntry

    return PresetEntry(
        name="imx8mp-all-releases",
        family="nxp",
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifests=["imx-6.6.52-2.2.2.xml", "imx-6.12.0-1.0.0.xml"],
        branches=["scarthgap", "styhead"],
    )


def _make_multi_release_bbsetup_preset(tmp_path: Path) -> object:
    """Return a two-release bbsetup PresetEntry with two distinct kas YAML stems."""
    from bakar.preset_config import PresetEntry

    yaml_a = tmp_path / "qemux86-64.yml"
    yaml_b = tmp_path / "qemuarm64.yml"
    yaml_a.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    yaml_b.write_text("header:\n  version: 14\nmachine: qemuarm64\n")
    return PresetEntry(
        name="avocado-all-machines",
        family="generic",
        machine="qemux86-64",
        image="avocado-os",
        kas_yamls=[str(yaml_a), str(yaml_b)],
    )


def _stub_single_release_runner(monkeypatch: pytest.MonkeyPatch, *, fail_index: int | None = None) -> list[int]:
    """Stub _run_single_preset_release to record calls and optionally fail one release.

    Returns the list of spec_index values passed to the stub so tests can
    assert sequential execution order.
    """
    import bakar.commands.build as build_mod

    call_indices: list[int] = []

    def fake_run_single(preset, spec_index, **kwargs):  # type: ignore[no-untyped-def]
        call_indices.append(spec_index)
        return 1 if spec_index == fail_index else 0

    monkeypatch.setattr(build_mod, "_run_single_preset_release", fake_run_single)
    return call_indices


def test_multi_release_nxp_runs_two_builds_sequentially(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A two-release NXP preset triggers exactly two sequential _run_single_preset_release calls."""
    _stub_user_config_loader(monkeypatch, hashserv=False)
    preset = _make_multi_release_nxp_preset()
    _stub_preset_loader(monkeypatch, [preset])
    call_indices = _stub_single_release_runner(monkeypatch)

    result = runner.invoke(app, ["build", "--preset", "imx8mp-all-releases"])

    assert result.exit_code == 0, result.output
    assert call_indices == [0, 1], f"expected releases [0, 1] in order, got {call_indices!r}"


def test_multi_release_all_succeed_exits_zero(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-release preset where all releases pass exits with code 0."""
    _stub_user_config_loader(monkeypatch, hashserv=False)
    preset = _make_multi_release_nxp_preset()
    _stub_preset_loader(monkeypatch, [preset])
    _stub_single_release_runner(monkeypatch, fail_index=None)

    result = runner.invoke(app, ["build", "--preset", "imx8mp-all-releases"])

    assert result.exit_code == 0, (
        f"expected exit 0 when all releases succeed, got {result.exit_code}; output: {result.output}"
    )


def test_multi_release_one_fails_exits_nonzero(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-release preset where one release fails exits with code 1."""
    _stub_user_config_loader(monkeypatch, hashserv=False)
    preset = _make_multi_release_nxp_preset()
    _stub_preset_loader(monkeypatch, [preset])
    # Release at index 1 (second release) fails.
    _stub_single_release_runner(monkeypatch, fail_index=1)

    result = runner.invoke(app, ["build", "--preset", "imx8mp-all-releases"])

    assert result.exit_code != 0, f"expected non-zero exit when a release fails, got 0; output: {result.output}"
    assert result.exit_code == 1, f"expected exit code 1, got {result.exit_code}"


def test_multi_release_both_releases_still_run_when_first_fails(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All releases run even when an earlier release fails (fan-out, not fail-fast)."""
    _stub_user_config_loader(monkeypatch, hashserv=False)
    preset = _make_multi_release_nxp_preset()
    _stub_preset_loader(monkeypatch, [preset])
    # Release at index 0 (first release) fails; second must still be attempted.
    call_indices = _stub_single_release_runner(monkeypatch, fail_index=0)

    result = runner.invoke(app, ["build", "--preset", "imx8mp-all-releases"])

    assert result.exit_code == 1, f"expected exit 1 when first release fails, got {result.exit_code}"
    assert call_indices == [0, 1], f"expected both releases attempted in order, got {call_indices!r}"


def test_multi_release_summary_table_printed(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Rich summary table is printed after a multi-release build with two distinct release IDs."""
    _stub_user_config_loader(monkeypatch, hashserv=False)
    preset = _make_multi_release_nxp_preset()
    _stub_preset_loader(monkeypatch, [preset])
    _stub_single_release_runner(monkeypatch)

    result = runner.invoke(app, ["build", "--preset", "imx8mp-all-releases"])

    assert result.exit_code == 0, result.output
    # Both manifest-version strings must appear in the summary table output.
    assert "6.6.52-2.2.2" in result.output, f"expected first release ID in output, got: {result.output!r}"
    assert "6.12.0-1.0.0" in result.output, f"expected second release ID in output, got: {result.output!r}"


def test_multi_release_bbsetup_distinct_output_dirs(
    runner: _CliRunner,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A two-release bbsetup preset produces two distinct output dir names (by kas YAML stem)."""
    _stub_user_config_loader(monkeypatch, hashserv=False)
    preset = _make_multi_release_bbsetup_preset(tmp_path)
    _stub_preset_loader(monkeypatch, [preset])

    import bakar.commands.build as build_mod
    from bakar.config import compose_preset_output_path

    # Capture the output subdirs computed for each release.
    captured_subdirs: list[str] = []

    def capturing_runner(active_preset, spec_index, **kwargs):  # type: ignore[no-untyped-def]
        captured_subdirs.append(compose_preset_output_path(active_preset, spec_index))
        return 0

    monkeypatch.setattr(build_mod, "_run_single_preset_release", capturing_runner)

    result = runner.invoke(app, ["build", "--preset", "avocado-all-machines"])

    assert result.exit_code == 0, result.output
    assert len(captured_subdirs) == 2, f"expected 2 release output dirs, got {captured_subdirs!r}"
    assert captured_subdirs[0] != captured_subdirs[1], (
        f"expected distinct output dirs for two kas YAML stems, got: {captured_subdirs!r}"
    )
    # Each stem should appear in the corresponding output dir name.
    assert "qemux86-64" in captured_subdirs[0], f"expected qemux86-64 stem in first dir: {captured_subdirs[0]!r}"
    assert "qemuarm64" in captured_subdirs[1], f"expected qemuarm64 stem in second dir: {captured_subdirs[1]!r}"


# ---------------------------------------------------------------------------
# build_stop integration (task 4.3)
#
# run_build() calls build_stop.check_unclean_stop(cfg.bsp_root, log.console)
# near the top, and _run_pty_with_ui() writes/removes build.pid around the
# subprocess. These tests stub the heavy parts (the PTY-driven kas-container
# invocation) and assert the build_stop wiring fires. Monkeypatching is
# module-qualified (step_kas.<attr>) to match the established style above.
# ---------------------------------------------------------------------------


def _build_ctx(tmp_path: Path, log: RunLogger) -> KasBuildContext:
    """A KasBuildContext whose kas YAML lives under bsp_root so the real
    _resolve_user_yaml / materialize_overlay path runs without stubbing.

    bsp_root for a generic cfg with no kas_yaml_override is workspace/generic,
    so both the YAML and the overlay are written there.
    """
    cfg = _make_cfg(tmp_path)
    bsp_root = cfg.bsp_root
    bsp_root.mkdir(parents=True, exist_ok=True)
    kas_yaml = bsp_root / "build.yml"
    kas_yaml.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    overlay = bsp_root / "overlay.yml"
    overlay.write_text("header:\n  version: 14\n")
    return KasBuildContext(cfg=cfg, log=log, kas_yaml=kas_yaml, overlay_source=overlay)


def test_run_build_invokes_check_unclean_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_build calls build_stop.check_unclean_stop(cfg.bsp_root, ...) before building.

    The heavy PTY-driven kas-container invocation is stubbed via
    _run_pty_with_ui so no real subprocess launches; clear_stale_bitbake_locks
    is stubbed to skip the /proc lock scan. The assertion pins the first
    positional argument to cfg.bsp_root so a wrong-path regression fails loudly.
    """
    recorded: list[Path] = []

    monkeypatch.setattr(step_kas.build_stop, "check_unclean_stop", lambda bsp_root, console: recorded.append(bsp_root))
    monkeypatch.setattr(step_kas, "clear_stale_bitbake_locks", lambda cfg: [])
    monkeypatch.setattr(step_kas, "_run_pty_with_ui", lambda *a, **kw: _PtyOutcome(rc=0))

    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = _build_ctx(tmp_path, log)
        rc = step_kas.run_build(ctx)

    assert rc == 0
    assert recorded == [ctx.cfg.bsp_root], f"expected check_unclean_stop called with cfg.bsp_root, got {recorded!r}"


def test_run_build_writes_then_removes_build_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_pty_with_ui writes build.pid after Popen and removes it on exit.

    Drives the REAL _run_pty_with_ui (so the write_pid/remove_pid wiring is
    exercised, not stubbed) but fakes subprocess.Popen so no kas-container
    launches: the fake proc exits immediately (wait/poll return 0). Recording
    write_pid/remove_pid confirms both fire in order; asserting no build.pid
    remains confirms the success path leaves a clean run dir.
    """
    calls: list[tuple[str, int]] = []

    def rec_write(run_dir, pgid):  # type: ignore[no-untyped-def]
        calls.append(("write", pgid))

    def rec_remove(run_dir):  # type: ignore[no-untyped-def]
        calls.append(("remove", 0))

    monkeypatch.setattr(step_kas.build_stop, "write_pid", rec_write)
    monkeypatch.setattr(step_kas.build_stop, "remove_pid", rec_remove)
    monkeypatch.setattr(step_kas.build_stop, "check_unclean_stop", lambda *a, **kw: None)
    monkeypatch.setattr(step_kas, "clear_stale_bitbake_locks", lambda cfg: [])

    class _FakeProc:
        pid = 424242

        def wait(self) -> int:
            return 0

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(step_kas.subprocess, "Popen", lambda *a, **kw: _FakeProc())

    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = _build_ctx(tmp_path, log)
        rc = step_kas.run_build(ctx)
        build_pid = log.run_dir / "build.pid"
        leftover = build_pid.exists()

    assert rc == 0
    assert calls == [("write", 424242), ("remove", 0)], f"expected write then remove, got {calls!r}"
    assert not leftover, "build.pid must be removed on the clean-exit path"
