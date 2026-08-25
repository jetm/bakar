"""``bakar build --feed``: sync then index, on a real build's success only.

The flag is wired into ``_finish_build`` rather than into each caller. That
function already raises on a non-zero rc before reaching its success path, so
"only on success" is a property of where the call sits rather than a guard every
future call site has to remember to write.

rc alone is not sufficient, though, and that is the subtle half: ``run_build``
returns 0 after printing a dry-run preview, so "rc == 0" reads a dry run as a
success. The dry-run filter therefore lives at request-resolution time, and
``test_dry_run_does_not_touch_the_feed`` is what holds it there.

Order is asserted through a shared call log rather than two independent mocks.
"Both were called" is satisfied by an index that ran against the pre-sync
channel, which is the failure worth catching: it would write a ``targets.json``
describing the previous sync and report success.

The stubs are built with ``create_autospec`` against the real functions rather
than as hand-written lambdas. A hand-written ``fake_sync(cfg, **kwargs)``
swallows any keyword, so renaming a parameter in ``feed.sync`` keeps this file
green while production raises ``TypeError`` on the first real ``--feed`` build.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
import typer

from bakar import feed as feed_mod
from bakar import feed_ops
from bakar.commands import build as build_mod
from bakar.diagnostics import CheckResult, Severity, Status

pytestmark = pytest.mark.unit

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI SGR escapes so help-text assertions survive colored output."""
    return _ANSI_RE.sub("", text)


@pytest.fixture
def cfg(tmp_path: Path) -> SimpleNamespace:
    """The minimum ``_finish_build`` and ``_sync_feed`` read off a BuildConfig."""
    return SimpleNamespace(
        host_mode=True,
        resolved_tmpdir=tmp_path / "tmp",
        machine="qemux86-64",
        workspace=tmp_path,
        kas_yaml=tmp_path / "machine.yml",
        effective_feed_dir=tmp_path / "_feed",
    )


@pytest.fixture
def log_stub(tmp_path: Path) -> SimpleNamespace:
    """The minimum ``_finish_build`` reads off a RunLogger."""
    return SimpleNamespace(run_id="r0", run_dir=tmp_path / "run", start_monotonic=time.monotonic())


@pytest.fixture
def request_stub(cfg) -> build_mod._FeedRequest:
    return build_mod._FeedRequest(kas_yaml=cfg.kas_yaml, release="dev", channel="local")


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch, cfg) -> list[tuple[str, object]]:
    """Record feed work in invocation order without touching a real feed.

    ``feed.sync`` and ``write_targets_index`` are autospec'd, so a signature
    change in either fails here rather than in production. The recorded deploy
    dir is kept so a test can assert the path ``feed_ops`` derived, which is the
    only real computation on this path.
    """
    log: list[tuple[str, object]] = []

    sync_spec = create_autospec(feed_mod.sync, spec_set=True)

    def record_sync(cfg_arg, **kwargs):
        log.append(("sync", kwargs["deploy_dir"]))
        return {
            "snapshot": "20260101T000000Z",
            "machines": ["qemux86-64"],
            "channel_root": cfg_arg.effective_feed_dir / kwargs["release"] / kwargs["channel"],
            "repos": ["target/qemux86-64"],
            "unstaged": [],
        }

    sync_spec.side_effect = record_sync

    index_spec = create_autospec(feed_ops.feed_index.write_targets_index, spec_set=True)

    def record_index(channel_root):
        log.append(("index", channel_root))
        return channel_root / "targets.json"

    index_spec.side_effect = record_index

    monkeypatch.setattr(feed_ops.feed, "sync", sync_spec)
    monkeypatch.setattr(feed_ops.feed_index, "write_targets_index", index_spec)
    monkeypatch.setattr(feed_ops.feed, "meta_avocado_scripts", lambda _yaml: Path("/scripts"))
    return log


def _names(log: list[tuple[str, object]]) -> list[str]:
    return [name for name, _ in log]


def test_failed_build_does_not_touch_the_feed(calls, cfg, log_stub, request_stub) -> None:
    """A partial deploy must never be staged, however the flag was passed."""
    with pytest.raises(typer.Exit) as exc:
        build_mod._finish_build(cfg, log_stub, 1, cfg.machine, feed=request_stub)

    assert exc.value.exit_code == 1
    assert calls == []


def test_successful_build_syncs_then_indexes(calls, cfg, log_stub, request_stub) -> None:
    """Index reads what sync rendered, so it can only run afterwards."""
    build_mod._finish_build(cfg, log_stub, 0, cfg.machine, feed=request_stub)

    assert _names(calls) == ["sync", "index"]


def test_index_targets_the_channel_the_sync_rendered(calls, cfg, log_stub, request_stub) -> None:
    """Recomputing the channel would agree only while both used the defaults.

    ``write_targets_index`` MKDIRS its channel rather than refusing, so a
    non-default release would otherwise leave a second, empty channel beside the
    one the sync just filled - with no error.
    """
    build_mod._finish_build(cfg, log_stub, 0, cfg.machine, feed=request_stub)

    assert dict(calls)["index"] == cfg.effective_feed_dir / "dev" / "local"


def test_sync_stages_the_rpm_deploy_directory(calls, cfg, log_stub, request_stub) -> None:
    """The map that declares which repos to render lives in deploy/rpm.

    Staging one level up finds no map; staging a nonexistent path renders an
    empty repo and still writes a valid targets.json, so a wrong path here is
    success-shaped rather than loud.
    """
    build_mod._finish_build(cfg, log_stub, 0, cfg.machine, feed=request_stub)

    assert dict(calls)["sync"] == cfg.resolved_tmpdir / "deploy" / "rpm"


def test_absent_flag_leaves_the_build_unchanged(calls, cfg, log_stub) -> None:
    """The default path must not acquire a feed side effect."""
    build_mod._finish_build(cfg, log_stub, 0, cfg.machine)

    assert calls == []


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CalledProcessError(1, ["/scripts/render-pool-local.py"]),
        FileNotFoundError("meta-avocado scripts not found"),
        # Neither is an OSError, and both are reachable: parse_repo_map reads the
        # map with no encoding, and a signature drift in the feed layer raises
        # TypeError. A narrow except tuple lets these escape as a traceback after
        # "build succeeded" has already printed.
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        TypeError("sync() got an unexpected keyword argument 'deploy_dir'"),
    ],
)
def test_a_feed_failure_never_fails_the_build(monkeypatch, calls, cfg, log_stub, failure) -> None:
    """The build succeeded and its artifacts are on disk.

    Turning a feed problem into a non-zero exit would discard hours of work over
    a step the user can repeat, so this must return normally - and must not go on
    to index a channel the sync never rendered.
    """
    request = build_mod._FeedRequest(kas_yaml=cfg.kas_yaml, release="dev", channel="local")
    monkeypatch.setattr(feed_ops.feed, "sync", lambda *a, **k: (_ for _ in ()).throw(failure))

    build_mod._finish_build(cfg, log_stub, 0, cfg.machine, feed=request)

    assert _names(calls) == []


def test_dry_run_resolves_no_feed_request(cfg) -> None:
    """run_build returns 0 for a dry run, so rc cannot tell "built" from "never ran".

    Syncing here would stage whatever a previous build left in the deploy tree
    and repin every client onto a fresh snapshot of stale RPMs - from a command
    documented to exit before invoking kas.
    """
    assert build_mod._resolve_feed_request(cfg, feed=True, dry_run=True, release="dev", channel="local") is None


def test_feed_request_carries_the_requested_release_and_channel(monkeypatch, cfg) -> None:
    """--feed-release/--feed-channel must reach the sync, not silently default."""
    monkeypatch.setattr(feed_ops, "preflight_results", lambda *a, **k: [])

    request = build_mod._resolve_feed_request(cfg, feed=True, dry_run=False, release="dev", channel="local")

    assert request == build_mod._FeedRequest(kas_yaml=cfg.kas_yaml, release="dev", channel="local")


def test_blocking_preflight_refuses_before_the_build(monkeypatch, capsys, cfg) -> None:
    """A missing createrepo_c must cost milliseconds to learn, not a whole build."""
    missing = CheckResult(
        name="createrepo_c",
        severity=Severity.BLOCK,
        status=Status.FAIL,
        message="not found on PATH",
        fix_hint="pacman -S createrepo_c",
    )
    monkeypatch.setattr(feed_ops, "preflight_results", lambda *a, **k: [missing])

    with pytest.raises(typer.Exit) as exc:
        build_mod._resolve_feed_request(cfg, feed=True, dry_run=False, release="dev", channel="local")

    assert exc.value.exit_code == 2
    # The failing check and its fix must both reach the user; a bare refusal
    # leaves them to re-run `bakar feed doctor` to learn what this already knew.
    printed = _plain(capsys.readouterr().err)
    assert "createrepo_c" in printed
    assert "pacman -S createrepo_c" in printed


def test_feed_flags_are_discoverable() -> None:
    """A flag absent from --help is a flag nobody finds."""
    from typer.testing import CliRunner

    from bakar.cli import app

    output = _plain(CliRunner().invoke(app, ["build", "--help"]).output)
    assert "--feed" in output
    assert "--feed-release" in output
    assert "--feed-channel" in output


def test_feed_without_a_kas_yaml_is_refused() -> None:
    """Sync stages one build's deploy tree, and the YAML is what names it.

    Exiting 2 beats defaulting to some other form's tree: the wrong tree still
    stages and still renders, so the mistake would surface as a feed quietly
    describing a build the caller did not ask about.
    """
    from typer.testing import CliRunner

    from bakar.cli import app

    result = CliRunner().invoke(app, ["build", "--feed"])
    assert result.exit_code == 2
    # The specific refusal, not merely a mention of the flag: an UNRECOGNISED
    # --feed also exits 2 and also names it, so a substring check on "--feed"
    # passes before the flag exists at all.
    assert "needs a kas YAML" in _plain(result.output)


def test_feed_with_remote_dispatch_is_refused(tmp_path: Path) -> None:
    """The feed would render on the remote, where the next rsync --delete removes it.

    The feed roots are not in RSYNC_EXCLUDES, so a subsequent --on dispatch
    mirrors the local tree over them and takes every retained snapshot with it.
    """
    from typer.testing import CliRunner

    from bakar.cli import app

    yaml = tmp_path / "machine.yml"
    yaml.write_text("header: {}\n")

    result = CliRunner().invoke(app, ["build", str(yaml), "--feed", "--on", "pc2"])
    assert result.exit_code == 2
    assert "cannot be combined with --on" in _plain(result.output)
