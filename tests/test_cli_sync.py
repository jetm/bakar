"""Tests for the ``bakar sync`` command.

The sync command does not call ``subprocess.run`` directly; it dispatches to
``bsp.sync_step`` which is bound at ``BspModel`` construction time to the
function references in ``bakar.steps.repo`` (NXP, ``init_and_sync``) and
``bakar.steps.ti_layertool`` (TI, ``populate``). Patching ``subprocess.run``
on those two step modules is the seam that keeps the dispatched argv
visible to the test.

The doctor pre-flight always runs now; an autouse fixture stubs ``run_all`` to
an empty pass list so these tests stay host-independent.
``workspace.detect`` is patched to a state that needs a sync (so the step
runs) but does not need a setup_env regenerate (so the second-half of
sync.py is skipped, avoiding the need to patch setup_env too).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from bakar.commands import app
from bakar.workspace import WorkspaceState

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _stub_doctor_checks():
    """Doctor always runs now; stub ``run_all`` to an empty (all-pass) list so these
    tests stay host-independent - real checks BLOCK on disk-free / git config."""
    with patch("bakar.commands._helpers.run_all", return_value=[]):
        yield


def _state_needing_sync(family: str) -> WorkspaceState:
    """Build a WorkspaceState that forces sync but skips setup_env.

    ``needs_repo_sync`` requires ``not (repo_initialized and sources_populated)``;
    leaving both False fires the sync. ``needs_setup_env`` is
    ``needs_full_reinit or not bblayers_present``; with the manifest/branch
    fields aligned and bblayers_present=True the second-half setup_env path
    in ``sync.py`` is skipped.
    """
    return WorkspaceState(
        bsp_family=family,  # type: ignore[arg-type]
        repo_initialized=False,
        sources_populated=False,
        build_dir_exists=False,
        bblayers_present=True,
        kas_yaml_present=False,
        forks_linux_imx=False,
        cache_dirs_ok=True,
        repo_manifest_include=None,
        repo_manifests_branch=None,
        requested_manifest="",
        requested_branch="",
        sha_drift=(),
    )


def _fake_completed(returncode: int = 0) -> MagicMock:
    """A stand-in for a ``subprocess.CompletedProcess`` with the given rc."""
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = ""
    cp.stderr = ""
    return cp


def test_sync_nxp_dispatches_to_repo(
    runner: CliRunner,
    fake_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NXP sync must invoke the ``repo`` tool via ``bakar.steps.repo``.

    Asserts that the patched ``subprocess.run`` in ``bakar.steps.repo`` was
    called and that at least one call's argv begins with ``"repo"``. The
    ``check=True`` keyword means ``subprocess.run`` would raise on a non-zero
    return; the fake returns 0 so the step completes normally.
    """
    monkeypatch.chdir(fake_workspace)
    # Patch detect so it forces sync but skips setup_env. Patch at the
    # sync.py import site so other tests are unaffected.
    monkeypatch.setattr(
        "bakar.commands.sync.detect",
        lambda cfg: _state_needing_sync("nxp"),
    )

    with patch(
        "bakar.steps.repo.subprocess.run",
        return_value=_fake_completed(0),
    ) as mock_run:
        result = runner.invoke(
            app,
            ["sync", "--manifest", "imx-6.6.52-2.2.2.xml"],
        )

    assert result.exit_code == 0, result.output
    assert mock_run.call_count >= 1, "expected bakar.steps.repo.subprocess.run to fire"
    argv_first_tokens = [call.args[0][0] for call in mock_run.call_args_list if call.args]
    assert "repo" in argv_first_tokens, f"expected at least one `repo` invocation, got {argv_first_tokens!r}"


def test_sync_ti_dispatches_to_oe_layertool(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TI sync must invoke oe-layertool, not ``repo``.

    Builds a TI-shaped workspace with the layertool script and config file
    that ``populate`` checks before calling ``subprocess.run``. Both step
    modules' ``subprocess.run`` symbols are the same stdlib reference, so
    a single ``patch("subprocess.run", ...)`` is enough; the assertion
    distinguishes the dispatched argv (``bash ./oe-layertool-setup.sh ...``
    for TI vs ``repo ...`` for NXP).
    """
    # TI workspace layout: marker + ti/ + oe-layertool script + config file.
    (tmp_path / ".bakar.toml").write_text("")
    ti_root = tmp_path / "ti"
    layertool_dir = ti_root / "oe-layertool"
    layertool_dir.mkdir(parents=True)
    (layertool_dir / "oe-layertool-setup.sh").write_text("#!/bin/sh\n")
    manifest_name = "processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt"
    cfg_dir = layertool_dir / "configs" / "variscite"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / manifest_name).write_text("")
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(
        "bakar.commands.sync.detect",
        lambda cfg: _state_needing_sync("ti"),
    )

    def _side(*args: object, **kwargs: object) -> MagicMock:
        # populate() verifies sources/oe-core/oe-init-build-env exists after
        # the script returns and raises otherwise; create it as a side effect
        # so the post-subprocess check passes.
        marker = ti_root / "sources" / "oe-core" / "oe-init-build-env"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("")
        return _fake_completed(0)

    with patch("bakar.steps.ti_layertool.subprocess.run", side_effect=_side) as mock_run:
        result = runner.invoke(
            app,
            ["sync", "--manifest", manifest_name],
        )

    assert result.exit_code == 0, result.output
    assert mock_run.call_count >= 1, "expected the TI subprocess.run to fire"
    # Confirm the dispatched argv is the layertool invocation, NOT `repo`.
    argv0_list = [call.args[0][0] for call in mock_run.call_args_list if call.args]
    assert "repo" not in argv0_list, f"TI subprocess argv must not start with `repo`, got {argv0_list!r}"
    # And confirm the layertool script was actually invoked.
    all_argv0_or_1 = [tok for call in mock_run.call_args_list if call.args for tok in call.args[0][:2]]
    assert any("oe-layertool-setup.sh" in tok for tok in all_argv0_or_1), (
        f"expected the TI layertool script in argv, got {all_argv0_or_1!r}"
    )


def test_sync_nxp_subprocess_failure_propagates(
    runner: CliRunner,
    fake_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ``repo`` call must bubble a non-zero exit out of ``sync``.

    ``init_and_sync`` calls ``subprocess.run(..., check=True)``. With check=True
    a non-zero rc raises ``CalledProcessError``; the test simulates this by
    making the patched function raise directly. The sync command does not
    catch the exception, so Typer surfaces it as a non-zero exit.
    """
    import subprocess

    monkeypatch.chdir(fake_workspace)
    monkeypatch.setattr(
        "bakar.commands.sync.detect",
        lambda cfg: _state_needing_sync("nxp"),
    )

    def _boom(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(returncode=1, cmd=args[0] if args else "repo")

    with patch("bakar.steps.repo.subprocess.run", side_effect=_boom):
        result = runner.invoke(
            app,
            ["sync", "--manifest", "imx-6.6.52-2.2.2.xml"],
        )

    assert result.exit_code != 0, f"expected non-zero exit on subprocess failure, got 0 with output:\n{result.output}"


def test_sync_workspace_not_found_exits_nonzero(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running ``sync`` outside a workspace must exit non-zero.

    ``_workspace_from_cwd`` walks parents looking for a marker; a bare
    ``tmp_path`` has none, so the helper raises ``typer.Exit(2)``. The
    error message names the missing marker so the user knows how to fix.
    """
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["sync", "--manifest", "imx-6.6.52-2.2.2.xml"],
    )

    assert result.exit_code != 0, f"expected non-zero exit when no workspace marker exists, got 0:\n{result.output}"


def test_sync_dry_run_script_stdout(
    runner: CliRunner,
    fake_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run-script -`` writes a bash script to stdout and exits 0.

    The script must start with the shebang line and contain a provenance
    comment naming the bsp_family. No subprocess calls are made; the
    command exits before sync.
    """
    monkeypatch.chdir(fake_workspace)

    result = runner.invoke(
        app,
        ["sync", "--manifest", "imx-6.6.52-2.2.2.xml", "--dry-run-script", "-"],
    )

    assert result.exit_code == 0, result.output
    assert result.output.startswith("#!/usr/bin/env bash"), (
        f"expected shebang as first line, got:\n{result.output[:200]}"
    )
    assert "# bsp_family: nxp" in result.output, f"expected bsp_family comment in script, got:\n{result.output[:400]}"


def test_sync_dry_run_script_file(
    runner: CliRunner,
    fake_workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run-script PATH`` writes the script to a file and exits 0.

    The command must not invoke any sync subprocess and the written file
    must contain the shebang header.
    """
    monkeypatch.chdir(fake_workspace)
    script_path = tmp_path / "sync.sh"

    result = runner.invoke(
        app,
        ["sync", "--manifest", "imx-6.6.52-2.2.2.xml", "--dry-run-script", str(script_path)],
    )

    assert result.exit_code == 0, result.output
    assert script_path.exists(), "expected script file to be written"
    content = script_path.read_text()
    assert content.startswith("#!/usr/bin/env bash"), f"expected shebang, got:\n{content[:200]}"


def test_sync_dry_run_script_nxp_contains_repo(
    runner: CliRunner,
    fake_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NXP ``--dry-run-script`` output must contain ``repo init``/``repo sync``.

    The sync step in the generated script branches on ``cfg.bsp_family``;
    for the NXP family the correct tool is ``repo``.
    """
    monkeypatch.chdir(fake_workspace)

    result = runner.invoke(
        app,
        ["sync", "--manifest", "imx-6.6.52-2.2.2.xml", "--dry-run-script", "-"],
    )

    assert result.exit_code == 0, result.output
    assert "repo init" in result.output, f"expected 'repo init' in script sync step, got:\n{result.output}"
    assert "repo sync" in result.output, f"expected 'repo sync' in script sync step, got:\n{result.output}"


def test_sync_dry_run_without_script_writes_no_file(
    runner: CliRunner,
    fake_workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` (without ``--dry-run-script``) must not create a script file.

    The plain ``--dry-run`` flag calls ``_print_dry_run`` and exits; it must
    never write any file to the workspace.
    """
    monkeypatch.chdir(fake_workspace)
    before = set((fake_workspace).rglob("*.sh"))

    result = runner.invoke(
        app,
        ["sync", "--manifest", "imx-6.6.52-2.2.2.xml", "--dry-run"],
    )

    after = set((fake_workspace).rglob("*.sh"))
    assert result.exit_code == 0, result.output
    assert after == before, f"unexpected script file(s) written: {after - before}"


def test_sync_dry_run_preview_goes_to_stderr_not_stdout(
    runner: CliRunner,
    fake_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``command: ...`` preview line is human-facing prose, so it must land
    on stderr and leave stdout empty - ``bakar sync --dry-run > payload`` must
    produce an empty file, matching the invariant ``commands/_app.py`` states
    for every other command's human-readable output.

    Also pins the preview as a single VERBATIM line: this command string is
    long enough to hard-wrap under Rich's console width, and a console.print
    regression would break it across lines mid-flag - a bare
    ``"command:" in result.stderr`` substring check would not catch that."""
    monkeypatch.chdir(fake_workspace)

    result = runner.invoke(
        app,
        ["sync", "--manifest", "imx-6.6.52-2.2.2.xml", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "", f"expected stdout empty, got: {result.stdout!r}"
    lines = result.stderr.splitlines()
    assert len(lines) == 1, f"expected exactly 1 unwrapped line, got: {result.stderr!r}"
    assert lines[0].startswith("command: ") and lines[0].endswith("--no-clone-bundle"), (
        f"expected an unwrapped 'command: ...' line, got: {lines[0]!r}"
    )


def test_sync_ctx_carries_every_flag_unchanged(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every _SyncCtx field equals the flag ``sync()`` was invoked with.

    Packing ten CLI parameters into a context is wrong in a way nothing else
    here sees: transpose two fields or drop one and the command still runs,
    ``--help`` is unchanged, and every other test stays green. This one
    captures the context object and compares it field-by-field against a flag
    set where no value is the default."""
    import dataclasses

    import bakar.commands.sync as sync_mod

    ws_dir = tmp_path / "ctx-workspace"
    ws_dir.mkdir()

    captured: list[object] = []
    monkeypatch.setattr(sync_mod, "_sync_impl", captured.append)

    result = runner.invoke(
        app,
        [
            "sync",
            "--machine",
            "ctx-machine",
            "--distro",
            "ctx-distro",
            "--image",
            "ctx-image",
            "--manifest",
            "ctx-manifest.xml",
            "--branch",
            "ctx-branch",
            "--clean",
            "--workspace",
            str(ws_dir),
            "--show-layers",
            "--dry-run",
            "--dry-run-script",
            "ctx-script.sh",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1, f"expected one ctx, got {len(captured)}"
    ctx = captured[0]

    expected = {
        "machine": "ctx-machine",
        "distro": "ctx-distro",
        "image": "ctx-image",
        "manifest": "ctx-manifest.xml",
        "branch": "ctx-branch",
        "clean": True,
        "workspace": ws_dir.resolve(),
        "show_layers": True,
        "dry_run": True,
        "dry_run_script": "ctx-script.sh",
    }
    for field, want in expected.items():
        assert getattr(ctx, field) == want, f"_SyncCtx.{field}: expected {want!r}, got {getattr(ctx, field)!r}"
    # Guards against a field being added to the dataclass but left unasserted.
    assert {f.name for f in dataclasses.fields(ctx)} == set(expected)


@pytest.mark.parametrize(
    ("flag", "field"),
    [
        ("--clean", "clean"),
        ("--show-layers", "show_layers"),
        ("--dry-run", "dry_run"),
    ],
)
def test_sync_ctx_sets_only_the_flag_passed(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    field: str,
) -> None:
    """One boolean at a time: the named field is set and its neighbours are not.

    The all-flags test above gives every boolean the same value, so a
    transposition between two of them is invisible to it. Passing exactly one
    boolean flag per case makes the values differ, which is what catches a
    swapped pair."""
    import dataclasses

    import bakar.commands.sync as sync_mod

    bool_fields = [f.name for f in dataclasses.fields(sync_mod._SyncCtx) if f.type == "bool"]
    assert field in bool_fields, f"{field} is not a bool field of _SyncCtx"

    captured: list[object] = []
    monkeypatch.setattr(sync_mod, "_sync_impl", captured.append)

    result = runner.invoke(app, ["sync", flag])

    assert result.exit_code == 0, result.output
    assert len(captured) == 1, f"expected one ctx, got {len(captured)}"
    ctx = captured[0]

    for name in bool_fields:
        want = name == field
        assert getattr(ctx, name) is want, (
            f"passing {flag} should set only {field}: _SyncCtx.{name} is {getattr(ctx, name)!r}"
        )


@pytest.mark.parametrize(
    ("family", "expected"),
    [("nxp", "repo_sync"), ("qcom", "repo_sync"), ("ti", "ti_layertool")],
)
def test_sync_skip_label_follows_family(monkeypatch: pytest.MonkeyPatch, family: str, expected: str) -> None:
    """A skipped sync step is labelled repo_sync for repo families (nxp, qcom), ti_layertool for ti."""
    from dataclasses import replace

    from bakar.commands.sync import _run_sync_body

    synced = replace(
        _state_needing_sync(family),
        repo_initialized=True,
        sources_populated=True,
        repo_manifest_include="m.xml",
        requested_manifest="m.xml",
    )
    monkeypatch.setattr("bakar.commands.sync.detect", lambda cfg: synced)
    monkeypatch.setattr("bakar.commands.sync._run_doctor_gate", lambda *a, **k: None)
    log = MagicMock()
    _run_sync_body(MagicMock(), log, bsp=MagicMock(), family=family, effective_show_layers=False)
    skipped = [c.args[0] for c in log.step_skip.call_args_list]
    assert expected in skipped
    if family == "qcom":
        assert "ti_layertool" not in skipped


def test_sync_clean_refuses_when_a_peer_holds_the_marker(
    runner: CliRunner,
    fake_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sync --clean`` reaches the live-build gate: no sync starts, the build dir survives."""
    from bakar import build_stop

    build_dir = fake_workspace / "nxp" / "build"
    (build_dir / "tmp").mkdir(parents=True)
    marker = build_dir / ".bakar-lock-marker"
    refusal = build_stop.LockRefusal(reason="peer-held", host="pc2", marker=marker)
    monkeypatch.setattr("bakar.build_stop.lock_mutation_guard", lambda c: refusal)
    monkeypatch.chdir(fake_workspace)
    monkeypatch.setattr("bakar.commands.sync.detect", lambda cfg: _state_needing_sync("nxp"))

    with patch("bakar.steps.repo.subprocess.run", return_value=_fake_completed(0)) as mock_run:
        result = runner.invoke(app, ["sync", "--manifest", "imx-6.6.52-2.2.2.xml", "--clean"])

    assert result.exit_code == 1, result.output
    assert "pc2" in result.output
    assert mock_run.call_count == 0
    assert (build_dir / "tmp").is_dir()


# ---------------------------------------------------------------------------
# kas form: ``bakar sync <meta-avocado kas file>`` moves clones onto their pins
# ---------------------------------------------------------------------------

_PIN_URL = "https://example.invalid/repo-a.git"


def _git(*args: str) -> str:
    import os
    import subprocess

    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    ident = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    done = subprocess.run(["git", *ident, *args], check=True, capture_output=True, text=True, env=env)
    return done.stdout.strip()


def _pinned_clone(ws: Path, name: str = "repo-a") -> tuple[Path, str, str]:
    """A clone under ``ws`` sitting at an old commit; returns (path, old sha, pin sha)."""
    repo = ws / name
    _git("init", "-q", "-b", "main", str(repo))
    (repo / "f").write_text("old")
    _git("-C", str(repo), "add", "-A")
    _git("-C", str(repo), "commit", "-q", "-m", "old")
    old = _git("-C", str(repo), "rev-parse", "HEAD")
    (repo / "f").write_text("new")
    _git("-C", str(repo), "commit", "-q", "-am", "new")
    pin = _git("-C", str(repo), "rev-parse", "HEAD")
    _git("-C", str(repo), "checkout", "-q", "--detach", old)
    return repo, old, pin


def _patch_pins(monkeypatch: pytest.MonkeyPatch, pin: str, release: str = "wrynose") -> None:
    from bakar.steps import kas_pins

    repos = {"repo-a": {"url": _PIN_URL, "commit": pin, "path": "repo-a"}}
    monkeypatch.setattr(kas_pins, "probe_pins", lambda _yaml, _checkout: (release, repos))


def _norm(text: str) -> str:
    return " ".join(text.split())


def test_sync_kas_moves_local_workspace(
    runner: CliRunner, meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay = meta_avocado_worktree_layout
    repo, _old, pin = _pinned_clone(lay.ws)
    _patch_pins(monkeypatch, pin)
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")

    result = runner.invoke(app, ["sync", str(lay.kas_yaml), "-w", str(lay.ws)])

    assert result.exit_code == 0, result.output
    assert "MOVED repo-a" in _norm(result.output)
    assert _git("-C", str(repo), "rev-parse", "HEAD") == pin


def test_sync_kas_dry_run_changes_nothing(
    runner: CliRunner, meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay = meta_avocado_worktree_layout
    repo, old, pin = _pinned_clone(lay.ws)
    _patch_pins(monkeypatch, pin)
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")

    result = runner.invoke(app, ["sync", str(lay.kas_yaml), "-w", str(lay.ws), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert f"repo-a: would move to {pin[:12]}" in _norm(result.output)
    assert "would still block" not in _norm(result.output)
    assert _git("-C", str(repo), "rev-parse", "HEAD") == old
    assert not (lay.ws / ".bakar").exists()


def test_sync_kas_dry_run_exits_1_when_a_dirty_bitbake_at_its_pin_would_still_block(
    runner: CliRunner, meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dry run predicts the real sync's post-sync re-check, exit code included."""
    from bakar.steps import kas_pins

    lay = meta_avocado_worktree_layout
    repo, _old, pin = _pinned_clone(lay.ws, "bitbake")
    _git("-C", str(repo), "checkout", "-q", "--detach", pin)
    (repo / "f").write_text("local edit")
    repos = {"bitbake": {"url": _PIN_URL, "commit": pin, "path": "bitbake"}}
    monkeypatch.setattr(kas_pins, "probe_pins", lambda _yaml, _checkout: ("wrynose", repos))

    dry = runner.invoke(app, ["sync", str(lay.kas_yaml), "-w", str(lay.ws), "--dry-run"])
    real = runner.invoke(app, ["sync", str(lay.kas_yaml), "-w", str(lay.ws)])

    out = _norm(dry.output)
    assert dry.exit_code == 1, dry.output
    assert "bitbake: already at its pin (has local changes)" in out
    assert "after the sync 1 repository would still block a build, and bakar sync cannot clear it:" in out
    assert f"bitbake ({repo}): off its pin or has local changes" in out
    assert real.exit_code == dry.exit_code, real.output


def _frozen_bitbake_and_dirty_core(ws: Path) -> dict:
    """A clean bitbake at its pin and an openembedded-core at its pin with a local edit."""
    bitbake, _old, bb_pin = _pinned_clone(ws, "bitbake")
    _git("-C", str(bitbake), "checkout", "-q", "--detach", bb_pin)
    core, _old, core_pin = _pinned_clone(ws, "openembedded-core")
    _git("-C", str(core), "checkout", "-q", "--detach", core_pin)
    (core / "f").write_text("local edit")
    return {
        "bitbake": {"url": _PIN_URL, "commit": bb_pin, "path": "bitbake"},
        "openembedded-core": {"url": _PIN_URL, "commit": core_pin, "path": "openembedded-core"},
    }


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("container", [True, False])
def test_sync_kas_pairs_core_with_bitbake_only_in_host_mode(
    runner: CliRunner,
    meta_avocado_worktree_layout,
    monkeypatch: pytest.MonkeyPatch,
    container: bool,
    dry_run: bool,
) -> None:
    """A container build refuses a dirty openembedded-core, so the re-check and the dry run do too."""
    from bakar.steps import kas_pins

    lay = meta_avocado_worktree_layout
    repos = _frozen_bitbake_and_dirty_core(lay.ws)
    monkeypatch.setattr(kas_pins, "probe_pins", lambda _yaml, _checkout: ("wrynose", repos))
    monkeypatch.setenv("BAKAR_CONTAINER", "1" if container else "0")

    argv = ["sync", str(lay.kas_yaml), "-w", str(lay.ws), *(["--dry-run"] if dry_run else [])]
    result = runner.invoke(app, argv)

    assert result.exit_code == (1 if container else 0), result.output
    core = lay.ws / "openembedded-core"
    assert (f"openembedded-core ({core}): off its pin or has local changes" in _norm(result.output)) is container


def test_sync_kas_output_replaces_control_characters(
    runner: CliRunner, meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kas key carrying an escape sequence cannot rewrite earlier terminal lines."""
    from bakar.steps import kas_pins

    lay = meta_avocado_worktree_layout
    repos = {"repo-\x1b[2Kb": {"url": _PIN_URL, "path": "repo-b"}}
    monkeypatch.setattr(kas_pins, "probe_pins", lambda _yaml, _checkout: ("wrynose", repos))

    result = runner.invoke(app, ["sync", str(lay.kas_yaml), "-w", str(lay.ws), "--dry-run"])

    assert "repo-?[2Kb: no commit pin" in result.output
    assert "\x1b" not in result.output


def test_sync_kas_exits_1_when_a_dirty_bitbake_at_its_pin_still_blocks_the_build(
    runner: CliRunner, meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bakar.steps import kas_pins

    lay = meta_avocado_worktree_layout
    repo, _old, pin = _pinned_clone(lay.ws, "bitbake")
    _git("-C", str(repo), "checkout", "-q", "--detach", pin)
    (repo / "f").write_text("local edit")
    repos = {"bitbake": {"url": _PIN_URL, "commit": pin, "path": "bitbake"}}
    monkeypatch.setattr(kas_pins, "probe_pins", lambda _yaml, _checkout: ("wrynose", repos))

    result = runner.invoke(app, ["sync", str(lay.kas_yaml), "-w", str(lay.ws)])

    out = _norm(result.output)
    assert result.exit_code == 1, result.output
    assert "AT PIN bitbake (has local changes)" in out
    assert "1 repository still blocks a build" in out
    assert f"bitbake ({repo}): off its pin or has local changes" in out
    assert out.index("still blocks") < out.index("batch complete")
    assert (repo / "f").read_text() == "local edit"


def test_sync_kas_dirty_clone_a_build_tolerates_does_not_fail_the_sync(
    runner: CliRunner, meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay = meta_avocado_worktree_layout
    repo, _old, pin = _pinned_clone(lay.ws)
    _git("-C", str(repo), "checkout", "-q", "--detach", pin)
    (repo / "f").write_text("local edit")
    _patch_pins(monkeypatch, pin)

    result = runner.invoke(app, ["sync", str(lay.kas_yaml), "-w", str(lay.ws)])

    assert result.exit_code == 0, result.output
    assert "AT PIN repo-a (has local changes)" in _norm(result.output)
    assert "still block" not in _norm(result.output)


def test_sync_kas_dry_run_names_an_entry_without_a_pin(
    runner: CliRunner, meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bakar.steps import kas_pins

    lay = meta_avocado_worktree_layout
    repos = {"repo-b": {"url": _PIN_URL, "path": "repo-b"}}
    monkeypatch.setattr(kas_pins, "probe_pins", lambda _yaml, _checkout: ("wrynose", repos))

    result = runner.invoke(app, ["sync", str(lay.kas_yaml), "-w", str(lay.ws), "--dry-run"])

    # A build refuses an entry without a pin and sync cannot give it one, so the dry run fails as the sync does.
    assert result.exit_code == 1, result.output
    assert "repo-b: no commit pin" in _norm(result.output)
    assert "1 repository would still block a build" in _norm(result.output)


def test_sync_kas_on_runs_script_on_host(
    runner: CliRunner, meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from bakar.steps import remote_dispatch
    from bakar.user_config import UserConfig

    lay = meta_avocado_worktree_layout
    repo, _old, pin = _pinned_clone(lay.ws)
    _patch_pins(monkeypatch, pin)
    cfg = UserConfig(workspaces_releases={"wrynose": str(lay.ws)})
    monkeypatch.setattr("bakar.commands._app.load_user_config", lambda *a, **k: cfg)
    checked: list[tuple] = []
    monkeypatch.setattr(remote_dispatch, "check_remote_workspace", lambda *a: checked.append(a))
    scripts: list[str] = []

    def fake_ssh(host: str, script: str, **_kw) -> subprocess.CompletedProcess:
        scripts.append(script)
        return subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True, check=False)

    monkeypatch.setattr(remote_dispatch, "_ssh_bash_capture", fake_ssh)

    result = runner.invoke(app, ["sync", str(lay.kas_yaml), "--on", "pc2"])

    assert result.exit_code == 0, result.output
    assert checked == [("pc2", str(lay.ws), "wrynose")]
    # The sync itself, then a read-only probe that re-checks what still blocks a build.
    assert len(scripts) == 2
    assert "BAKAR_SYNC" in scripts[0]
    assert "BAKAR_PIN" in scripts[1]
    assert "MOVED repo-a" in _norm(result.output)
    assert _git("-C", str(repo), "rev-parse", "HEAD") == pin


@pytest.mark.parametrize(
    "flag",
    [
        ["--machine", "m"],
        ["--distro", "d"],
        ["--image", "i"],
        ["--manifest", "x.xml"],
        ["--branch", "b"],
        ["--clean"],
        ["--show-layers"],
        ["--dry-run-script", "-"],
    ],
)
def test_sync_kas_conflicts_refused(
    runner: CliRunner, meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch, flag: list[str]
) -> None:
    lay = meta_avocado_worktree_layout
    _patch_pins(monkeypatch, "0" * 40)

    result = runner.invoke(app, ["sync", str(lay.kas_yaml), "-w", str(lay.ws), *flag])

    assert result.exit_code == 2, result.output
    assert flag[0] in _norm(result.output)
    assert not (lay.ws / ".bakar").exists()


def test_sync_non_meta_avocado_kas_refused(runner: CliRunner, tmp_path: Path) -> None:
    kas = tmp_path / "other" / "my.yml"
    kas.parent.mkdir()
    kas.write_text("header:\n  version: 14\nmachine: qemux86-64\n")

    result = runner.invoke(app, ["sync", str(kas), "-w", str(tmp_path)])

    assert result.exit_code == 2, result.output
    assert "supports meta-avocado release workspaces only" in _norm(result.output)
    assert not (tmp_path / ".bakar").exists()


def test_sync_on_missing_workspace_refused(
    runner: CliRunner, meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bakar.steps import remote_dispatch
    from bakar.user_config import UserConfig

    lay = meta_avocado_worktree_layout
    _patch_pins(monkeypatch, "0" * 40)
    cfg = UserConfig(workspaces_releases={"wrynose": str(lay.ws)})
    monkeypatch.setattr("bakar.commands._app.load_user_config", lambda *a, **k: cfg)

    def missing(host: str, workspace: str, release: str) -> None:
        raise remote_dispatch.DispatchError(f"the release workspace {workspace} does not exist on {host}")

    monkeypatch.setattr(remote_dispatch, "check_remote_workspace", missing)
    ran: list[str] = []
    monkeypatch.setattr(remote_dispatch, "_ssh_bash_capture", lambda *a, **k: ran.append("ssh"))

    result = runner.invoke(app, ["sync", str(lay.kas_yaml), "--on", "pc2"])

    # A host that cannot be checked is not a usage error.
    assert result.exit_code == 1, result.output
    assert "does not exist on pc2" in _norm(result.output)
    assert ran == []


def _invoke_dry_and_real(runner: CliRunner, lay) -> tuple:
    dry = runner.invoke(app, ["sync", str(lay.kas_yaml), "-w", str(lay.ws), "--dry-run"])
    real = runner.invoke(app, ["sync", str(lay.kas_yaml), "-w", str(lay.ws)])
    return dry, real


def test_sync_kas_dry_run_exits_1_for_a_clone_the_sync_would_leave_dirty(
    runner: CliRunner, meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay = meta_avocado_worktree_layout
    repo, _old, pin = _pinned_clone(lay.ws)
    (repo / "f").write_text("local edit")
    _patch_pins(monkeypatch, pin)
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")

    dry, real = _invoke_dry_and_real(runner, lay)

    assert "repo-a: left alone because of local changes" in _norm(dry.output)
    assert "LEFT repo-a" in _norm(real.output)
    assert (dry.exit_code, real.exit_code) == (1, 1), (dry.output, real.output)


def test_sync_kas_dry_run_exits_1_for_a_rejected_entry(
    runner: CliRunner, meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bakar.steps import kas_pins

    lay = meta_avocado_worktree_layout
    _repo, _old, pin = _pinned_clone(lay.ws)
    repos = {"repo-a": {"url": "-evil", "commit": pin, "path": "repo-a"}}
    monkeypatch.setattr(kas_pins, "probe_pins", lambda _yaml, _checkout: ("wrynose", repos))
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")

    dry, real = _invoke_dry_and_real(runner, lay)

    assert "repo-a: cannot be synced (url begins with '-')" in _norm(dry.output)
    assert (dry.exit_code, real.exit_code) == (1, 1), (dry.output, real.output)


def test_sync_kas_abbreviated_pin_already_at_its_commit_is_at_pin(
    runner: CliRunner, meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay = meta_avocado_worktree_layout
    repo, _old, pin = _pinned_clone(lay.ws)
    _git("-C", str(repo), "checkout", "-q", "--detach", pin)
    _patch_pins(monkeypatch, pin[:12])
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")

    dry, real = _invoke_dry_and_real(runner, lay)

    # Only git in the clone can tell whether the prefix names one commit, so the dry run claims no more.
    assert "repo-a: HEAD starts with its abbreviated pin, which a dry run cannot resolve" in _norm(dry.output)
    assert "already at its pin" not in dry.output
    assert "AT PIN repo-a" in _norm(real.output)
    assert (dry.exit_code, real.exit_code) == (0, 0), (dry.output, real.output)


def test_sync_kas_dry_run_checks_an_abbreviated_pin_before_local_changes(
    runner: CliRunner, meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay = meta_avocado_worktree_layout
    repo, old, pin = _pinned_clone(lay.ws)
    (repo / "f").write_text("dirty")
    _patch_pins(monkeypatch, pin[:12])
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")

    dry, real = _invoke_dry_and_real(runner, lay)

    # The script checks the pin's length before the clone's state, and the dry run says what it will.
    assert "repo-a: cannot be synced (pin is not a full 40-character commit id)" in _norm(dry.output)
    assert "FAIL repo-a pin is not a full 40-character commit id" in _norm(real.output)
    assert (dry.exit_code, real.exit_code) == (1, 1), (dry.output, real.output)
    assert _git("-C", str(repo), "rev-parse", "HEAD") == old


def test_sync_kas_abbreviated_pin_off_its_commit_is_not_moved(
    runner: CliRunner, meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay = meta_avocado_worktree_layout
    repo, old, pin = _pinned_clone(lay.ws)
    _patch_pins(monkeypatch, pin[:12])
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")

    dry, real = _invoke_dry_and_real(runner, lay)

    assert "repo-a: cannot be synced (pin is not a full 40-character commit id)" in _norm(dry.output)
    assert "FAIL repo-a pin is not a full 40-character commit id" in _norm(real.output)
    assert (dry.exit_code, real.exit_code) == (1, 1), (dry.output, real.output)
    assert _git("-C", str(repo), "rev-parse", "HEAD") == old


def test_sync_on_with_workspace_option_refused(
    runner: CliRunner, meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay = meta_avocado_worktree_layout
    _patch_pins(monkeypatch, "0" * 40)

    result = runner.invoke(app, ["sync", str(lay.kas_yaml), "--on", "pc2", "-w", str(lay.ws)])

    assert result.exit_code == 2, result.output
    assert "-w cannot be combined with --on" in _norm(result.output)


def test_sync_on_without_kas_refused(runner: CliRunner) -> None:
    result = runner.invoke(app, ["sync", "--on", "pc2"])

    assert result.exit_code == 2, result.output
    assert "--on needs a kas file" in _norm(result.output)


# ---------------------------------------------------------------------------
# bakar sync --revert
# ---------------------------------------------------------------------------


def _revert_record(ws: Path, repo: Path, head: str, branch: str = "main") -> Path:
    record_dir = ws / ".bakar" / "sync-revert"
    record_dir.mkdir(parents=True)
    record = record_dir / "20260101-000000-1.tsv"
    record.write_text(f"repo-a\t{repo}\t{head}\t{branch}\n")
    return record


def test_sync_revert_restores_local(runner: CliRunner, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    repo, _old, pin = _pinned_clone(ws)
    record = _revert_record(ws, repo, pin)

    result = runner.invoke(app, ["sync", "--revert", str(record)])

    assert result.exit_code == 0, result.output
    out = _norm(result.output)
    assert "RESTORED line 1" in out
    assert "batch complete: 1/1" in out
    assert _git("-C", str(repo), "rev-parse", "HEAD") == pin
    assert _git("-C", str(repo), "symbolic-ref", "--short", "HEAD") == "main"


def test_sync_revert_on_host(runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from bakar.steps import remote_dispatch

    ws = tmp_path / "ws"
    ws.mkdir()
    repo, _old, pin = _pinned_clone(ws)
    record = _revert_record(ws, repo, pin)
    seen: list[str] = []

    def fake_ssh(host: str, script: str, **_kw) -> subprocess.CompletedProcess:
        seen.append(host)
        return subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True, check=False)

    monkeypatch.setattr(remote_dispatch, "_ssh_bash_capture", fake_ssh)

    result = runner.invoke(app, ["sync", "--revert", str(record), "--on", "pc2"])

    assert result.exit_code == 0, result.output
    assert seen == ["pc2"]
    assert "RESTORED line 1" in _norm(result.output)
    assert _git("-C", str(repo), "rev-parse", "HEAD") == pin


def test_sync_revert_refuses_path_outside_sync_revert_dir(runner: CliRunner, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    repo, old, pin = _pinned_clone(ws)
    stray = ws / "record.tsv"
    stray.write_text(f"repo-a\t{repo}\t{pin}\tmain\n")

    result = runner.invoke(app, ["sync", "--revert", str(stray)])

    assert result.exit_code == 2, result.output
    assert "sync-revert" in _norm(result.output)
    assert _git("-C", str(repo), "rev-parse", "HEAD") == old


@pytest.mark.parametrize(
    "extra",
    [["--dry-run"], ["--machine", "m"], ["--clean"], ["--dry-run-script", "-"]],
)
def test_sync_revert_conflicts_refused(runner: CliRunner, tmp_path: Path, extra: list[str]) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    repo, old, pin = _pinned_clone(ws)
    record = _revert_record(ws, repo, pin)

    result = runner.invoke(app, ["sync", "--revert", str(record), *extra])
    with_kas = runner.invoke(app, ["sync", "some.yml", "--revert", str(record)])

    assert result.exit_code == 2, result.output
    assert extra[0] in _norm(result.output)
    assert with_kas.exit_code == 2, with_kas.output
    assert _git("-C", str(repo), "rev-parse", "HEAD") == old


def test_sync_revert_refuses_workspace_option(runner: CliRunner, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    repo, old, pin = _pinned_clone(ws)
    record = _revert_record(ws, repo, pin)

    result = runner.invoke(app, ["sync", "--revert", str(record), "-w", str(ws)])

    assert result.exit_code == 2, result.output
    assert "-w" in _norm(result.output)
    assert _git("-C", str(repo), "rev-parse", "HEAD") == old


def test_sync_revert_on_host_refuses_a_relative_record(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a host a relative record resolves against the login directory, so it is refused."""
    from bakar.steps import pin_sync

    ran: list[str] = []
    monkeypatch.setattr(pin_sync, "run_host", lambda host, script: ran.append(host) or (0, "", ""))

    result = runner.invoke(app, ["sync", "--revert", "ws/.bakar/sync-revert/r.tsv", "--on", "pc2"])

    assert result.exit_code == 2, result.output
    assert "--revert FILE on a host must be an absolute path" in _norm(result.output)
    assert ran == []


def test_sync_revert_refuses_option_like_host(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bakar.steps import pin_sync

    ran: list[str] = []
    monkeypatch.setattr(pin_sync, "run_host", lambda host, script: ran.append(host) or (0, "", ""))
    record = tmp_path / ".bakar" / "sync-revert" / "r.tsv"

    result = runner.invoke(app, ["sync", "--revert", str(record), "--on=-oProxyCommand=x"])

    assert result.exit_code == 2, result.output
    assert "begins with '-'" in _norm(result.output)
    assert ran == []


def test_sync_kas_form_refuses_option_like_host(
    runner: CliRunner, meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay = meta_avocado_worktree_layout
    _patch_pins(monkeypatch, "0" * 40)

    result = runner.invoke(app, ["sync", str(lay.kas_yaml), "--on=-oProxyCommand=x"])

    assert result.exit_code == 2, result.output
    assert "begins with '-'" in _norm(result.output)
