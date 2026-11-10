"""Tests for defaulting ``bakar feed`` release/channel from the build's codename.

A build records ``DISTRO_CODENAME`` (``release/channel``) in its testdata, and
that is the pair a client's ``$releasever`` expands to. ``sync`` and ``index``
(given a kas file) default to it; an explicit flag is used as given and the
preflight cross-check stays real here, because whether a disagreeing pair is
still blocked is the property this change must not lose. Staging, rendering and
the host-tool lookups are stubbed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from typer.testing import CliRunner

from bakar import feed as feed_mod
from bakar import feed_ops
from bakar.commands import app

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

_WRYNOSE = "2026-wrynose/local"


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


@pytest.fixture
def feed_root(tmp_path: Path) -> Path:
    root = tmp_path / "feed"
    (root / feed_mod.DEFAULT_RELEASE / feed_mod.DEFAULT_CHANNEL).mkdir(parents=True)
    return root


@pytest.fixture
def cfg(tmp_path: Path, feed_root: Path):
    """A config stub exposing only what the feed commands read."""
    return mock.Mock(
        effective_feed_dir=feed_root,
        resolved_tmpdir=tmp_path / "build" / "tmp",
        workspace=tmp_path,
    )


def _build(cfg, testdata: dict | str | None) -> Path:
    """Lay out a finished build: the repo map, and a testdata file when given.

    ``testdata`` as a string is written verbatim, so a test can record a file
    that does not parse.
    """
    deploy = cfg.resolved_tmpdir / "deploy" / "rpm"
    deploy.mkdir(parents=True)
    (deploy / "avocado-repo.map").write_text("repo=target/qemux86-64\n")
    if testdata is not None:
        images = cfg.resolved_tmpdir / "deploy" / "images" / "qemux86-64"
        images.mkdir(parents=True)
        body = testdata if isinstance(testdata, str) else json.dumps(testdata)
        (images / "core-image-minimal-qemux86-64.testdata.json").write_text(body)
    return deploy


def _codename(value: object) -> dict:
    return {"DISTRO_CODENAME": value, "MACHINE": "qemux86-64"}


def _yaml(tmp_path: Path) -> Path:
    path = tmp_path / "machine.yml"
    path.write_text("header: {}\n")
    return path


def _scripts(tmp_path: Path) -> Path:
    directory = tmp_path / "meta-avocado" / "scripts"
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("render-pool-local.py", "repo-stage-rpms.sh"):
        path = directory / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
    return directory


def _sync_result(cfg) -> dict:
    return {
        "snapshot": "20261110T000000Z",
        "channel_root": cfg.effective_feed_dir / "x",
        "repos": ["target/qemux86-64"],
        "declared": ["target/qemux86-64"],
        "unstaged": [],
        "machines": ["qemux86-64"],
        "pointers": [],
    }


def _run_sync(cli: CliRunner, cfg, tmp_path: Path, *flags: str):
    """Invoke ``feed sync`` with staging stubbed; return (result, sync mock)."""
    with (
        mock.patch("bakar.commands.feed._resolve_daemon_cfg", return_value=cfg),
        mock.patch("bakar.feed_preflight.shutil.which", side_effect=lambda tool: f"/usr/bin/{tool}"),
        mock.patch("bakar.commands.feed.feed_mod.meta_avocado_scripts", return_value=_scripts(tmp_path)),
        mock.patch("bakar.commands.feed.feed_mod.sync") as synced,
    ):
        synced.return_value = _sync_result(cfg)
        result = cli.invoke(app, ["feed", "sync", str(_yaml(tmp_path)), *flags])
    return result, synced


def _published(synced) -> tuple[str, str]:
    publish = synced.call_args.kwargs["publish"]
    return publish.release, publish.channel


# --- sync: defaulting from the build ----------------------------------------


def test_sync_defaults_from_build_codename_with_no_flags(cli: CliRunner, cfg, tmp_path: Path) -> None:
    _build(cfg, _codename(_WRYNOSE))

    result, synced = _run_sync(cli, cfg, tmp_path)

    assert result.exit_code == 0, result.output
    assert _published(synced) == ("2026-wrynose", "local")


def test_sync_defaults_from_build_codename_channel_when_only_release_given(cli: CliRunner, cfg, tmp_path: Path) -> None:
    _build(cfg, _codename(_WRYNOSE))

    result, synced = _run_sync(cli, cfg, tmp_path, "--release", "2026-wrynose")

    assert result.exit_code == 0, result.output
    assert _published(synced) == ("2026-wrynose", "local")


def test_sync_defaults_from_build_codename_release_when_only_channel_given(cli: CliRunner, cfg, tmp_path: Path) -> None:
    _build(cfg, _codename(_WRYNOSE))

    result, synced = _run_sync(cli, cfg, tmp_path, "--channel", "local")

    assert result.exit_code == 0, result.output
    assert _published(synced) == ("2026-wrynose", "local")


def test_sync_partial_flag_that_disagrees_is_still_blocked(cli: CliRunner, cfg, tmp_path: Path) -> None:
    """Defaulting the other half must not launder a wrong half past the preflight."""
    _build(cfg, _codename(_WRYNOSE))

    result, synced = _run_sync(cli, cfg, tmp_path, "--channel", "edge")

    assert result.exit_code == 1
    synced.assert_not_called()
    assert "pass --release 2026-wrynose --channel local" in result.output


# --- sync: explicit flags ----------------------------------------------------


def test_sync_explicit_flags_override_and_match_the_build_proceeds(cli: CliRunner, cfg, tmp_path: Path) -> None:
    _build(cfg, _codename(_WRYNOSE))

    result, synced = _run_sync(cli, cfg, tmp_path, "--release", "2026-wrynose", "--channel", "local")

    assert result.exit_code == 0, result.output
    assert _published(synced) == ("2026-wrynose", "local")
    assert "declares no usable" not in result.output


def test_sync_explicit_flags_override_the_fallback_defaults(cli: CliRunner, cfg, tmp_path: Path) -> None:
    """With no codename to check against, an explicit pair is used exactly as given."""
    _build(cfg, _codename(None))

    result, synced = _run_sync(cli, cfg, tmp_path, "--release", "dev", "--channel", "local")

    assert result.exit_code == 0, result.output
    assert _published(synced) == ("dev", "local")


def test_sync_explicit_pair_mismatch_still_blocks(cli: CliRunner, cfg, tmp_path: Path) -> None:
    _build(cfg, _codename(_WRYNOSE))

    result, synced = _run_sync(cli, cfg, tmp_path, "--release", "2024", "--channel", "edge")

    assert result.exit_code == 1
    synced.assert_not_called()
    assert "nothing was staged" in result.output
    assert "pass --release 2026-wrynose --channel local" in result.output


# --- sync: no usable codename -----------------------------------------------


def test_sync_no_codename_keeps_defaults(cli: CliRunner, cfg, tmp_path: Path) -> None:
    _build(cfg, {"MACHINE": "qemux86-64"})

    result, synced = _run_sync(cli, cfg, tmp_path)

    assert result.exit_code == 0, result.output
    assert _published(synced) == (feed_mod.DEFAULT_RELEASE, feed_mod.DEFAULT_CHANNEL)
    assert result.output.count("build declares no usable codename") == 1


def test_sync_no_codename_keeps_defaults_when_no_testdata_was_recorded(cli: CliRunner, cfg, tmp_path: Path) -> None:
    _build(cfg, None)

    result, synced = _run_sync(cli, cfg, tmp_path)

    assert result.exit_code == 0, result.output
    assert _published(synced) == (feed_mod.DEFAULT_RELEASE, feed_mod.DEFAULT_CHANNEL)
    assert "build declares no usable codename" in result.output


def test_sync_no_codename_keeps_defaults_for_a_separatorless_codename(cli: CliRunner, cfg, tmp_path: Path) -> None:
    _build(cfg, _codename("wrynose"))

    result, synced = _run_sync(cli, cfg, tmp_path)

    assert result.exit_code == 0, result.output
    assert _published(synced) == (feed_mod.DEFAULT_RELEASE, feed_mod.DEFAULT_CHANNEL)
    assert "build declares no usable codename" in result.output


def test_sync_no_codename_keeps_defaults_when_testdata_is_unreadable(cli: CliRunner, cfg, tmp_path: Path) -> None:
    _build(cfg, "{ not json")

    result, synced = _run_sync(cli, cfg, tmp_path)

    assert result.exit_code == 0, result.output
    assert _published(synced) == (feed_mod.DEFAULT_RELEASE, feed_mod.DEFAULT_CHANNEL)
    assert "build declares no usable codename" in result.output


# --- the feed_ops reader ------------------------------------------------------


def test_declared_release_channel_reads_the_build_codename(cfg) -> None:
    _build(cfg, _codename(_WRYNOSE))

    assert feed_ops.declared_release_channel(cfg) == ("2026-wrynose", "local")


@pytest.mark.parametrize(
    "testdata",
    [None, {"MACHINE": "qemux86-64"}, _codename("wrynose"), _codename(""), _codename(42), "{ not json", "[]"],
    ids=["no-testdata", "no-key", "no-separator", "empty", "not-a-string", "malformed", "not-a-mapping"],
)
def test_declared_release_channel_no_codename_returns_none(cfg, testdata) -> None:
    _build(cfg, testdata)

    assert feed_ops.declared_release_channel(cfg) == (None, None)


def test_declared_release_channel_needs_no_network(cfg) -> None:
    _build(cfg, _codename(_WRYNOSE))

    with mock.patch("socket.socket", side_effect=AssertionError("network touched")):
        assert feed_ops.declared_release_channel(cfg) == ("2026-wrynose", "local")


def test_declared_release_channel_leaves_build_and_feed_unchanged(cfg, feed_root: Path) -> None:
    _build(cfg, "{ not json")
    roots = (cfg.resolved_tmpdir, feed_root)
    before = {p: (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None) for r in roots for p in r.rglob("*")}

    assert feed_ops.declared_release_channel(cfg) == (None, None)

    after = {p: (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None) for r in roots for p in r.rglob("*")}
    assert after == before


# --- index -------------------------------------------------------------------


def _run_index(cli: CliRunner, cfg, *args: str):
    with mock.patch("bakar.commands.feed._resolve_daemon_cfg", return_value=cfg):
        return cli.invoke(app, ["feed", "index", *args])


def test_index_with_kas_file_defaults_from_build_codename(cli: CliRunner, cfg, feed_root: Path, tmp_path) -> None:
    _build(cfg, _codename(_WRYNOSE))
    (feed_root / "2026-wrynose" / "local").mkdir(parents=True)

    result = _run_index(cli, cfg, str(_yaml(tmp_path)))

    assert result.exit_code == 0, result.output
    assert (feed_root / "2026-wrynose" / "local" / "targets.json").is_file()
    assert not (feed_root / "2024" / "edge" / "targets.json").exists()


def test_index_with_kas_file_no_codename_keeps_defaults(cli: CliRunner, cfg, feed_root: Path, tmp_path) -> None:
    _build(cfg, _codename("wrynose"))

    result = _run_index(cli, cfg, str(_yaml(tmp_path)))

    assert result.exit_code == 0, result.output
    assert (feed_root / "2024" / "edge" / "targets.json").is_file()
    assert "build declares no usable codename" in result.output


def test_index_without_kas_file_keeps_defaults(cli: CliRunner, cfg, feed_root: Path) -> None:
    """No kas file names no build, so a codename lying around is not consulted."""
    _build(cfg, _codename(_WRYNOSE))
    (feed_root / "2026-wrynose" / "local").mkdir(parents=True)

    result = _run_index(cli, cfg)

    assert result.exit_code == 0, result.output
    assert (feed_root / "2024" / "edge" / "targets.json").is_file()
    assert not (feed_root / "2026-wrynose" / "local" / "targets.json").exists()
    assert "declares no usable" not in result.output


# --- help --------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["sync", "index"])
def test_help_states_the_build_declared_default_and_the_fallback(cli: CliRunner, verb: str) -> None:
    result = cli.invoke(app, ["feed", verb, "--help"], env={"COLUMNS": "300", "NO_COLOR": "1"})

    assert result.exit_code == 0
    text = " ".join(result.output.split())
    assert "Default: the build's declared release; 2024 when it declares none" in text
    assert "Default: the build's declared channel; edge when it declares none" in text


# --- the other verbs keep the constants ---------------------------------------


def test_status_keeps_the_constant_defaults(cli: CliRunner, cfg, tmp_path: Path) -> None:
    _build(cfg, _codename(_WRYNOSE))

    with (
        mock.patch("bakar.commands.feed._resolve_daemon_cfg", return_value=cfg),
        mock.patch("bakar.commands.feed.feed_serve.feed_status") as status,
    ):
        status.return_value = {
            "feed_root": cfg.effective_feed_dir,
            "channel_root": "x",
            "targets": [],
            "pool_entries": 0,
            "snapshots": [],
            "serving": False,
            "url": None,
        }
        result = cli.invoke(app, ["feed", "status", str(_yaml(tmp_path))])

    assert result.exit_code == 0, result.output
    assert status.call_args.kwargs["release"] == feed_mod.DEFAULT_RELEASE
    assert status.call_args.kwargs["channel"] == feed_mod.DEFAULT_CHANNEL


def test_gc_keeps_the_constant_defaults(cli: CliRunner, cfg, feed_root: Path, tmp_path: Path) -> None:
    _build(cfg, _codename(_WRYNOSE))
    (feed_root / "2026-wrynose" / "local").mkdir(parents=True)

    with (
        mock.patch("bakar.commands.feed._resolve_daemon_cfg", return_value=cfg),
        mock.patch("bakar.commands.feed.feed_retention.plan_retention") as plan,
    ):
        plan.return_value = mock.Mock(
            unreadable_pointers=[],
            foreign_snapshot_entries=[],
            pool_reclaim_suppressed=False,
            is_empty=True,
            kept_snapshots=[],
        )
        result = cli.invoke(app, ["feed", "gc", str(_yaml(tmp_path))])

    assert result.exit_code == 0, result.output
    assert plan.call_args.args[0] == feed_root / "2024" / "edge"


def test_mirror_keeps_the_constant_defaults(cli: CliRunner, cfg) -> None:
    _build(cfg, _codename(_WRYNOSE))

    with (
        mock.patch("bakar.commands.feed._resolve_daemon_cfg", return_value=cfg),
        mock.patch("bakar.commands.feed.feed_mirror.mirror", side_effect=OSError("stubbed")) as mirrored,
    ):
        cli.invoke(app, ["feed", "mirror", "http://example.invalid/feed", "--repo", "sdk/all"])

    request = mirrored.call_args.args[0]
    assert (request.release, request.channel) == (feed_mod.DEFAULT_RELEASE, feed_mod.DEFAULT_CHANNEL)
