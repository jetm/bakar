"""Tests for the ``bakar feed`` sub-app.

The CLI's job is argument plumbing and reporting, so the feed modules are mocked
where they would touch a build. Three things are NOT mocked, because they are the
parts that have been wrong before: whether the sub-app is actually registered on
the root CLI, whether ``gc`` can reach deletion without ``--confirm``, and
whether the arguments it hands ``apply_retention`` are the ones it resolved.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest
import typer
from typer.testing import CliRunner

from bakar import feed_retention
from bakar.commands import app

pytestmark = pytest.mark.unit

_VERBS = ("sync", "index", "serve", "stop", "status", "gc")


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


@pytest.fixture
def feed_root(tmp_path: Path) -> Path:
    root = tmp_path / "feed"
    (root / "2024" / "edge").mkdir(parents=True)
    return root


@pytest.fixture
def cfg(tmp_path: Path, feed_root: Path):
    """A config stub exposing only what the feed commands read."""
    return mock.Mock(
        effective_feed_dir=feed_root,
        resolved_tmpdir=tmp_path / "build" / "tmp",
        workspace=tmp_path,
    )


def _patch_cfg(cfg):
    return mock.patch("bakar.commands.feed._resolve_cfg", return_value=cfg)


def _yaml(tmp_path: Path) -> Path:
    path = tmp_path / "machine.yml"
    path.write_text("header: {}\n")
    return path


def _with_repo_map(cfg) -> Path:
    deploy = cfg.resolved_tmpdir / "deploy" / "rpm"
    deploy.mkdir(parents=True)
    (deploy / "avocado-repo.map").write_text("repo=target/qemux86-64\n")
    return deploy


def _real_scripts(tmp_path: Path) -> Path:
    """A scripts dir the preflight accepts: both files present and executable."""
    directory = tmp_path / "meta-avocado" / "scripts"
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("render-pool-local.py", "repo-stage-rpms.sh"):
        path = directory / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
    return directory


def _tools_present():
    """Stub every external tool as present, so preflight is not the thing under
    test in a sync test."""
    return mock.patch("bakar.feed_preflight.shutil.which", side_effect=lambda tool: f"/usr/bin/{tool}")


# --- registration ----------------------------------------------------------


def test_the_sub_app_is_registered_on_the_root_cli(cli: CliRunner) -> None:
    result = cli.invoke(app, ["feed", "--help"])

    assert result.exit_code == 0
    for verb in _VERBS:
        assert verb in result.output


def test_every_verb_is_reachable(cli: CliRunner) -> None:
    for verb in _VERBS:
        result = cli.invoke(app, ["feed", verb, "--help"])
        assert result.exit_code == 0, f"{verb}: {result.output}"


# --- sync ------------------------------------------------------------------


def test_sync_requires_a_kas_yaml(cli: CliRunner) -> None:
    result = cli.invoke(app, ["feed", "sync"])

    assert result.exit_code != 0
    assert "Missing argument" in result.output


def test_sync_refuses_a_deploy_tree_with_no_repo_map(cli: CliRunner, cfg, tmp_path: Path) -> None:
    """No map means the build produced no feed. Staging it would render nothing."""
    with (
        _patch_cfg(cfg),
        _tools_present(),
        mock.patch("bakar.commands.feed.feed_mod.meta_avocado_scripts", return_value=_real_scripts(tmp_path)),
    ):
        result = cli.invoke(app, ["feed", "sync", str(_yaml(tmp_path))])

    assert result.exit_code == 1
    assert "no RPM deploy directory" in result.output
    assert "nothing was staged" in result.output


def test_sync_stages_from_the_rpm_deploy_dir(cli: CliRunner, cfg, tmp_path: Path) -> None:
    """``avocado-repo.map`` lives in ``deploy/rpm``, not in ``deploy``."""
    deploy = _with_repo_map(cfg)

    with (
        _patch_cfg(cfg),
        _tools_present(),
        mock.patch("bakar.commands.feed.feed_mod.meta_avocado_scripts", return_value=_real_scripts(tmp_path)),
        mock.patch("bakar.commands.feed.feed_mod.sync") as synced,
    ):
        synced.return_value = {
            "snapshot": "20260823T000000Z",
            "channel_root": cfg.effective_feed_dir / "2024" / "edge",
            "repos": ["target/qemux86-64"],
            "declared": ["target/qemux86-64"],
            "unstaged": [],
            "machines": ["qemux86-64"],
            "pointers": [Path("target/qemux86-64/snapshots-latest.json")],
        }
        result = cli.invoke(app, ["feed", "sync", str(_yaml(tmp_path))])

    assert result.exit_code == 0
    assert synced.call_args.kwargs["deploy_dir"] == deploy
    assert "20260823T000000Z" in result.output


def test_sync_reports_declared_but_unbuilt_repos_without_failing(cli: CliRunner, cfg, tmp_path: Path) -> None:
    _with_repo_map(cfg)

    with (
        _patch_cfg(cfg),
        _tools_present(),
        mock.patch("bakar.commands.feed.feed_mod.meta_avocado_scripts", return_value=_real_scripts(tmp_path)),
        mock.patch("bakar.commands.feed.feed_mod.sync") as synced,
    ):
        synced.return_value = {
            "snapshot": "20260823T000000Z",
            "channel_root": cfg.effective_feed_dir / "2024" / "edge",
            "repos": [],
            "declared": ["sdk/imx93-frdm"],
            "unstaged": ["sdk/imx93-frdm"],
            "machines": [],
            "pointers": [],
        }
        result = cli.invoke(app, ["feed", "sync", str(_yaml(tmp_path))])

    assert result.exit_code == 0
    assert "declared but not built" in result.output
    assert "sdk/imx93-frdm" in result.output


def test_sync_names_a_missing_scripts_checkout_instead_of_raising(cli: CliRunner, cfg, tmp_path: Path) -> None:
    """meta_avocado_scripts raises by design; cli.py catches no such thing."""
    _with_repo_map(cfg)

    with (
        _patch_cfg(cfg),
        _tools_present(),
        mock.patch(
            "bakar.commands.feed.feed_mod.meta_avocado_scripts",
            side_effect=FileNotFoundError("meta-avocado scripts not found at /nowhere"),
        ),
    ):
        result = cli.invoke(app, ["feed", "sync", str(_yaml(tmp_path))])

    assert result.exit_code == 1
    assert "no meta-avocado checkout" in result.output
    assert "Traceback" not in result.output


def test_sync_reports_a_failed_render_script_rather_than_a_traceback(cli: CliRunner, cfg, tmp_path: Path) -> None:
    """The render scripts run with check=True, so a non-zero exit raises."""
    _with_repo_map(cfg)

    with (
        _patch_cfg(cfg),
        _tools_present(),
        mock.patch("bakar.commands.feed.feed_mod.meta_avocado_scripts", return_value=_real_scripts(tmp_path)),
        mock.patch(
            "bakar.commands.feed.feed_mod.sync",
            side_effect=subprocess.CalledProcessError(2, ["/scripts/render-pool-local.py", "--staged"]),
        ),
    ):
        result = cli.invoke(app, ["feed", "sync", str(_yaml(tmp_path))])

    assert result.exit_code == 1
    assert "render-pool-local.py exited 2" in result.output
    assert "still serves the previous snapshot" in result.output


# --- index -----------------------------------------------------------------


def test_index_writes_targets_json_for_the_named_channel(cli: CliRunner, cfg, feed_root: Path) -> None:
    with _patch_cfg(cfg):
        result = cli.invoke(app, ["feed", "index", "--release", "2024", "--channel", "edge"])

    assert result.exit_code == 0
    written = feed_root / "2024" / "edge" / "targets.json"
    assert written.is_file()
    assert written.read_text().startswith("{")


def test_index_refuses_a_channel_that_does_not_exist(cli: CliRunner, cfg, feed_root: Path) -> None:
    """Writing the index would mkdir it, turning a typo into a new empty channel."""
    with _patch_cfg(cfg):
        result = cli.invoke(app, ["feed", "index", "--release", "2024", "--channel", "edg"])

    assert result.exit_code == 1
    assert "no such channel" in result.output
    assert not (feed_root / "2024" / "edg").exists()


# --- serve / stop / status -------------------------------------------------


def test_status_answers_on_a_never_synced_feed(cli: CliRunner, cfg) -> None:
    with _patch_cfg(cfg):
        result = cli.invoke(app, ["feed", "status"])

    assert result.exit_code == 0
    assert "packages: 0" in result.output
    assert "snapshot: (none)" in result.output


def test_serve_does_not_start_a_second_server(cli: CliRunner, cfg) -> None:
    with (
        _patch_cfg(cfg),
        mock.patch("bakar.commands.feed.feed_serve.is_serving", return_value=True),
        mock.patch("bakar.commands.feed.feed_serve.recorded_port", return_value=9001),
        mock.patch("bakar.commands.feed.feed_serve.start_serving") as started,
    ):
        result = cli.invoke(app, ["feed", "serve"])

    assert result.exit_code == 0
    assert "already serving" in result.output
    assert "9001" in result.output
    started.assert_not_called()


def test_serve_reports_the_url_pid_and_bind(cli: CliRunner, cfg) -> None:
    with (
        _patch_cfg(cfg),
        mock.patch("bakar.commands.feed.feed_serve.is_serving", return_value=False),
        mock.patch("bakar.commands.feed.feed_serve.start_serving", return_value=4242) as started,
    ):
        result = cli.invoke(app, ["feed", "serve", "--port", "9001"])

    assert result.exit_code == 0
    assert "9001" in result.output
    assert "4242" in result.output
    assert started.call_args.kwargs["port"] == 9001
    assert started.call_args.kwargs["bind"] == "127.0.0.1"


def test_serve_passes_an_explicit_bind_through(cli: CliRunner, cfg) -> None:
    with (
        _patch_cfg(cfg),
        mock.patch("bakar.commands.feed.feed_serve.is_serving", return_value=False),
        mock.patch("bakar.commands.feed.feed_serve.start_serving", return_value=7) as started,
    ):
        result = cli.invoke(app, ["feed", "serve", "--bind", "0.0.0.0"])

    assert result.exit_code == 0
    assert started.call_args.kwargs["bind"] == "0.0.0.0"


def test_serve_fails_when_the_server_did_not_come_up(cli: CliRunner, cfg) -> None:
    """A start that could not bind must not be reported as serving."""
    with (
        _patch_cfg(cfg),
        mock.patch("bakar.commands.feed.feed_serve.is_serving", return_value=False),
        mock.patch("bakar.commands.feed.feed_serve.start_serving", return_value=None),
    ):
        result = cli.invoke(app, ["feed", "serve"])

    assert result.exit_code == 1
    assert "failed to serve" in result.output


def test_stop_distinguishes_stopped_from_not_running(cli: CliRunner, cfg) -> None:
    with _patch_cfg(cfg), mock.patch("bakar.commands.feed.feed_serve.stop_serving", return_value=False):
        result = cli.invoke(app, ["feed", "stop"])

    assert result.exit_code == 0
    assert "not running" in result.output


# --- gc --------------------------------------------------------------------


def _plan(cfg, **kwargs) -> feed_retention.RetentionPlan:
    return feed_retention.RetentionPlan(channel_root=cfg.effective_feed_dir / "2024" / "edge", **kwargs)


def _gc_args() -> list[str]:
    return ["feed", "gc", "--release", "2024", "--channel", "edge"]


def test_gc_refuses_a_channel_that_does_not_exist(cli: CliRunner, cfg) -> None:
    """Without a kas YAML the feed root comes from a CWD walk, so it can be the
    wrong one - and an absent channel is what that looks like. Planning it would
    report a clean no-op for a tree the operator never meant."""
    with _patch_cfg(cfg), mock.patch("bakar.commands.feed.feed_retention.plan_retention") as planned:
        result = cli.invoke(app, ["feed", "gc", "--release", "2024", "--channel", "nope"])

    assert result.exit_code == 1
    assert "no such channel" in result.output
    assert "Nothing was examined" in result.output
    planned.assert_not_called()


def test_gc_previews_by_default_and_deletes_nothing(cli: CliRunner, cfg) -> None:
    """The load-bearing default: no --confirm must not reach a deletion."""
    plan = _plan(
        cfg,
        kept_snapshots=["20260823T000000Z"],
        removed_snapshots=["20260801T000000Z"],
        pinned="20260823T000000Z",
        stale_metadata=[Path("stale-primary.xml.gz")],
    )

    with (
        _patch_cfg(cfg),
        mock.patch("bakar.commands.feed.feed_retention.plan_retention", return_value=plan),
        mock.patch(
            "bakar.commands.feed.feed_retention.apply_retention",
            wraps=feed_retention.apply_retention,
        ) as applied,
    ):
        result = cli.invoke(app, _gc_args())

    assert result.exit_code == 0
    assert applied.call_args.kwargs["confirm"] is False
    assert "would remove" in result.output
    assert "pass --confirm to remove" in result.output


def test_gc_hands_apply_the_feed_root_and_channel_it_resolved(cli: CliRunner, cfg, feed_root: Path) -> None:
    """Plumbing the wrong root makes the post-run audit look at nothing."""
    plan = _plan(cfg, removed_snapshots=["20260801T000000Z"])

    with (
        _patch_cfg(cfg),
        mock.patch("bakar.commands.feed.feed_retention.plan_retention", return_value=plan) as planned,
        mock.patch("bakar.commands.feed.feed_retention.apply_retention") as applied,
    ):
        applied.return_value = feed_retention.RetentionResult(applied=True)
        result = cli.invoke(app, [*_gc_args(), "--confirm", "--keep", "5"])

    assert result.exit_code == 0
    assert planned.call_args.args[0] == feed_root / "2024" / "edge"
    assert planned.call_args.kwargs["feed_root"] == feed_root
    assert planned.call_args.kwargs["keep"] == 5
    assert applied.call_args.kwargs["feed_root"] == feed_root


def test_gc_passes_confirm_through(cli: CliRunner, cfg) -> None:
    plan = _plan(cfg, removed_snapshots=["20260801T000000Z"])

    with (
        _patch_cfg(cfg),
        mock.patch("bakar.commands.feed.feed_retention.plan_retention", return_value=plan),
        mock.patch("bakar.commands.feed.feed_retention.apply_retention") as applied,
    ):
        applied.return_value = feed_retention.RetentionResult(applied=True, removed_snapshots=["20260801T000000Z"])
        result = cli.invoke(app, [*_gc_args(), "--confirm"])

    assert result.exit_code == 0
    assert applied.call_args.kwargs["confirm"] is True
    assert "removed snapshots" in result.output


def test_gc_says_so_when_there_is_nothing_to_do(cli: CliRunner, cfg) -> None:
    """A no-op must be distinguishable from a run that did not look."""
    plan = _plan(cfg, kept_snapshots=["20260823T000000Z"])

    with (
        _patch_cfg(cfg),
        mock.patch("bakar.commands.feed.feed_retention.plan_retention", return_value=plan),
        mock.patch("bakar.commands.feed.feed_retention.apply_retention") as applied,
    ):
        result = cli.invoke(app, _gc_args())

    assert result.exit_code == 0
    assert "nothing to reclaim" in result.output
    applied.assert_not_called()


def test_gc_warns_when_the_pin_is_unknown(cli: CliRunner, cfg, feed_root: Path) -> None:
    """An existing-but-unparseable pointer is not the same as no pointer."""
    channel = feed_root / "2024" / "edge"
    pointer = channel / "target" / "qemux86-64" / "snapshots-latest.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("{ not json")
    plan = _plan(cfg, kept_snapshots=["20260823T000000Z"], unreadable_pointers=[pointer])

    with (
        _patch_cfg(cfg),
        mock.patch("bakar.commands.feed.feed_retention.plan_retention", return_value=plan),
    ):
        result = cli.invoke(app, _gc_args())

    assert result.exit_code == 0
    assert "name no snapshot" in result.output
    assert "no snapshot will be removed" in result.output


def test_gc_reports_suppressed_pool_reclaim(cli: CliRunner, cfg) -> None:
    """An unreadable package list means no pool entry may be reclaimed.

    Silently reclaiming nothing would read as "the pool is fully referenced".
    """
    plan = _plan(
        cfg,
        kept_snapshots=["20260823T000000Z"],
        removed_snapshots=["20260801T000000Z"],
        unreadable_primaries=[Path("broken-primary.xml.gz")],
    )

    with (
        _patch_cfg(cfg),
        mock.patch("bakar.commands.feed.feed_retention.plan_retention", return_value=plan),
        mock.patch("bakar.commands.feed.feed_retention.apply_retention") as applied,
    ):
        applied.return_value = feed_retention.RetentionResult(applied=False)
        result = cli.invoke(app, _gc_args())

    assert result.exit_code == 0
    assert "SUPPRESSED" in result.output
    assert "broken-primary.xml.gz" in result.output


def test_gc_reports_a_foreign_snapshot_entry(cli: CliRunner, cfg) -> None:
    plan = _plan(
        cfg,
        kept_snapshots=["20260823T000000Z"],
        removed_snapshots=["20260801T000000Z"],
        foreign_snapshot_entries=["keep-me"],
    )

    with (
        _patch_cfg(cfg),
        mock.patch("bakar.commands.feed.feed_retention.plan_retention", return_value=plan),
        mock.patch("bakar.commands.feed.feed_retention.apply_retention") as applied,
    ):
        applied.return_value = feed_retention.RetentionResult(applied=False)
        result = cli.invoke(app, _gc_args())

    assert result.exit_code == 0
    assert "keep-me" in result.output
    assert "not counted against --keep" in result.output


def test_gc_fails_when_a_run_leaves_a_dangling_reference(cli: CliRunner, cfg) -> None:
    """The audit is the run's own gate; a dangling reference must be an error."""
    plan = _plan(cfg, removed_snapshots=["20260801T000000Z"])

    with (
        _patch_cfg(cfg),
        mock.patch("bakar.commands.feed.feed_retention.plan_retention", return_value=plan),
        mock.patch("bakar.commands.feed.feed_retention.apply_retention") as applied,
    ):
        applied.return_value = feed_retention.RetentionResult(
            applied=True,
            dangling=[(Path("repo/repodata/repomd.xml"), Path("_pkgs/aa/aaa.rpm"))],
        )
        result = cli.invoke(app, [*_gc_args(), "--confirm"])

    assert result.exit_code == 1
    assert "dangling" in result.output


def test_gc_fails_when_the_audit_could_not_verify_a_repository(cli: CliRunner, cfg) -> None:
    """Nothing dangling is not a pass when the audit could not read a primary."""
    plan = _plan(cfg, removed_snapshots=["20260801T000000Z"])

    with (
        _patch_cfg(cfg),
        mock.patch("bakar.commands.feed.feed_retention.plan_retention", return_value=plan),
        mock.patch("bakar.commands.feed.feed_retention.apply_retention") as applied,
    ):
        applied.return_value = feed_retention.RetentionResult(
            applied=True,
            dangling=[],
            unreadable_primaries=[Path("repo/repodata/broken-primary.xml.gz")],
        )
        result = cli.invoke(app, [*_gc_args(), "--confirm"])

    assert result.exit_code == 1
    assert "unverifiable" in result.output


def test_gc_reports_a_snapshot_it_failed_to_remove(cli: CliRunner, cfg) -> None:
    plan = _plan(cfg, removed_snapshots=["20260801T000000Z"])

    with (
        _patch_cfg(cfg),
        mock.patch("bakar.commands.feed.feed_retention.plan_retention", return_value=plan),
        mock.patch("bakar.commands.feed.feed_retention.apply_retention") as applied,
    ):
        applied.return_value = feed_retention.RetentionResult(
            applied=True,
            removed_snapshots=[],
            failed_snapshots=["20260801T000000Z"],
        )
        result = cli.invoke(app, [*_gc_args(), "--confirm"])

    assert result.exit_code == 0
    assert "FAILED to remove" in result.output


# --- doctor ----------------------------------------------------------------


def test_doctor_is_registered_and_runs_without_a_kas_yaml(cli: CliRunner, cfg) -> None:
    """A first-time user needs "can this machine do it" before anything else.

    Tools are stubbed present: this asserts the verb is wired and reports, not
    that whatever host runs the suite happens to have createrepo_c installed.
    Without the stub the test passes on a dev box and fails on CI, which is a
    statement about the runner rather than about bakar.
    """
    with _patch_cfg(cfg), _tools_present():
        result = cli.invoke(app, ["feed", "doctor"])

    assert result.exit_code == 0
    assert "createrepo_c" in result.output
    assert "host prerequisites met" in result.output


def test_doctor_exits_nonzero_when_a_prerequisite_is_missing(cli: CliRunner, cfg) -> None:
    with (
        _patch_cfg(cfg),
        mock.patch("bakar.feed_preflight.shutil.which", return_value=None),
    ):
        result = cli.invoke(app, ["feed", "doctor"])

    assert result.exit_code == 1
    assert "FAIL createrepo_c" in result.output


def test_sync_runs_preflight_and_stages_nothing_when_it_blocks(cli: CliRunner, cfg, tmp_path: Path) -> None:
    """The whole point: a missing native binary must not surface as a traceback
    from inside a half-finished stage."""
    _with_repo_map(cfg)

    with (
        _patch_cfg(cfg),
        mock.patch("bakar.commands.feed.feed_mod.meta_avocado_scripts", return_value=tmp_path / "scripts"),
        mock.patch("bakar.feed_preflight.shutil.which", return_value=None),
        mock.patch("bakar.commands.feed.feed_mod.sync") as synced,
    ):
        result = cli.invoke(app, ["feed", "sync", str(_yaml(tmp_path))])

    assert result.exit_code == 1
    assert "prerequisite(s) missing; nothing was staged" in result.output
    synced.assert_not_called()


def test_doctor_checks_host_tools_when_no_workspace_resolves(cli: CliRunner) -> None:
    """The first command after `pip install bakar`, run from anywhere.

    Workspace detection failing must not hide the answer to "can this machine
    build a feed at all" - that question is about host tools and has an answer
    before any workspace exists. Reporting the workspace error instead sends a
    first-time user after the wrong problem.
    """
    with (
        mock.patch("bakar.commands.feed._resolve_cfg", side_effect=typer.Exit(code=2)),
        _tools_present(),
    ):
        result = cli.invoke(app, ["feed", "doctor"])

    assert result.exit_code == 0
    assert "host prerequisites only" in result.output
    assert "createrepo_c" in result.output
    assert "run from a workspace" in result.output


def test_doctor_still_blocks_on_a_missing_tool_with_no_workspace(cli: CliRunner) -> None:
    """The host tier must keep its teeth when the workspace tier is skipped."""
    with (
        mock.patch("bakar.commands.feed._resolve_cfg", side_effect=typer.Exit(code=2)),
        mock.patch("bakar.feed_preflight.shutil.which", return_value=None),
    ):
        result = cli.invoke(app, ["feed", "doctor"])

    assert result.exit_code == 1
    assert "FAIL createrepo_c" in result.output


def test_preflight_omits_the_root_checks_when_no_roots_are_given() -> None:
    from bakar import feed_preflight

    names = {r.name for r in feed_preflight.preflight()}

    assert "createrepo_c" in names
    assert "feed root" not in names
    assert "stage root" not in names
