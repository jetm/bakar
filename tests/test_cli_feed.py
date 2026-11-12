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

_VERBS = ("sync", "refresh", "index", "serve", "stop", "status", "gc")


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
    return mock.patch("bakar.commands.feed._resolve_daemon_cfg", return_value=cfg)


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
    assert synced.call_args.kwargs["publish"].deploy_dir == deploy
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
        mock.patch("bakar.commands.feed._resolve_daemon_cfg", side_effect=typer.Exit(code=2)),
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
        mock.patch("bakar.commands.feed._resolve_daemon_cfg", side_effect=typer.Exit(code=2)),
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


# --- refresh ---------------------------------------------------------------


def _refresh_patches(cfg):
    """Stub everything that builds, resolves a workspace, or binds a socket."""
    return (
        mock.patch("bakar.commands.feed._resolve_refresh_cfg", return_value=cfg),
        mock.patch("bakar.commands.feed._feed_refresh.run_refresh"),
        mock.patch("bakar.commands.feed._feed_refresh_output.preflight_blocks", return_value=False),
        mock.patch("bakar.commands.feed._feed_refresh_output.check_port", return_value=None),
        mock.patch("bakar.commands.feed._feed_refresh_output.present_refresh", return_value=0),
    )


def test_refresh_without_recipe_fails_before_build(cli: CliRunner, cfg, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    resolve, run, preflight, port, present = _refresh_patches(cfg)

    with resolve as resolved, run as ran, preflight as preflighted, port, present:
        result = cli.invoke(app, ["feed", "refresh", str(_yaml(tmp_path)), "--project", str(project)])

    assert result.exit_code == 2
    assert "--recipe" in result.output
    ran.assert_not_called()
    resolved.assert_not_called()
    preflighted.assert_not_called()


def test_refresh_missing_project_dir_fails_before_build(cli: CliRunner, cfg, tmp_path: Path) -> None:
    resolve, run, preflight, port, present = _refresh_patches(cfg)

    with resolve as resolved, run as ran, preflight, port, present:
        absent = tmp_path / "no-such-project"
        result = cli.invoke(
            app, ["feed", "refresh", str(_yaml(tmp_path)), "--recipe", "busybox", "--project", str(absent)]
        )
        unset = cli.invoke(app, ["feed", "refresh", str(_yaml(tmp_path)), "--recipe", "busybox"])

    assert result.exit_code == 2
    assert "not a directory" in result.output
    assert unset.exit_code == 2
    assert "--project" in unset.output
    ran.assert_not_called()
    resolved.assert_not_called()


def test_refresh_accepts_global_container_flag(cli: CliRunner, cfg, tmp_path: Path) -> None:
    """The refresh resolves through ``resolve_global_config`` so ``--container`` reaches kas."""
    project = tmp_path / "proj"
    project.mkdir()
    seen = []

    def spy(request):
        seen.append(request)
        return cfg

    with (
        mock.patch("bakar.commands.feed._normalize_dispatch", return_value=("generic", None, _yaml(tmp_path), None)),
        mock.patch("bakar.commands.feed._resolve_workspace", return_value=tmp_path),
        mock.patch("bakar.commands.feed.resolve_global_config", side_effect=spy),
        mock.patch("bakar.commands.feed.feed_ops.scripts_dir", return_value=None),
        mock.patch("bakar.commands.feed._feed_refresh.run_refresh") as ran,
        mock.patch("bakar.commands.feed._feed_refresh_output.preflight_blocks", return_value=False),
        mock.patch("bakar.commands.feed._feed_refresh_output.check_port", return_value=None),
        mock.patch("bakar.commands.feed._feed_refresh_output.present_refresh", return_value=0),
    ):
        result = cli.invoke(
            app,
            ["--container", "feed", "refresh", str(_yaml(tmp_path)), "--recipe", "busybox", "--project", str(project)],
        )

    assert result.exit_code == 0, result.output
    assert len(seen) == 1
    assert seen[0].workspace == tmp_path
    request = ran.call_args.args[0]
    assert request.recipes == ("busybox",)
    assert request.delta_root.parent == cfg.effective_feed_dir.parent
    assert request.delta_root != cfg.effective_feed_dir
    assert request.work_root != request.delta_root


def _refresh_defaults_request(
    cfg, tmp_path: Path, *flags: str, declared: tuple[str | None, str | None] = ("wrynose", "stable")
):
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    resolve, run, preflight, port, present = _refresh_patches(cfg)
    with (
        resolve,
        run as ran,
        preflight,
        port,
        present,
        mock.patch("bakar.feed_ops.declared_release_channel", return_value=declared),
    ):
        cli = CliRunner()
        result = cli.invoke(
            app,
            ["feed", "refresh", str(_yaml(tmp_path)), "--recipe", "busybox", "--project", str(project), *flags],
        )
    assert result.exit_code == 0, result.output
    return ran.call_args.args[0]


def test_refresh_defaults_release_and_channel_from_the_builds_codename(cfg, tmp_path: Path) -> None:
    request = _refresh_defaults_request(cfg, tmp_path)
    assert (request.release, request.channel) == ("wrynose", "stable")
    assert (request.base_release, request.base_channel) == ("wrynose", "stable")


def test_refresh_explicit_release_wins_and_base_follows_the_resolved_pair(cfg, tmp_path: Path) -> None:
    request = _refresh_defaults_request(cfg, tmp_path, "--release", "2026")
    assert (request.release, request.channel) == ("2026", "stable")
    assert (request.base_release, request.base_channel) == ("2026", "stable")


def test_refresh_explicit_base_pair_is_kept(cfg, tmp_path: Path) -> None:
    request = _refresh_defaults_request(cfg, tmp_path, "--base-release", "2025", "--base-channel", "edge")
    assert (request.release, request.channel) == ("wrynose", "stable")
    assert (request.base_release, request.base_channel) == ("2025", "edge")


def test_refresh_stops_before_build_when_preflight_blocks(cli: CliRunner, cfg, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    resolve, run, _preflight, port, present = _refresh_patches(cfg)

    with (
        resolve,
        run as ran,
        mock.patch("bakar.commands.feed._feed_refresh_output.preflight_blocks", return_value=True),
        port,
        present,
        mock.patch("bakar.commands.feed.feed_ops.scripts_dir", return_value=None),
    ):
        result = cli.invoke(
            app, ["feed", "refresh", str(_yaml(tmp_path)), "--recipe", "busybox", "--project", str(project)]
        )

    assert result.exit_code == 1
    ran.assert_not_called()


def test_refresh_refused_port_exits_before_build(cli: CliRunner, cfg, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    resolve, run, preflight, _port, present = _refresh_patches(cfg)

    with (
        resolve,
        run as ran,
        preflight,
        present,
        mock.patch("bakar.commands.feed.feed_ops.scripts_dir", return_value=None),
        mock.patch("bakar.commands.feed._feed_refresh_output.check_port", return_value="cannot serve: busy"),
    ):
        result = cli.invoke(
            app,
            ["feed", "refresh", str(_yaml(tmp_path)), "--recipe", "busybox", "--project", str(project), "--serve"],
        )

    assert result.exit_code == 1
    assert "cannot serve: busy" in result.output
    ran.assert_not_called()


def test_refresh_failure_is_reported_not_raised(cli: CliRunner, cfg, tmp_path: Path) -> None:
    from bakar.commands._feed_refresh import RefreshFailedError

    project = tmp_path / "proj"
    project.mkdir()
    resolve, run, preflight, port, present = _refresh_patches(cfg)

    with (
        resolve,
        run as ran,
        preflight,
        port,
        present as presented,
        mock.patch("bakar.commands.feed.feed_ops.scripts_dir", return_value=None),
    ):
        ran.side_effect = RefreshFailedError("build failed (exit 1)")
        result = cli.invoke(
            app, ["feed", "refresh", str(_yaml(tmp_path)), "--recipe", "busybox", "--project", str(project)]
        )

    assert result.exit_code == 1
    assert "build failed (exit 1)" in result.output
    presented.assert_not_called()


# --- mirror --metadata-only ------------------------------------------------


def test_mirror_metadata_only_reaches_the_request(cli: CliRunner, cfg) -> None:
    with _patch_cfg(cfg), mock.patch("bakar.commands.feed.feed_mirror.mirror") as mirrored:
        mirrored.return_value = mock.Mock(
            feed_root=cfg.effective_feed_dir,
            source_channel="https://x/2024/edge",
            repos=[],
            unpublished=[],
            packages_downloaded=0,
            bytes_downloaded=0,
            packages_reused=0,
        )
        result = cli.invoke(app, ["feed", "mirror", "https://x", "--repo", "sdk/all", "--metadata-only"])

    assert result.exit_code == 0, result.output
    assert mirrored.call_args.args[0].metadata_only is True


def test_mirror_defaults_to_full_download(cli: CliRunner, cfg) -> None:
    with _patch_cfg(cfg), mock.patch("bakar.commands.feed.feed_mirror.mirror") as mirrored:
        mirrored.return_value = mock.Mock(
            feed_root=cfg.effective_feed_dir,
            source_channel="https://x/2024/edge",
            repos=[],
            unpublished=[],
            packages_downloaded=0,
            bytes_downloaded=0,
            packages_reused=0,
        )
        cli.invoke(app, ["feed", "mirror", "https://x", "--repo", "sdk/all"])

    assert mirrored.call_args.args[0].metadata_only is False


def _refresh_request(cli: CliRunner, cfg, tmp_path: Path, *extra: str):
    """Invoke ``feed refresh`` with everything stubbed and return ``(result, request_passed_to_run_refresh)``."""
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    resolve, run, preflight, port, present = _refresh_patches(cfg)
    args = ["feed", "refresh", str(_yaml(tmp_path)), "--recipe", "busybox", "--project", str(project), *extra]
    with resolve, run as ran, preflight, port, present:
        result = cli.invoke(app, args)
    request = ran.call_args.args[0] if ran.call_args else None
    return result, request


def _write_pointer(feed_root: Path, release: str, channel: str, machine: str, snapshot_id: str) -> None:
    target = feed_root / release / channel / "target" / machine
    target.mkdir(parents=True, exist_ok=True)
    (target / "snapshots-latest.json").write_text(f'{{"id": "{snapshot_id}", "created": "2026-01-01T00:00:00Z"}}\n')


def test_refresh_base_defaults_to_release_and_channel_without_a_pointer(cli: CliRunner, cfg, tmp_path: Path) -> None:
    result, request = _refresh_request(cli, cfg, tmp_path, "--release", "2024", "--channel", "edge")

    assert result.exit_code == 0, result.output
    assert (request.base_release, request.base_channel) == ("2024", "edge")
    assert request.base_snapshot_id is None


def test_refresh_base_options_override_and_snapshot_id_comes_from_the_published_pointer(
    cli: CliRunner, cfg, feed_root: Path, tmp_path: Path
) -> None:
    _write_pointer(feed_root, "2023", "stable", "qemux86-64", "snap-base")
    _write_pointer(feed_root, "2024", "edge", "qemux86-64", "snap-not-the-base")

    result, request = _refresh_request(
        cli,
        cfg,
        tmp_path,
        "--release",
        "2024",
        "--channel",
        "edge",
        "--base-release",
        "2023",
        "--base-channel",
        "stable",
    )

    assert result.exit_code == 0, result.output
    assert (request.release, request.channel) == ("2024", "edge")
    assert (request.base_release, request.base_channel) == ("2023", "stable")
    assert request.base_snapshot_id == "snap-base"


def test_refresh_base_snapshot_prefers_the_cfg_machine_pointer(
    cli: CliRunner, cfg, feed_root: Path, tmp_path: Path
) -> None:
    cfg.machine = "qemux86-64"
    _write_pointer(feed_root, "2024", "edge", "other-machine", "snap-other")
    _write_pointer(feed_root, "2024", "edge", "qemux86-64", "snap-mine")

    _result, request = _refresh_request(cli, cfg, tmp_path, "--release", "2024", "--channel", "edge")

    assert request.base_snapshot_id == "snap-mine"


def test_refresh_base_snapshot_is_none_when_machines_disagree(
    cli: CliRunner, cfg, feed_root: Path, tmp_path: Path
) -> None:
    _write_pointer(feed_root, "2024", "edge", "a", "snap-a")
    _write_pointer(feed_root, "2024", "edge", "b", "snap-b")

    _result, request = _refresh_request(cli, cfg, tmp_path, "--release", "2024", "--channel", "edge")

    assert request.base_snapshot_id is None


def test_refresh_bad_advertise_fails_before_resolving_or_building(cli: CliRunner, cfg, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    resolve, run, preflight, port, present = _refresh_patches(cfg)

    with resolve as resolved, run as ran, preflight as preflighted, port, present:
        result = cli.invoke(
            app,
            [
                "feed",
                "refresh",
                str(_yaml(tmp_path)),
                "--recipe",
                "busybox",
                "--project",
                str(project),
                "--advertise",
                "myhost:9000",
            ],
        )

    assert result.exit_code == 2
    assert "--advertise takes a host only" in result.output
    ran.assert_not_called()
    resolved.assert_not_called()
    preflighted.assert_not_called()


def test_refresh_cfg_equals_the_cfg_bakar_bitbake_resolves(
    meta_avocado_worktree_layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh must read the build directory, tmpdir and layers of the build it runs."""
    from bakar.commands import bitbake as bitbake_cmd
    from bakar.commands.feed import _resolve_refresh_cfg

    layout = meta_avocado_worktree_layout
    monkeypatch.setenv("BAKAR_WORKTREE_BUILD_DIR", "build-{stem}-dispatched")
    for name in ("materialize_cache_classify_layer", "materialize_sccache_layer", "materialize_host_layer"):
        monkeypatch.setattr(bitbake_cmd, name, lambda _cfg: None)

    refresh_cfg = _resolve_refresh_cfg(layout.ws, layout.kas_yaml)
    bitbake_cfg, _overlay, _extra = bitbake_cmd._prepare_run(
        manifest=None, machine=None, workspace=layout.ws, kas_yaml=str(layout.kas_yaml)
    )

    assert refresh_cfg.bsp_root == layout.ws / "build-m-dispatched"
    assert refresh_cfg.bsp_root == bitbake_cfg.bsp_root
    assert refresh_cfg.workspace == bitbake_cfg.workspace
    assert refresh_cfg.resolved_tmpdir == bitbake_cfg.resolved_tmpdir
    assert refresh_cfg.bblayers_conf == bitbake_cfg.bblayers_conf


def test_refresh_base_snapshot_uses_the_target_slug_not_the_machine_name(
    cli: CliRunner, cfg, feed_root: Path, tmp_path: Path
) -> None:
    cfg.machine = "avocado-qemux86-64"
    deploy = cfg.resolved_tmpdir / "deploy" / "rpm"
    deploy.mkdir(parents=True)
    (deploy / "avocado-repo.map").write_text("repo=$releasever/target/qemux86-64\n")
    _write_pointer(feed_root, "2024", "edge", "raspberrypi5", "snap-other")
    _write_pointer(feed_root, "2024", "edge", "qemux86-64", "snap-mine")

    _result, request = _refresh_request(cli, cfg, tmp_path, "--release", "2024", "--channel", "edge")

    assert request.base_snapshot_id == "snap-mine"
