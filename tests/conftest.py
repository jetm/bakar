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
  ``triage._last_event_matching`` filters on ``rec.get("event")`` and
  ``analyse`` reads ``step``/``reason`` (``src/bakar/triage.py:53,209-210``).
- ``SAMPLE_KAS_LOG`` includes an ``ERROR: <recipe> do_compile: ...`` line
  matching ``_RECIPE_ERROR_RE`` (``src/bakar/triage.py:101-105``).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from typer.testing import CliRunner

import bakar.cli  # noqa: F401 - registers all subcommands on the shared app
from bakar import cache_render
from bakar.commands._app import app
from bakar.config import BuildConfig
from bakar.report import ReportSummary
from bakar.setup.profile import HostProfile
from bakar.steps import build_ui

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
    build_ui._ICON_COMPILE,
    build_ui._ICON_FETCH,
    build_ui._ICON_CONFIGURE,
    build_ui._ICON_PACKAGE,
    build_ui._ICON_SETSCENE,
    build_ui._ICON_TIMER,
    build_ui._ICON_DRIFT,
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
        mock.patch("bakar.commands.monitor.resolve", return_value=cfg),
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
    from pathlib import Path

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
# step. triage._last_event_matching scans for event=="step_fail" and
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
