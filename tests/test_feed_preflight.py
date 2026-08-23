"""Prerequisite checks for the local feed.

The point of this module is a first run on an unfamiliar machine, so the tests
drive the absent case as hard as the present one - a check that cannot fail is
worse than no check, because it reports readiness that was never established.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

from bakar import feed_preflight
from bakar.diagnostics import Severity, Status

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _by_name(results, name):
    return next(r for r in results if r.name == name)


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "feed", tmp_path / "feed-stage"


@pytest.fixture
def scripts(tmp_path: Path) -> Path:
    directory = tmp_path / "meta-avocado" / "scripts"
    directory.mkdir(parents=True)
    for name in ("render-pool-local.py", "repo-stage-rpms.sh"):
        path = directory / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
    return directory


@pytest.fixture
def deploy(tmp_path: Path) -> Path:
    directory = tmp_path / "build" / "tmp" / "deploy" / "rpm"
    directory.mkdir(parents=True)
    (directory / "avocado-repo.map").write_text("repo=$releasever/sdk/all\n")
    return directory


# --- createrepo_c, the one that actually bites -----------------------------


def test_a_missing_createrepo_blocks(monkeypatch, roots) -> None:
    """The renderer shells out to it with no fallback, so this must block."""
    monkeypatch.setattr(
        feed_preflight.shutil, "which", lambda tool: None if tool == "createrepo_c" else f"/usr/bin/{tool}"
    )

    result = feed_preflight.check_createrepo()

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert feed_preflight.blocking([result]) == [result]


def test_the_createrepo_hint_names_a_package_per_distro(monkeypatch) -> None:
    """The command and the package differ by distro, so the command alone is not
    an actionable hint - and macOS has no package at all."""
    monkeypatch.setattr(feed_preflight.shutil, "which", lambda _tool: None)

    hint = feed_preflight.check_createrepo().fix_hint or ""

    assert "createrepo-c" in hint, "Debian/Ubuntu spells it with a hyphen"
    assert "createrepo_c" in hint
    assert "macOS" in hint


def test_a_present_createrepo_passes(monkeypatch) -> None:
    monkeypatch.setattr(feed_preflight.shutil, "which", lambda _tool: "/usr/bin/createrepo_c")

    assert feed_preflight.check_createrepo().status is Status.PASS


# --- shell tools -----------------------------------------------------------


def test_missing_bash_and_tar_both_block(monkeypatch) -> None:
    monkeypatch.setattr(feed_preflight.shutil, "which", lambda _tool: None)

    results = feed_preflight.check_shell_tools()

    assert {r.name for r in results} == {"bash", "tar"}
    assert all(r.status is Status.FAIL for r in results)
    assert len(feed_preflight.blocking(results)) == 2


def test_stdbuf_is_not_required(monkeypatch) -> None:
    """The staging script guards it with `command -v` and only uses it for output."""
    monkeypatch.setattr(feed_preflight.shutil, "which", lambda tool: None if tool == "stdbuf" else f"/usr/bin/{tool}")

    results = feed_preflight.check_shell_tools()

    assert all(r.status is Status.PASS for r in results)
    assert "stdbuf" not in {r.name for r in results}


# --- the layer checkout ----------------------------------------------------


def test_no_meta_avocado_checkout_reports_once_not_per_script() -> None:
    """An absent checkout is one problem, not two missing files."""
    results = feed_preflight.check_scripts(None)

    assert len(results) == 1
    assert results[0].name == "meta-avocado"
    assert results[0].status is Status.FAIL


def test_a_complete_checkout_passes(scripts: Path) -> None:
    results = feed_preflight.check_scripts(scripts)

    assert [r.status for r in results] == [Status.PASS, Status.PASS]


def test_a_missing_script_is_named(scripts: Path) -> None:
    (scripts / "render-pool-local.py").unlink()

    results = feed_preflight.check_scripts(scripts)

    assert _by_name(results, "render-pool-local.py").status is Status.FAIL
    assert _by_name(results, "repo-stage-rpms.sh").status is Status.PASS


def test_a_non_executable_script_is_caught(scripts: Path) -> None:
    """A checkout from an archive can lose the executable bit.

    The renderer is invoked as a program, not through an interpreter, so a
    readable-but-not-executable script fails with EACCES mid-sync.
    """
    (scripts / "render-pool-local.py").chmod(0o644)

    result = _by_name(feed_preflight.check_scripts(scripts), "render-pool-local.py")

    assert result.status is Status.FAIL
    assert "chmod +x" in (result.fix_hint or "")


# --- build output ----------------------------------------------------------


def test_a_missing_deploy_dir_is_reported_as_no_build(tmp_path: Path) -> None:
    result = feed_preflight.check_build_output(tmp_path / "nope" / "deploy" / "rpm")

    assert result.status is Status.FAIL
    assert "run a build first" in (result.fix_hint or "")


def test_a_deploy_dir_without_a_repo_map_is_a_different_failure(deploy: Path) -> None:
    """Present-but-no-map means the build produced no feed, which is not the
    same as no build at all - so it gets its own message."""
    (deploy / "avocado-repo.map").unlink()

    result = feed_preflight.check_build_output(deploy)

    assert result.status is Status.FAIL
    assert result.name == "avocado-repo.map"


def test_a_complete_build_passes(deploy: Path) -> None:
    assert feed_preflight.check_build_output(deploy).status is Status.PASS


# --- writability -----------------------------------------------------------


def test_writability_is_checked_for_the_stage_root_too(tmp_path: Path) -> None:
    """The stage root is a SIBLING of the feed, so a writable feed says nothing
    about it - and staging fails at the first tar pipe."""
    results = feed_preflight.check_writable(tmp_path / "feed", tmp_path / "feed-stage")

    assert {r.name for r in results} == {"feed root", "stage root"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the write bit")
def test_an_unwritable_root_blocks(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        results = feed_preflight.check_writable(locked / "feed", locked / "feed-stage")
        assert len(feed_preflight.blocking(results)) == 2
    finally:
        locked.chmod(0o700)


def test_writability_walks_up_to_an_existing_parent(tmp_path: Path) -> None:
    """Neither root exists yet on a first run, so the check has to ask about the
    nearest parent that does rather than about a path that is absent."""
    results = feed_preflight.check_writable(tmp_path / "a" / "b" / "c" / "feed", tmp_path / "a" / "b" / "c" / "stage")

    assert all(r.status is Status.PASS for r in results)


# --- platform and composition ---------------------------------------------


def test_a_non_posix_platform_warns_without_blocking(monkeypatch) -> None:
    """Reported, not refused: someone on an unusual setup should be told which
    half works rather than stopped outright."""
    monkeypatch.setattr(feed_preflight.os, "name", "nt")

    result = feed_preflight.check_platform()

    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert feed_preflight.blocking([result]) == []


def test_preflight_without_a_workspace_checks_only_the_host(roots) -> None:
    """`bakar feed doctor` with no kas YAML answers "can this machine do it".

    It must not report a missing checkout or a missing build, because neither was
    named.
    """
    feed_root, stage_root = roots

    names = {r.name for r in feed_preflight.preflight(feed_root=feed_root, stage_root=stage_root)}

    assert "createrepo_c" in names
    assert "meta-avocado" not in names
    assert "avocado-repo.map" not in names


def test_preflight_reports_every_problem_not_just_the_first(monkeypatch, roots) -> None:
    """One round trip per missing prerequisite is the failure mode being avoided."""
    monkeypatch.setattr(feed_preflight.shutil, "which", lambda _tool: None)
    feed_root, stage_root = roots

    results = feed_preflight.preflight(
        feed_root=feed_root,
        stage_root=stage_root,
        scripts=None,
        deploy_dir=feed_root / "no-such-build",
    )

    failed = {r.name for r in results if r.status is Status.FAIL}
    assert {"createrepo_c", "bash", "tar", "meta-avocado", "build output"} <= failed


def test_preflight_on_a_ready_machine_blocks_nothing(monkeypatch, roots, scripts, deploy) -> None:
    monkeypatch.setattr(feed_preflight.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    feed_root, stage_root = roots

    results = feed_preflight.preflight(feed_root=feed_root, stage_root=stage_root, scripts=scripts, deploy_dir=deploy)

    assert feed_preflight.blocking(results) == []


# --- release/channel must match the build ---------------------------------


def _with_codename(deploy: Path, codename: str) -> None:
    """Write testdata where the build records it: deploy/images/<machine>/."""
    images = deploy.parent / "images" / "avocado-qemux86-64"
    images.mkdir(parents=True, exist_ok=True)
    (images / "avocado-image.testdata.json").write_text(json.dumps({"DISTRO_CODENAME": codename}))


def test_a_release_channel_mismatch_blocks(deploy: Path) -> None:
    """DISTRO_CODENAME IS release/channel - it is what dnf expands $releasever
    to, so the wrong pair renders a valid feed where no client looks."""
    _with_codename(deploy, "dev/local")

    result = feed_preflight.check_release_channel(deploy, "2024", "edge")

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert result.fix_hint == "pass --release dev --channel local"


def test_a_matching_release_channel_passes(deploy: Path) -> None:
    _with_codename(deploy, "dev/local")

    assert feed_preflight.check_release_channel(deploy, "dev", "local").status is Status.PASS


def test_no_codename_is_skipped_rather_than_guessed(deploy: Path) -> None:
    """A codename with no separator names a release and no channel; inventing
    `edge` for it would be the same class of guess."""
    _with_codename(deploy, "scarthgap")

    result = feed_preflight.check_release_channel(deploy, "2024", "edge")

    assert result.status is Status.SKIP
    assert result.severity is Severity.INFO


def test_absent_testdata_is_skipped_not_failed(deploy: Path) -> None:
    assert feed_preflight.check_release_channel(deploy, "2024", "edge").status is Status.SKIP


def test_preflight_skips_the_pair_check_when_not_given_one(roots, scripts, deploy, monkeypatch) -> None:
    monkeypatch.setattr(feed_preflight.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    _with_codename(deploy, "dev/local")
    feed_root, stage_root = roots

    without = feed_preflight.preflight(feed_root=feed_root, stage_root=stage_root, scripts=scripts, deploy_dir=deploy)
    with_pair = feed_preflight.preflight(
        feed_root=feed_root,
        stage_root=stage_root,
        scripts=scripts,
        deploy_dir=deploy,
        release="2024",
        channel="edge",
    )

    assert "release/channel" not in {r.name for r in without}
    assert feed_preflight.blocking(without) == []
    assert len(feed_preflight.blocking(with_pair)) == 1
