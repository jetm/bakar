"""Shared fixtures for the bakar test suite.

Provides hermetic fixtures and sample-content constants consumed by the
Category B logic-module tests (``test_triage``, ``test_workspace``,
``test_layers``) and the Category A command-module tests
(``test_cli_log``, ``test_cli_triage``, etc.).

All fixtures stay rooted in ``tmp_path`` so tests never touch the real
host filesystem. The sample constants intentionally match the parsers
in ``src/bakar``:

- ``MINIMAL_NXP_MANIFEST`` uses ``path=`` and a 40-hex-char ``revision``
  because ``workspace.parse_manifest_pins`` filters on those exact two
  attributes (``src/bakar/workspace.py:106-110``).
- ``SAMPLE_EVENTS_JSONL`` uses ``event``/``step``/``reason`` keys because
  ``observability.last_run_event`` filters on ``rec.get("event")`` and
  ``analyse`` reads ``step``/``reason`` (``src/bakar/triage.py:53,209-210``).
- ``SAMPLE_KAS_LOG`` includes an ``ERROR: <recipe> do_compile: ...`` line
  matching ``_RECIPE_ERROR_RE`` (``src/bakar/triage.py:101-105``).
"""

from __future__ import annotations

import atexit
import contextlib
import importlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from typer.testing import CliRunner

# Record the developer's real home, then point HOME at a throwaway directory
# BEFORE the first ``bakar`` import: several modules compute state locations
# from ``Path.home()`` at import time, and those must land in the session home.
REAL_HOME = Path.home()
_SESSION_HOME = Path(tempfile.mkdtemp(prefix="bakar-test-home-"))
os.environ["HOME"] = str(_SESSION_HOME)
atexit.register(shutil.rmtree, _SESSION_HOME, ignore_errors=True)

import bakar.cli  # noqa: E402, F401 - registers all subcommands on the shared app
from bakar import cache_render  # noqa: E402
from bakar.commands._app import app  # noqa: E402
from bakar.config import BuildConfig  # noqa: E402
from bakar.report import ReportSummary  # noqa: E402
from bakar.setup.actions.base import RunCommand, WriteFile  # noqa: E402
from bakar.setup.profile import HostProfile  # noqa: E402
from bakar.steps import build_ui_render  # noqa: E402

# Test modules load tests/_feed_upstream.py through ``pytest_plugins``. pytest
# rewrites asserts only in plugins registered before they are imported, so
# register the helper here or PytestAssertRewriteWarning fires.
pytest.register_assert_rewrite("tests._feed_upstream")

# Prevent Rich from inserting ANSI escape codes into captured CLI output.
# Without this, --help text arrives with mid-token color resets (e.g.
# "--sstate" + ESC[0m + "-mirror"), breaking plain substring assertions.
os.environ.setdefault("NO_COLOR", "1")

# A prepared-host baseline profile: every knob meets its recommended target,
# docker is installed and the user is in its group. The seven ``test_setup_*``
# suites build on this via ``make_host_profile(**overrides)``, each overriding
# the fields its action reads to simulate a specific gap.
_BASE_HOST_PROFILE: dict[str, object] = {
    "cpu_count": 8,
    "mem_available_gb": 32.0,
    "disk_free_gb": 400.0,
    "distro_id": "arch",
    "pkg_manager": "pacman",
    "in_docker_group": True,
    "docker_installed": True,
    "inotify_instances": 8192,
    "inotify_watches": 1048576,
    "swappiness": 10,
    "docker_nofile_soft": 65536,
}


def make_host_profile(**overrides: object) -> HostProfile:
    """A prepared-host ``HostProfile`` by default; override fields to simulate gaps."""
    return HostProfile(**{**_BASE_HOST_PROFILE, **overrides})


def run_commands(ops: Sequence[RunCommand | WriteFile]) -> list[RunCommand]:
    """Assert every operation is a ``RunCommand`` and return them as such.

    ``Action.operations()`` returns ``list[RunCommand | WriteFile]``, so a test
    reading ``op.argv`` off the result is asserting the shape as well as the
    contents. Stating that as an assertion keeps the claim checked at runtime
    instead of leaving it implicit in an attribute access that would raise
    ``AttributeError`` on a ``WriteFile``.
    """
    commands = []
    for op in ops:
        assert isinstance(op, RunCommand), f"expected a RunCommand operation, got {op!r}"
        commands.append(op)
    return commands


# Minimal NXP BuildConfig defaults (the imx8mp / 6.6.52 / 5.2-f40 host-mode shape
# duplicated verbatim across the kas-env, diagnostics-host, buildtools-provision,
# eventlog-injection, and run-build-host suites). Callers pass ``workspace=`` and
# override any field whose suite-specific value differs, so every constructed
# config stays field-for-field identical to its former local builder.
_BUILD_CONFIG_DEFAULTS: dict[str, object] = {
    "bsp_family": "nxp",
    "machine": "imx8mp-var-dart",
    "distro": "fsl-imx-xwayland",
    "image": "core-image-minimal",
    "manifest": "imx-6.6.52-2.2.2.xml",
    "repo_url": "https://example.invalid/repo.git",
    "repo_branch": "imx-6.6.52-2.2.2",
    "kas_container_image": "jetm/kas-build-env:5.2-f40",
}


def make_build_config(**overrides: object) -> BuildConfig:
    """A minimal NXP ``BuildConfig``; pass ``workspace=`` plus any field overrides."""
    return BuildConfig(**{**_BUILD_CONFIG_DEFAULTS, **overrides})  # type: ignore[arg-type]


# A successful-run ``ReportSummary`` shape shared by the report-command tests
# (``test_report``'s ccache cases, ``test_report_buildhistory``, and
# ``test_report_sstate``). Each site overrides the section-specific fields
# (sstate counts, buildhistory packages, ccache/dist maps) it asserts on, so
# every constructed summary stays field-for-field identical to its former local
# builder. ``layers`` is left to the dataclass ``default_factory`` so each
# summary gets a fresh list.
_REPORT_SUMMARY_DEFAULTS: dict[str, object] = {
    "run_id": "20260527-100000",
    "status": "success",
    "duration_s": 1845.0,
    "deploy_dir": "/work/build/tmp/deploy/images/imx8mp-var-dart",
    "image_size": 123456,
    "build_revision": None,
}


def make_report_summary(**overrides: object) -> ReportSummary:
    """A success-run ``ReportSummary``; override any field the test asserts on."""
    return ReportSummary(**{**_REPORT_SUMMARY_DEFAULTS, **overrides})  # type: ignore[arg-type]


# Plain-mode glyph icons that must never leak into no-ANSI/no-glyph assertions.
# Shared by test_ci_output_mode.py, test_monitor_plain.py, test_build_ui_plain.py
# (each previously carried its own identical copy of this tuple).
_GLYPHS = (
    build_ui_render._ICON_COMPILE,
    build_ui_render._ICON_FETCH,
    build_ui_render._ICON_CONFIGURE,
    build_ui_render._ICON_PACKAGE,
    build_ui_render._ICON_SETSCENE,
    build_ui_render._ICON_TIMER,
    build_ui_render._ICON_DRIFT,
    # Cache-backend "none" badge glyph (nf-fa-ban) - rendered only in the Rich
    # make_renderable() task table, never in plain_status_line()'s ASCII
    # cache_backend= token.
    cache_render._BACKEND_BAN_GLYPH,
)

# Synthetic `bakar monitor` snapshot + CLI-invocation helper shared by
# test_ci_output_mode.py and test_monitor_plain.py. Carries non-empty
# daemons/running/failures so it exercises both the --json equality check and
# the plain-render field assertions in test_monitor_plain.py.
MONITOR_SNAPSHOT = {
    "run": "20260101-000000",
    "cluster": {
        "reachable": True,
        "error": None,
        "capacity": {"num_servers": 1, "num_cpus": 8, "in_progress": 0, "servers": []},
    },
    "build_daemon": None,
    "ccache": None,
    "daemons": {
        "hashserv": {"url": "ws://h:8686", "running": True},  # nosemgrep
        "prserv": {"host": "h:8585", "running": False},
    },
    "build": {
        "live": True,
        "outcome": None,
        "elapsed_seconds": 61,
        "tasks_done": 10,
        "tasks_total": 100,
        "tasks_remaining": 90,
        "tasks_running": 3,
        "tasks_failed": 1,
        "tasks_setscene_rerun": 2,
        "running": [{"recipe": "foo", "task": "do_compile", "cache_backend": None}],
        "failures": [{"recipe": "bar", "task": "do_install"}],
    },
    "kas_errors": [],
}


def _invoke_monitor(args, tmp_path):
    """Invoke the CLI with monitor-command args against MONITOR_SNAPSHOT."""
    cfg = mock.Mock(runs_dir=tmp_path)
    with (
        mock.patch("bakar.commands.monitor.resolve_global_config", return_value=cfg),
        mock.patch("bakar.commands.monitor._resolve_workspace", return_value=tmp_path),
        mock.patch("bakar.commands.monitor._bsp_from_cwd", return_value="nxp"),
        mock.patch("bakar.commands.monitor._resolve_run_dir", return_value=tmp_path),
        mock.patch("bakar.commands.monitor._daemon_status", return_value={}),
        mock.patch("bakar.commands.monitor._resolve_scheduler_url", return_value=None),
        mock.patch("bakar.commands.monitor._snapshot", return_value=dict(MONITOR_SNAPSHOT)),
        mock.patch("bakar.commands.monitor._recent_kas_errors", return_value=[]),
    ):
        return CliRunner().invoke(app, args)


@pytest.fixture(autouse=True)
def _reset_invocation_cwd():
    """Clear ``_helpers._INVOCATION`` around every test.

    The ``-w`` callback (``_enter_workspace``) records the pre-chdir cwd in the
    module-global ``_INVOCATION`` so ``_bsp_from_cwd`` can read it. Direct
    ``_bsp_from_cwd`` unit tests do not go through the callback, so without this
    reset a prior test's captured cwd leaks into them under ``pytest-randomly``.
    """
    from bakar.commands._helpers import _INVOCATION

    _INVOCATION.clear()
    yield
    _INVOCATION.clear()


@pytest.fixture(autouse=True)
def _reset_routing_env(monkeypatch):
    """Clear the process-wide routing variables around every test.

    ``_resolve_workspace`` sets ``BAKAR_REPO_POLICY=reuse`` in ``os.environ`` when
    it routes a build, and a dispatched build reads ``BAKAR_WORKTREE_BUILD_DIR``.
    A test that routes without isolating them leaks the reuse policy into later
    tests, which then build an overlay their mocks do not expect.
    """
    monkeypatch.delenv("BAKAR_REPO_POLICY", raising=False)
    monkeypatch.delenv("BAKAR_WORKTREE_BUILD_DIR", raising=False)


def home_constants(home: Path) -> list[tuple[str, str, Path]]:
    """Every module-level constant derived from the home, as ``(module, attr, value)``."""
    state = home / ".local" / "state" / "bakar"
    buildtools = home / ".local" / "share" / "bakar" / "buildtools"
    return [
        ("bakar.daemon_log", "STATE_DIR", state),
        ("bakar.central_service", "_STATE_DIR", state),
        ("bakar.sccache_server", "_STATE_DIR", state),
        ("bakar.task_timings", "TIMINGS_DIR", home / ".local/state/bakar/task-timings"),
        ("bakar.task_timings", "DEFAULT_TIMINGS_PATH", home / ".local/state/bakar/task-timings.json"),
        ("bakar.commands.presets", "_CONFIG_PATH", home / ".config" / "bakar" / "config.toml"),
        ("bakar.steps.bitbake_override", "DEFAULT_OVERRIDE_REPO", home / "repos" / "personal" / "yocto" / "bitbake"),
        ("bakar.setup.actions.tools", "DEFAULT_BUILDTOOLS_DIR", buildtools),
        ("bakar.setup.plan", "DEFAULT_BUILDTOOLS_DIR", buildtools),
    ]


@pytest.fixture(autouse=True)
def _isolated_home(request, tmp_path_factory, monkeypatch):
    """Give every test a fresh empty home, or the real one under ``real_home``.

    The import-time constants were computed under the session home, so they are
    repointed on every test: at a fresh per-test home normally, back at the
    real home for a ``real_home``-marked test. monkeypatch restores them.
    """
    if request.node.get_closest_marker("real_home"):
        home = REAL_HOME
    else:
        home = tmp_path_factory.mktemp("home")
        # git reads identity and commit signing from the developer's global config
        # (home and XDG); give fixtures that run ``git commit`` a neutral one.
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
        for who in ("AUTHOR", "COMMITTER"):
            monkeypatch.setenv(f"GIT_{who}_NAME", "bakar-test")
            monkeypatch.setenv(f"GIT_{who}_EMAIL", "bakar-test@example.invalid")
    monkeypatch.setenv("HOME", str(home))
    for module, attr, value in home_constants(home):
        monkeypatch.setattr(importlib.import_module(module), attr, value)
    return home


@pytest.fixture
def user_config_file() -> Path:
    """Path of the isolated ``$HOME/.config/bakar/config.toml``, parents created."""
    path = Path(os.environ["HOME"]) / ".config" / "bakar" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(autouse=True)
def _no_user_workspaces(monkeypatch):
    """Route every test through an empty user config unless it injects its own.

    Routing reads ``~/.config/bakar/config.toml``; a real ``[workspaces]`` table
    there would make any meta-avocado test run a real ``kas dump`` probe. Tests
    that exercise routing patch these names themselves, which overrides this.
    """
    from bakar.user_config import UserConfig

    monkeypatch.setattr("bakar.commands._workspace.load_user_config", UserConfig)
    monkeypatch.setattr("bakar.commands.triage.load_user_config", UserConfig)


@pytest.fixture(autouse=True)
def _no_systemd_scope_probe(request, monkeypatch):
    """Default ``build_scope.systemd_run_available`` to False across the suite.

    The real implementation runs a live ``systemd-run --user --scope -- true``
    probe (cached via ``functools.cache``), which both spawns a real transient
    scope as a test side effect and collides with tests that fake
    ``subprocess.Popen``: the probe's ``subprocess.run`` then hits the fake proc
    (no ``__exit__``) and raises, flakily, depending on whether ``pytest-randomly``
    warmed the cache first. Stubbing it False makes every build-driving test run
    unscoped and deterministic. ``test_build_scope`` exercises the real gate and
    manages this itself, so it is exempt.
    """
    if "test_build_scope" in request.module.__name__:
        yield
        return
    import bakar.build_scope as _build_scope

    monkeypatch.setattr(_build_scope, "systemd_run_available", lambda: False)
    yield


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

# Two synthetic projects pinned to 40-hex-char SHAs.  parse_manifest_pins
# only emits pins whose revision matches _HEX40_RE, so the SHAs below are
# deliberately literal 40-char hex strings.
MINIMAL_NXP_MANIFEST = """\
<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="freescale" fetch="https://github.com/nxp-imx"/>
  <default revision="master" remote="freescale" sync-j="4"/>
  <project path="sources/poky" name="poky" revision="{sha_a}"/>
  <project path="sources/meta-imx" name="meta-imx" revision="{sha_b}"/>
</manifest>
""".format(sha_a="a" * 40, sha_b="b" * 40)

# Two JSON lines: a step_start and a matching step_fail for the same
# step. observability.last_run_event scans for event=="step_fail" and
# analyse() reads step/reason off the resulting record.
SAMPLE_EVENTS_JSONL = (
    '{"event": "step_start", "step": "kas-build", "ts": "2026-05-29T12:00:00Z"}\n'
    '{"event": "step_fail", "step": "kas-build", "reason": "bitbake exited 1", '
    '"ts": "2026-05-29T12:05:00Z"}\n'
)

# Includes one line matching _RECIPE_ERROR_RE (recipe + do_compile +
# message) plus surrounding context so _scan_recipe_errors finds exactly
# one RecipeError and _tail has multiple lines to slice.
SAMPLE_KAS_LOG = """\
NOTE: Resolving any missing task queue dependencies
Initialising tasks: 100% |#######################################| Time: 0:00:03
NOTE: Executing Tasks
ERROR: linux-imx-6.6.52+gitAUTOINC+a1b2c3d4e5-r0 do_compile: Function failed: do_compile
ERROR: Logfile of failure stored in: /work/tmp/work/linux-imx/temp/log.do_compile.12345
NOTE: Tasks Summary: Attempted 4321 tasks of which 4320 didn't need to be rerun and 1 failed.
"""

# Two recipes whose TASK graph is a DAG but whose PN collapse is cyclic: a's
# do_configure needs b's sysroot while b's do_package needs a's.  This is the
# shape every real OE graph has - shared between test_graph_analyze.py (which
# guards both directions with test_task_level_dag_survives_a_cyclic_pn_collapse)
# and test_cli_graph.py, so an edit to the shape cannot drift between them.
PN_CYCLE_TASK_DAG_DOT = (
    'digraph depends {\n"a.do_configure" -> "b.do_populate_sysroot"\n"b.do_package" -> "a.do_populate_sysroot"\n}\n'
)


@pytest.fixture
def nfs_probe(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool | None], None]:
    """Return a setter that fixes the NFS probe answer the stop module sees.

    build_stop calls ``mounts.is_path_on_nfs`` through the module, so patching
    the definer is the seam it observes.
    """

    def _set(answer: bool | None) -> None:
        monkeypatch.setattr("bakar.mounts.is_path_on_nfs", lambda _p: answer)

    return _set


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_workspace(tmp_path: Path) -> Path:
    """Minimal NXP workspace: ``.bakar.toml`` marker, ``nxp/`` subdir, manifest."""
    (tmp_path / ".bakar.toml").write_text("")
    nxp = tmp_path / "nxp"
    nxp.mkdir()
    (nxp / "imx-6.1.55-2.2.0.xml").write_text(MINIMAL_NXP_MANIFEST)
    return tmp_path


@pytest.fixture
def nxp_workspace(tmp_path: Path) -> Path:
    """Minimal NXP workspace: an ``nxp/`` subdir so workspace detection picks nxp."""
    (tmp_path / "nxp").mkdir()
    return tmp_path


@pytest.fixture
def fake_run_dir(tmp_path: Path) -> Path:
    """Synthetic build run dir with ``events.jsonl`` and ``kas.log``."""
    run = tmp_path / "build" / "runs" / "20260529-120000"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(SAMPLE_EVENTS_JSONL)
    (run / "kas.log").write_text(SAMPLE_KAS_LOG)
    return run


@pytest.fixture(autouse=True)
def _fake_buildtools_toolchain(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make host-mode builds find a pinned buildtools-extended toolchain.

    Host builds now refuse to fall back to the system gcc: ``_build_env`` calls
    ``_provision_buildtools`` which raises when no toolchain is detected. The
    suite's host-mode env-emission tests don't care about provisioning, so this
    autouse fixture sets ``OECORE_NATIVE_SYSROOT`` to a temp sysroot carrying a
    stub gcc. Tests that specifically exercise the missing-toolchain path
    (``test_buildtools_provision.py``) clear these vars in their own autouse
    fixture, which runs after this one and wins.
    """
    sysroot = tmp_path_factory.mktemp("buildtools-sysroot")
    gcc = sysroot / "usr" / "bin" / "gcc"
    gcc.parent.mkdir(parents=True, exist_ok=True)
    gcc.write_text("#!/bin/sh\n")
    monkeypatch.setenv("OECORE_NATIVE_SYSROOT", str(sysroot))


@dataclass(frozen=True)
class MetaAvocadoWorktreeLayout:
    ws: Path
    main: Path
    worktree: Path
    kas_yaml: Path


@pytest.fixture
def meta_avocado_worktree_layout(tmp_path: Path) -> MetaAvocadoWorktreeLayout:
    """A workspace whose ``meta-avocado`` is a real git repo with one linked worktree."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    ident = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]

    def git(*args: str) -> None:
        subprocess.run(["git", *ident, *args], check=True, capture_output=True, env=env)

    ws = tmp_path / "ws"
    (ws / "openembedded-core" / "meta" / "conf").mkdir(parents=True)
    (ws / ".bakar.toml").write_text("")
    (ws / "openembedded-core" / "meta" / "conf" / "layer.conf").write_text('LAYERSERIES_CORENAMES = "wrynose"\n')
    main = ws / "meta-avocado"
    (main / "kas" / "machine").mkdir(parents=True)
    git("init", "-q", "-b", "main", str(main))
    (main / "kas" / "machine" / "m.yml").write_text(
        "header:\n  version: 16\nmachine: qemux86-64\nrepos:\n  meta-avocado:\n    path: meta-avocado\n"
    )
    git("-C", str(main), "add", "-A")
    git("-C", str(main), "commit", "-q", "-m", "init")
    worktree = tmp_path / "root" / ".worktrees" / "meta-avocado" / "wt1"
    worktree.parent.mkdir(parents=True)
    git("-C", str(main), "worktree", "add", "-q", str(worktree), "-b", "wt1")
    return MetaAvocadoWorktreeLayout(
        ws=ws, main=main, worktree=worktree, kas_yaml=worktree / "kas" / "machine" / "m.yml"
    )


_CHILD_READY_TIMEOUT_S = 5.0


@contextlib.contextmanager
def _live_child(*extra_argv: str) -> Iterator[subprocess.Popen[bytes]]:
    """Start a sleeping child in its own session, yield it once /proc shows ``extra_argv``.

    ``Popen`` returns before the child's ``/proc/<pid>/cmdline`` is guaranteed
    to carry the final argv under load, and the bitbake liveness probe reads
    exactly that file. Polling (bounded) until every ``extra_argv`` word is
    visible removes the race; a timeout fails the test with the cmdline seen.
    Teardown signals only the child's own process group.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", *extra_argv],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert os.getpgid(child.pid) == child.pid  # fresh session leader
        deadline = time.monotonic() + _CHILD_READY_TIMEOUT_S
        cmdline = ""
        while True:
            try:
                raw = Path(f"/proc/{child.pid}/cmdline").read_bytes()
                cmdline = raw.replace(b"\x00", b" ").decode(errors="replace")
            except OSError:
                cmdline = ""
            if child.poll() is None and all(word in cmdline for word in extra_argv):
                break
            if time.monotonic() > deadline:
                pytest.fail(
                    f"child {child.pid} not ready within {_CHILD_READY_TIMEOUT_S}s "
                    f"(alive={child.poll() is None}, cmdline={cmdline!r}, wanted={extra_argv!r})"
                )
            time.sleep(0.01)
        yield child
    finally:
        if child.poll() is None and os.getpgid(child.pid) == child.pid:
            os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if os.getpgid(child.pid) == child.pid:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()


@pytest.fixture
def live_child() -> Callable[..., contextlib.AbstractContextManager[subprocess.Popen[bytes]]]:
    """Factory for ``_live_child``: ``with live_child("bitbake") as child:``."""
    return _live_child


@pytest.fixture
def bitbake_child() -> Iterator[subprocess.Popen[bytes]]:
    """A live child whose cmdline is confirmed to contain ``bitbake``."""
    with _live_child("bitbake") as child:
        yield child
