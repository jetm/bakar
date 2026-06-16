"""E2E integration tests that drive bakar through the real installed CLI binary.

Each test invokes ``bakar`` as a subprocess and asserts on stdout, stderr, and
the exit code. No monkey-patching or CliRunner - these tests catch wiring
mistakes that unit tests cannot: wrong stream routing, missing env vars, broken
workspace detection.

Run with:
    uv run pytest tests/test_e2e_cli.py -v
    uv run pytest -m integration

Notes:
    - ``console.print()`` routes to stderr because ``_app.py`` uses
      ``Console(stderr=True)``.  All assertions on human-readable output
      check ``result.stderr``; only ``report --json`` uses ``result.stdout``.
    - The ``for-all`` user command's stdout inherits bakar's stdout
      (subprocess.run with no redirection), so echo output is in
      ``result.stdout`` while bakar headers are in ``result.stderr``.
    - Dump/lock tests unset ``KAS_CONTAINER_IMAGE`` to force host mode; the
      ``file://`` repo paths are not bind-mounted inside ``kas-container``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

_BAKAR = shutil.which("bakar")
if _BAKAR is None:
    pytest.skip("bakar not installed - run: uv tool install .", allow_module_level=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(args: list[str], *, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    """Run bakar and return the CompletedProcess with text streams."""
    return subprocess.run(
        [_BAKAR, *args],
        capture_output=True,
        text=True,
        check=False,
        env=env if env is not None else os.environ.copy(),
    )


def _git_init(path: Path) -> str:
    """Create a minimal git repo with one commit at *path*; return HEAD SHA."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "--initial-branch=main", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "e2e@test.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "E2E Test"], check=True)
    (path / "README").write_text("test layer\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def home_env(tmp_path: Path) -> dict:
    """Environment with HOME redirected to a temp dir so settings writes there."""
    env = os.environ.copy()
    home = tmp_path / "home"
    home.mkdir()
    env["HOME"] = str(home)
    return env


@pytest.fixture
def nxp_ws(tmp_path: Path) -> Path:
    """Workspace with an ``nxp/`` subdir, which triggers NXP family dispatch."""
    (tmp_path / "nxp").mkdir()
    return tmp_path


@pytest.fixture
def bbsetup_ws(tmp_path: Path) -> Path:
    """Minimal bbsetup workspace with the two required detection markers.

    ``is_bbsetup_workspace()`` checks for ``config/config-upstream.json``
    (with ``data`` and ``bitbake-config`` top-level keys) and
    ``build/init-build-env``.
    """
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config-upstream.json").write_text(
        '{"data": {"version": "1.0"}, "bitbake-config": {}, "name": "test"}\n'
    )
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "init-build-env").write_text("#!/bin/sh\n")
    return tmp_path


@pytest.fixture
def layer_ws(tmp_path: Path) -> Path:
    """NXP workspace with a real git layer and a bblayers.conf pointing at it.

    Layout::

        workspace/nxp/layers/meta-local/   <- git repo
        workspace/nxp/build/conf/bblayers.conf

    ``layers`` dispatches through ``_dispatch_bsp`` (no manifest flag), which
    defaults to ``nxp``, so ``bsp_root = workspace/nxp``.  TOPDIR in
    bblayers.conf expands to ``workspace/nxp/build``, making
    ``${TOPDIR}/../layers/meta-local`` resolve to ``workspace/nxp/layers/meta-local``.
    """
    layer = tmp_path / "nxp" / "layers" / "meta-local"
    _git_init(layer)
    conf_dir = tmp_path / "nxp" / "build" / "conf"
    conf_dir.mkdir(parents=True)
    (conf_dir / "bblayers.conf").write_text('BBLAYERS ?= " \\\n    ${TOPDIR}/../layers/meta-local"\n')
    return tmp_path


# ---------------------------------------------------------------------------
# 1. settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_list_shows_all_recognized_keys(self, home_env: dict) -> None:
        result = _run(["settings", "list"], env=home_env)
        assert result.returncode == 0
        assert "defaults.nxp.machine" in result.stderr
        assert "defaults.ti.machine" in result.stderr
        assert "build.container_image" in result.stderr
        assert "layers.show_hashes" in result.stderr
        assert "(unset)" in result.stderr

    def test_set_get_round_trip(self, home_env: dict) -> None:
        _run(["settings", "set", "defaults.nxp.machine", "imx8mp-var-dart"], env=home_env)
        result = _run(["settings", "get", "defaults.nxp.machine"], env=home_env)
        assert result.returncode == 0
        assert "imx8mp-var-dart" in result.stderr

    def test_unset_clears_key(self, home_env: dict) -> None:
        _run(["settings", "set", "defaults.nxp.machine", "imx8mp-var-dart"], env=home_env)
        _run(["settings", "unset", "defaults.nxp.machine"], env=home_env)
        result = _run(["settings", "get", "defaults.nxp.machine"], env=home_env)
        assert "(unset)" in result.stderr

    def test_multiple_keys_survive_independently(self, home_env: dict) -> None:
        _run(["settings", "set", "defaults.nxp.machine", "imx8mp-var-dart"], env=home_env)
        _run(["settings", "set", "defaults.nxp.distro", "fsl-imx-xwayland"], env=home_env)
        _run(["settings", "set", "build.container_image", "jetm/kas-build-env:latest"], env=home_env)
        result = _run(["settings", "list"], env=home_env)
        assert "imx8mp-var-dart" in result.stderr
        assert "fsl-imx-xwayland" in result.stderr
        assert "jetm/kas-build-env:latest" in result.stderr

    def test_invalid_key_rejected(self, home_env: dict) -> None:
        result = _run(["settings", "set", "nonexistent.key", "val"], env=home_env)
        assert result.returncode != 0

    def test_non_integer_host_count_rejected(self, home_env: dict) -> None:
        result = _run(["settings", "set", "host.inotify_instances", "8.5"], env=home_env)
        assert result.returncode != 0
        assert "inotify_instances" in result.stderr


# ---------------------------------------------------------------------------
# 2. diff (XML manifests)
# ---------------------------------------------------------------------------


@pytest.fixture
def manifest_pair(tmp_path: Path) -> tuple[Path, Path]:
    v1 = tmp_path / "v1.xml"
    v2 = tmp_path / "v2.xml"
    v1.write_text(
        "<manifest>\n"
        '  <project path="sources/poky"'
        ' revision="aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"/>\n'
        '  <project path="sources/meta-freescale"'
        ' revision="bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222"/>\n'
        "</manifest>\n"
    )
    v2.write_text(
        "<manifest>\n"
        '  <project path="sources/poky"'
        ' revision="cccc3333cccc3333cccc3333cccc3333cccc3333"/>\n'
        '  <project path="sources/meta-freescale"'
        ' revision="bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222"/>\n'
        "</manifest>\n"
    )
    return v1, v2


class TestDiff:
    def test_changed_and_unchanged_rows(self, manifest_pair: tuple[Path, Path], nxp_ws: Path) -> None:
        v1, v2 = manifest_pair
        result = _run(["diff", str(v1), str(v2), "--workspace", str(nxp_ws)])
        assert result.returncode == 0
        out = result.stderr
        assert "poky" in out
        assert "aaaa1111" in out
        assert "cccc3333" in out
        assert "changed" in out
        assert "meta-freescale" in out
        assert "bbbb2222" in out
        assert "unchanged" in out

    def test_identical_manifests_all_unchanged(self, manifest_pair: tuple[Path, Path], nxp_ws: Path) -> None:
        v1, _ = manifest_pair
        result = _run(["diff", str(v1), str(v1), "--workspace", str(nxp_ws)])
        assert result.returncode == 0
        out = result.stderr
        assert "unchanged" in out
        # "unchanged" contains "changed" - strip it before checking no plain "changed"
        assert "changed" not in out.replace("unchanged", "")


# ---------------------------------------------------------------------------
# 3. report
# ---------------------------------------------------------------------------


@pytest.fixture
def success_run_ws(bbsetup_ws: Path) -> Path:
    """bbsetup workspace with a synthetic success run."""
    run_dir = bbsetup_ws / "build" / "runs" / "20260601-120000"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text(
        '{"ts": "2026-06-01T12:00:00Z", "event": "run_start", "run_id": "20260601-120000"}\n'
        '{"ts": "2026-06-01T12:30:00Z", "event": "step_ok", "step": "kas_build",'
        ' "deploy_dir": "/work/deploy"}\n'
        '{"ts": "2026-06-01T12:30:00Z", "event": "run_end"}\n'
    )
    return bbsetup_ws


class TestReport:
    def test_success_run_human_output(self, success_run_ws: Path) -> None:
        result = _run(["report", "--workspace", str(success_run_ws)])
        assert result.returncode == 0
        assert "20260601-120000" in result.stderr
        assert "success" in result.stderr
        assert "duration" in result.stderr

    def test_json_output_is_parseable(self, success_run_ws: Path) -> None:
        result = _run(["report", "--json", "--workspace", str(success_run_ws)])
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["run_id"] == "20260601-120000"
        assert payload["status"] == "success"
        assert "duration_s" in payload
        assert "layers" in payload

    def test_latest_run_selects_newest(self, success_run_ws: Path) -> None:
        fail_dir = success_run_ws / "build" / "runs" / "20260601-130000"
        fail_dir.mkdir()
        (fail_dir / "events.jsonl").write_text(
            '{"ts": "2026-06-01T13:00:00Z", "event": "run_start", "run_id": "20260601-130000"}\n'
            '{"ts": "2026-06-01T13:05:00Z", "event": "step_fail", "step": "kas_build",'
            ' "reason": "build error"}\n'
            '{"ts": "2026-06-01T13:05:00Z", "event": "run_end"}\n'
        )
        result = _run(["report", "--workspace", str(success_run_ws)])
        assert result.returncode == 0
        assert "20260601-130000" in result.stderr
        assert "failure" in result.stderr

    def test_explicit_run_id_selects_older_run(self, success_run_ws: Path) -> None:
        fail_dir = success_run_ws / "build" / "runs" / "20260601-130000"
        fail_dir.mkdir()
        (fail_dir / "events.jsonl").write_text(
            '{"ts": "2026-06-01T13:00:00Z", "event": "run_start", "run_id": "20260601-130000"}\n'
        )
        result = _run(["report", "20260601-120000", "--workspace", str(success_run_ws)])
        assert result.returncode == 0
        assert "20260601-120000" in result.stderr

    def test_nonexistent_run_id_exits_nonzero(self, success_run_ws: Path) -> None:
        result = _run(["report", "99991231-000000", "--workspace", str(success_run_ws)])
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# 4. layers
# ---------------------------------------------------------------------------


class TestLayers:
    def test_repo_with_git_hash(self, layer_ws: Path) -> None:
        result = _run(["layers", "--workspace", str(layer_ws)])
        assert result.returncode == 0
        assert "Layers (" in result.stderr
        assert "meta-local" in result.stderr

    def test_empty_workspace_shows_guidance(self, nxp_ws: Path) -> None:
        # nxp_ws has no bblayers.conf yet - collect_layer_hashes returns []
        result = _run(["layers", "--workspace", str(nxp_ws)])
        assert result.returncode == 0
        guidance = result.stderr
        assert "bakar build" in guidance or "bakar sync" in guidance


# ---------------------------------------------------------------------------
# 5. for-all
# ---------------------------------------------------------------------------


class TestForAll:
    def test_repo_name_injected_into_command(self, layer_ws: Path) -> None:
        # console headers -> stderr; subprocess echo output -> stdout
        result = subprocess.run(
            [_BAKAR, "for-all", "echo REPO=$BAKAR_REPO_NAME", "--workspace", str(layer_ws)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert "REPO=meta-local" in result.stdout

    def test_repo_commit_env_var_is_non_empty(self, layer_ws: Path) -> None:
        result = subprocess.run(
            [_BAKAR, "for-all", "echo COMMIT=$BAKAR_REPO_COMMIT", "--workspace", str(layer_ws)],
            capture_output=True,
            text=True,
            check=False,
        )
        commit_line = next((ln for ln in result.stdout.splitlines() if "COMMIT=" in ln), "")
        sha = commit_line.split("COMMIT=", 1)[-1].strip()
        assert sha, "BAKAR_REPO_COMMIT was empty"

    def test_nonzero_exit_propagates(self, layer_ws: Path) -> None:
        result = subprocess.run(
            [_BAKAR, "for-all", "exit 1", "--workspace", str(layer_ws)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# 6-7. dump / lock  (require kas on PATH; use host mode via unset KCI)
# ---------------------------------------------------------------------------

_kas_required = pytest.mark.skipif(shutil.which("kas") is None, reason="kas not installed on this host")


@pytest.fixture
def kas_env() -> dict:
    """Environment with KAS_CONTAINER_IMAGE unset so bakar uses plain ``kas``.

    When KAS_CONTAINER_IMAGE is set, bakar picks ``kas-container``, which
    runs kas inside Docker and cannot see ``file://`` paths outside its
    bind-mounted work directory.
    """
    env = os.environ.copy()
    env.pop("KAS_CONTAINER_IMAGE", None)
    return env


@pytest.fixture
def kas_layer(tmp_path: Path) -> tuple[Path, str]:
    """A local git layer repo and its HEAD SHA."""
    layer = tmp_path / "meta-e2e"
    sha = _git_init(layer)
    return layer, sha


@pytest.fixture
def kas_yaml_pinned(tmp_path: Path, kas_layer: tuple[Path, str]) -> Path:
    """kas YAML with a pinned commit so ``kas dump`` needs no clone."""
    layer, sha = kas_layer
    config = tmp_path / "kas-e2e.yml"
    config.write_text(
        "header:\n"
        "  version: 21\n"
        "machine: qemux86-64\n"
        "distro: nodistro\n"
        "target: core-image-minimal\n"
        "repos:\n"
        "  meta-e2e:\n"
        f"    url: file://{layer}\n"
        f"    commit: {sha}\n"
        "    path: layers/meta-e2e\n"
        "    layers: {}\n"
    )
    return config


@pytest.fixture
def kas_yaml_branch(tmp_path: Path, kas_layer: tuple[Path, str]) -> Path:
    """kas YAML referencing the local layer by branch (for ``lock`` to resolve)."""
    layer, _ = kas_layer
    config = tmp_path / "kas-lock-e2e.yml"
    config.write_text(
        "header:\n"
        "  version: 21\n"
        "machine: qemux86-64\n"
        "distro: nodistro\n"
        "target: core-image-minimal\n"
        "repos:\n"
        "  meta-e2e:\n"
        f"    url: file://{layer}\n"
        "    branch: main\n"
        "    path: layers/meta-e2e\n"
        "    layers: {}\n"
    )
    return config


@_kas_required
@pytest.mark.timeout(120)
class TestDump:
    def test_output_file_contains_machine(self, kas_yaml_pinned: Path, kas_env: dict, tmp_path: Path) -> None:
        out = tmp_path / "dump.yml"
        result = subprocess.run(
            [_BAKAR, "dump", str(kas_yaml_pinned), "--output", str(out)],
            capture_output=True,
            text=True,
            check=False,
            env=kas_env,
        )
        assert result.returncode == 0, result.stderr
        assert out.exists()
        content = out.read_text()
        assert "machine:" in content
        assert "qemux86-64" in content

    def test_stdout_contains_machine(self, kas_yaml_pinned: Path, kas_env: dict) -> None:
        result = subprocess.run(
            [_BAKAR, "dump", str(kas_yaml_pinned)],
            capture_output=True,
            text=True,
            check=False,
            env=kas_env,
        )
        assert result.returncode == 0, result.stderr
        assert "machine:" in result.stdout


@_kas_required
@pytest.mark.timeout(120)
class TestLock:
    def test_lockfile_written_with_commit_sha(self, kas_yaml_branch: Path, kas_env: dict) -> None:
        result = subprocess.run(
            [_BAKAR, "lock", str(kas_yaml_branch)],
            capture_output=True,
            text=True,
            check=False,
            env=kas_env,
        )
        assert result.returncode == 0, result.stderr
        lock = kas_yaml_branch.with_suffix(".lock.yml")
        assert lock.exists(), f"expected {lock} to be created"
        assert "commit:" in lock.read_text()


# ---------------------------------------------------------------------------
# 8. triage
# ---------------------------------------------------------------------------


@pytest.fixture
def failure_run_ws(bbsetup_ws: Path) -> Path:
    """bbsetup workspace with a synthetic failure run."""
    run_dir = bbsetup_ws / "build" / "runs" / "20260601-100000"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text(
        '{"ts": "2026-06-01T10:00:00Z", "event": "run_start", "run_id": "20260601-100000"}\n'
        '{"ts": "2026-06-01T10:05:00Z", "event": "step_fail", "step": "kas_build",'
        ' "reason": "recipe failed"}\n'
        '{"ts": "2026-06-01T10:05:00Z", "event": "run_end"}\n'
    )
    return bbsetup_ws


class TestTriage:
    def test_failure_run_shows_failing_step(self, failure_run_ws: Path) -> None:
        result = _run(["triage", "--workspace", str(failure_run_ws)])
        assert result.returncode == 0
        assert "kas_build" in result.stderr

    def test_success_run_shows_no_failures(self, success_run_ws: Path) -> None:
        result = _run(["triage", "--workspace", str(success_run_ws)])
        assert result.returncode == 0
        assert "no step_fail events found" in result.stderr

    def test_nonexistent_run_id_exits_nonzero(self, failure_run_ws: Path) -> None:
        result = _run(["triage", "99991231-000000", "--workspace", str(failure_run_ws)])
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# 9. log (error paths only -- happy path calls _tail_follow which blocks)
# ---------------------------------------------------------------------------


@pytest.fixture
def runs_ws(nxp_ws: Path) -> Path:
    """NXP workspace with a synthetic run containing a console.log and events.jsonl."""
    run_dir = nxp_ws / "nxp" / "build" / "runs" / "20260601-090000"
    run_dir.mkdir(parents=True)
    (run_dir / "console.log").write_text("step started\nstep ok\n")
    (run_dir / "events.jsonl").write_text('{"ts": "2026-06-01T09:00:00Z", "event": "run_start"}\n')
    return nxp_ws


class TestLog:
    def test_invalid_which_exits_2(self, nxp_ws: Path) -> None:
        result = _run(["log", "--which", "invalid", "--workspace", str(nxp_ws)])
        assert result.returncode == 2

    def test_no_runs_dir_exits_nonzero(self, nxp_ws: Path) -> None:
        # nxp_ws has no nxp/build/runs/ - command should bail with exit 1
        result = _run(["log", "--workspace", str(nxp_ws)])
        assert result.returncode != 0

    def test_run_id_not_found_exits_nonzero(self, runs_ws: Path) -> None:
        result = _run(["log", "--run", "99991231-000000", "--workspace", str(runs_ws)])
        assert result.returncode != 0

    def test_missing_kas_and_console_log_exits_nonzero(self, runs_ws: Path) -> None:
        # Remove console.log; kas.log is also absent. Both fallbacks gone -> exit 1.
        (runs_ws / "nxp" / "build" / "runs" / "20260601-090000" / "console.log").unlink()
        result = _run(["log", "--workspace", str(runs_ws)])
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# 10. clean
# ---------------------------------------------------------------------------


class TestClean:
    def test_removes_build_directory(self, nxp_ws: Path) -> None:
        build_dir = nxp_ws / "nxp" / "build"
        build_dir.mkdir(parents=True)
        (build_dir / "conf").mkdir()
        result = _run(["clean", "--bsp", "nxp", "--workspace", str(nxp_ws)])
        assert result.returncode == 0
        assert not build_dir.exists()

    def test_noop_when_no_build_dir(self, nxp_ws: Path) -> None:
        result = _run(["clean", "--bsp", "nxp", "--workspace", str(nxp_ws)])
        assert result.returncode == 0

    def test_invalid_bsp_exits_2(self, nxp_ws: Path) -> None:
        result = _run(["clean", "--bsp", "invalid", "--workspace", str(nxp_ws)])
        assert result.returncode == 2


# ---------------------------------------------------------------------------
# 11. doctor
# ---------------------------------------------------------------------------


class TestDoctor:
    def test_exits_0_or_2(self, nxp_ws: Path) -> None:
        # In a test environment most tool checks will WARN or BLOCK; 0 means all
        # pass, 2 means at least one BLOCK failure -- any other code is a bug.
        result = _run(["doctor", "--workspace", str(nxp_ws)])
        assert result.returncode in (0, 2)

    def test_produces_diagnostic_output(self, nxp_ws: Path) -> None:
        result = _run(["doctor", "--workspace", str(nxp_ws)])
        combined = result.stderr + result.stdout
        assert any(marker in combined for marker in ("PASS", "WARN", "BLOCK", "FAIL", "SKIP", "checks passed"))
