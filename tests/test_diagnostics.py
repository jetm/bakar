"""Unit tests for bspctl.diagnostics.

Focuses on ``check_container_os``: verifies the BLOCK escalation for
container Python 3.13.x and 3.14.x, the PASS path on 3.12 and earlier
3.11/3.10, and the WARN-skip behaviour when docker is unreachable.
``subprocess.run`` is patched so no real container is spawned.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bspctl.config import BuildConfig
from bspctl.diagnostics import (
    _DOCKER_CHECKS,
    _REQUIRED_TOOLS_BY_FAMILY,
    SHARED_CHECKS,
    Severity,
    Status,
    _read_sysctl,
    check_bbsetup_config_sources,
    check_bitbake_locks,
    check_container_os,
    check_git_global_config,
    check_host_tools,
    check_psi_support,
    check_sysctl,
    run_all,
)

pytestmark = pytest.mark.unit


def _cfg() -> BuildConfig:
    return BuildConfig(
        workspace=Path("/tmp/fake-workspace"),
        bsp_family="ti",
        machine="am62x-var-som",
        distro="arago",
        image="var-thin-image",
        manifest="processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
        repo_url="https://example.invalid/none.git",
        repo_branch="scarthgap",
        container_image="jetm/kas-build-env:latest",
    )


def _mock_run(stdout: str, returncode: int = 0):
    """Return a CompletedProcess-shaped object with the given stdout."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


@pytest.mark.parametrize(
    ("stdout", "expected_minor_label"),
    [
        ("ubuntu noble\nPython 3.12.7\n", "3.12"),
        ("ubuntu jammy\nPython 3.10.12\n", "3.10"),
        ("fedora \nPython 3.12.5\n", "3.12"),  # Fedora 40 (no codename emitted)
    ],
)
def test_supported_python_passes_at_block_severity(stdout: str, expected_minor_label: str) -> None:
    """3.12 and earlier pass the check; severity stays BLOCK on success."""
    with patch("bspctl.diagnostics.subprocess.run", return_value=_mock_run(stdout)):
        result = check_container_os(_cfg())
    assert result.status == Status.PASS
    assert result.severity == Severity.BLOCK
    assert expected_minor_label in result.message


@pytest.mark.parametrize(
    "stdout",
    [
        "debian trixie\nPython 3.13.1\n",
        "debian trixie\nPython 3.13.0\n",
        "ubuntu \nPython 3.13.5\n",  # any distro, just 3.13
    ],
)
def test_python_313_blocks(stdout: str) -> None:
    with patch("bspctl.diagnostics.subprocess.run", return_value=_mock_run(stdout)):
        result = check_container_os(_cfg())
    assert result.status == Status.FAIL
    assert result.severity == Severity.BLOCK
    assert "3.13" in result.message
    assert "fork-in-multi-thread" in result.message
    assert result.fix_hint is not None
    assert "5.2-ubuntu24.04" in result.fix_hint


@pytest.mark.parametrize(
    "stdout",
    [
        "fedora \nPython 3.14.0\n",  # Fedora 43
        "fedora \nPython 3.14.1\n",
        "ubuntu questing\nPython 3.14.0\n",
    ],
)
def test_python_314_blocks(stdout: str) -> None:
    with patch("bspctl.diagnostics.subprocess.run", return_value=_mock_run(stdout)):
        result = check_container_os(_cfg())
    assert result.status == Status.FAIL
    assert result.severity == Severity.BLOCK
    assert "3.14" in result.message
    assert "PicklingError" in result.message or "forkserver" in result.message
    assert result.fix_hint is not None
    assert "5.2-ubuntu24.04" in result.fix_hint


def test_docker_timeout_skips_at_warn() -> None:
    """A transient docker hiccup must not block the build."""
    with patch(
        "bspctl.diagnostics.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=20),
    ):
        result = check_container_os(_cfg())
    assert result.status == Status.SKIP
    assert result.severity == Severity.WARN


def test_docker_missing_skips_at_warn() -> None:
    with patch(
        "bspctl.diagnostics.subprocess.run",
        side_effect=FileNotFoundError("docker"),
    ):
        result = check_container_os(_cfg())
    assert result.status == Status.SKIP
    assert result.severity == Severity.WARN


def test_nonzero_returncode_skips_at_warn() -> None:
    with patch(
        "bspctl.diagnostics.subprocess.run",
        return_value=_mock_run("", returncode=1),
    ):
        result = check_container_os(_cfg())
    assert result.status == Status.SKIP
    assert result.severity == Severity.WARN


def test_empty_output_skips_at_warn() -> None:
    with patch(
        "bspctl.diagnostics.subprocess.run",
        return_value=_mock_run("\n"),
    ):
        result = check_container_os(_cfg())
    assert result.status == Status.SKIP
    assert result.severity == Severity.WARN


def test_unparseable_python_line_passes() -> None:
    """If the python3 --version line is malformed, fall through to PASS
    rather than producing a false BLOCK."""
    with patch(
        "bspctl.diagnostics.subprocess.run",
        return_value=_mock_run("ubuntu noble\nweird-output-no-version\n"),
    ):
        result = check_container_os(_cfg())
    assert result.status == Status.PASS
    assert result.severity == Severity.BLOCK


def _host_cfg(bsp_family: str = "generic") -> BuildConfig:
    """Build a BuildConfig with ``host_mode=True`` for host-build checks."""
    return BuildConfig(
        workspace=Path("/tmp/fake-workspace"),
        bsp_family=bsp_family,  # type: ignore[arg-type]
        machine="qemux86-64",
        distro="poky",
        image="core-image-minimal",
        manifest="generic.yml",
        repo_url="https://example.invalid/none.git",
        repo_branch="main",
        container_image="jetm/kas-build-env:latest",
        host_mode=True,
    )


def test_run_all_skips_docker_checks_in_host_mode() -> None:
    """``run_all`` must filter the Docker-dependent checks out of its
    iteration when ``cfg.host_mode`` is True so plain ``kas`` builds do
    not trip container-runtime gates."""
    cfg = _host_cfg()
    results = run_all(cfg)
    names = {r.name for r in results}
    docker_check_names = {
        "docker-daemon",
        "container-image",
        "container-os",
        "container-bitbake",
        "docker-ulimits",
    }
    assert names.isdisjoint(docker_check_names), (
        f"host-mode run_all returned Docker-dependent check names: {names & docker_check_names}"
    )


def test_check_host_tools_host_mode_substitutes_kas() -> None:
    """``check_host_tools`` in host mode reports ``kas`` instead of
    ``kas-container`` and drops ``docker`` from the required-tools tuple."""
    cfg = _host_cfg()
    result = check_host_tools(cfg)
    hint = result.fix_hint or ""
    combined = result.message + " " + hint
    assert "kas" in combined
    assert "kas-container" not in combined
    assert "docker" not in combined


# ---------------------------------------------------------------------------
# bbsetup checks
# ---------------------------------------------------------------------------

_FIXTURE = Path(__file__).resolve().parent.parent / "examples" / "bbsetup-oe-nodistro-wrynose"


def _bbsetup_cfg(workspace: Path, *, host_mode: bool = False) -> BuildConfig:
    """Build a bbsetup BuildConfig rooted at ``workspace`` (the setup dir)."""
    return BuildConfig(
        workspace=workspace,
        bsp_family="bbsetup",  # type: ignore[arg-type]
        machine="qemux86-64",
        distro="nodistro",
        image="core-image-minimal",
        manifest="config-upstream.json",
        repo_url="https://example.invalid/none.git",
        repo_branch="wrynose",
        container_image="jetm/kas-build-env:latest",
        host_mode=host_mode,
    )


def _write_bbsetup_workspace(root: Path, sources: dict) -> None:
    """Create a minimal bbsetup workspace under ``root`` with the given sources block."""
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "build").mkdir(parents=True, exist_ok=True)
    config = {
        "data": {"sources": sources, "version": "1.0"},
        "bitbake-config": {"bb-layers": [], "name": "nodistro"},
        "name": "test-workspace",
        "type": "registry",
    }
    (root / "config" / "config-upstream.json").write_text(json.dumps(config))
    (root / "build" / "init-build-env").write_text("")


def test_required_tools_bbsetup_matches_generic_toolset() -> None:
    """bbsetup uses the same toolset as generic - no repo/oe-layertool tools."""
    assert _REQUIRED_TOOLS_BY_FAMILY["bbsetup"] == ("kas-container", "docker", "python3")


def test_run_all_bbsetup_includes_both_bbsetup_checks() -> None:
    """``run_all`` for a bbsetup cfg appends both bbsetup check names."""
    cfg = _bbsetup_cfg(_FIXTURE)
    names = {r.name for r in run_all(cfg)}
    assert "bbsetup-init" in names
    assert "bbsetup-sources" in names


def test_check_bbsetup_config_sources_fails_on_empty_sources(tmp_path: Path) -> None:
    """An empty ``data.sources`` block is a BLOCK failure."""
    _write_bbsetup_workspace(tmp_path, sources={})
    result = check_bbsetup_config_sources(_bbsetup_cfg(tmp_path))
    assert result.status == Status.FAIL
    assert result.severity == Severity.BLOCK


def test_run_all_bbsetup_host_mode_filters_docker_but_keeps_bbsetup_checks(tmp_path: Path) -> None:
    """Host-mode still drops the Docker-dependent checks while the bbsetup
    pre-flight checks remain in the assembled list."""
    _write_bbsetup_workspace(tmp_path, sources={"bitbake": {"git-remote": {"uri": "x"}}})
    cfg = _bbsetup_cfg(tmp_path, host_mode=True)
    names = {r.name for r in run_all(cfg)}
    assert "docker-daemon" not in names
    assert "bbsetup-init" in names


# ---------------------------------------------------------------------------
# PSI support check
# ---------------------------------------------------------------------------


def _psi_cfg(**kwargs) -> BuildConfig:
    """BuildConfig with tuning fields for PSI tests."""
    return BuildConfig(
        workspace=Path("/tmp/fake-workspace"),
        bsp_family="nxp",
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        repo_url="https://example.invalid/none.git",
        repo_branch="scarthgap",
        container_image="jetm/kas-build-env:latest",
        **kwargs,
    )


def test_psi_available_and_configured_is_pass(monkeypatch) -> None:
    """PSI readable + at least one threshold set yields PASS/INFO naming the values."""
    monkeypatch.setattr("bspctl.diagnostics._read_psi_avg10", lambda _r: 12.5)
    cfg = _psi_cfg(pressure_max_cpu=60, pressure_max_io=45, pressure_max_memory=20)

    result = check_psi_support(cfg)

    assert result.status == Status.PASS
    assert result.severity == Severity.INFO
    assert "cpu=60" in result.message


def test_psi_available_unconfigured_is_skip_info(monkeypatch) -> None:
    """PSI readable but no thresholds set yields SKIP/INFO (optional tuning, not a failure)."""
    monkeypatch.setattr("bspctl.diagnostics._read_psi_avg10", lambda _r: 0.0)
    cfg = _psi_cfg()

    result = check_psi_support(cfg)

    assert result.status == Status.SKIP
    assert result.severity == Severity.INFO
    assert "psi-calibrate" in result.message


def test_psi_unavailable_unconfigured_is_skip(monkeypatch) -> None:
    """PSI unreadable + no thresholds set yields SKIP/INFO (silent)."""
    monkeypatch.setattr("bspctl.diagnostics._read_psi_avg10", lambda _r: None)
    cfg = _psi_cfg()

    result = check_psi_support(cfg)

    assert result.status == Status.SKIP
    assert result.severity == Severity.INFO


def test_psi_unavailable_configured_is_fail_warn(monkeypatch) -> None:
    """PSI unreadable + threshold set yields FAIL/WARN (misconfigured host)."""
    monkeypatch.setattr("bspctl.diagnostics._read_psi_avg10", lambda _r: None)
    cfg = _psi_cfg(pressure_max_cpu=60)

    result = check_psi_support(cfg)

    assert result.status == Status.FAIL
    assert result.severity == Severity.WARN


def test_check_psi_support_in_shared_checks_not_docker_checks() -> None:
    """check_psi_support is in SHARED_CHECKS and absent from _DOCKER_CHECKS."""
    assert check_psi_support in SHARED_CHECKS
    assert check_psi_support not in _DOCKER_CHECKS


# ---------------------------------------------------------------------------
# Regression tests for the `except A, B:` Python 2 syntax fix
# ---------------------------------------------------------------------------


def test_check_bitbake_locks_handles_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError reading bitbake.lock must hit the cleanup branch, not crash.

    Before the fix, ``except ValueError, OSError:`` was parsed as Python 2
    syntax (``except ValueError as OSError:``) so an OSError would propagate
    as an unhandled exception. The fix is ``except (ValueError, OSError):``.
    """
    build_dir = tmp_path / "ti" / "build"
    build_dir.mkdir(parents=True)
    lock = build_dir / "bitbake.lock"
    lock.write_text("12345")

    cfg = BuildConfig(
        workspace=tmp_path,
        bsp_family="ti",
        machine="am62x-var-som",
        distro="arago",
        image="var-thin-image",
        manifest="processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
        repo_url="https://example.invalid/none.git",
        repo_branch="scarthgap",
        container_image="jetm/kas-build-env:latest",
    )

    real_read_text = Path.read_text

    def _raising_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == lock:
            raise OSError("simulated I/O failure")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raising_read_text)
    # Stub the cleanup helper - the parallel bug at kas_build.py:319 would
    # otherwise propagate the OSError through and mask the diagnostics fix.
    monkeypatch.setattr(
        "bspctl.steps.kas_build.clear_stale_bitbake_locks",
        lambda _cfg: [lock],
    )

    result = check_bitbake_locks(cfg)

    assert result.status == Status.PASS
    assert result.severity == Severity.BLOCK
    assert "unreadable" in result.message


def test_read_sysctl_returns_none_on_missing_file() -> None:
    """``_read_sysctl`` must return ``None`` for a sysctl key with no proc file.

    Before the fix, ``except FileNotFoundError, ValueError:`` was parsed as
    Python 2 syntax, so only FileNotFoundError was caught and the variable
    ``ValueError`` was shadowed. The fix is ``except (FileNotFoundError, ValueError):``.
    """
    assert _read_sysctl("does.not.exist") is None


def _sysctl_stub(values: dict[str, int | None]):
    """Return a fake ``_read_sysctl`` that looks up ``values`` by key."""

    def _stub(key: str) -> int | None:
        return values.get(key)

    return _stub


def test_check_sysctl_watches_meets_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """When every sysctl key meets its threshold, ``check_sysctl`` returns PASS."""
    monkeypatch.setattr(
        "bspctl.diagnostics._read_sysctl",
        _sysctl_stub(
            {
                "fs.inotify.max_user_instances": 8192,
                "fs.inotify.max_user_watches": 1048576,
                "vm.swappiness": 10,
            }
        ),
    )
    result = check_sysctl(_cfg())
    assert result.status is Status.PASS


def test_check_sysctl_watches_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """A watches value below 524288 must surface as FAIL with the value in the message."""
    monkeypatch.setattr(
        "bspctl.diagnostics._read_sysctl",
        _sysctl_stub(
            {
                "fs.inotify.max_user_instances": 8192,
                "fs.inotify.max_user_watches": 100000,
                "vm.swappiness": 10,
            }
        ),
    )
    result = check_sysctl(_cfg())
    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert "100000" in result.message
    assert "fs.inotify.max_user_watches" in result.message


def test_check_sysctl_watches_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable watches sysctl must surface as FAIL with ``unreadable`` in the message."""
    monkeypatch.setattr(
        "bspctl.diagnostics._read_sysctl",
        _sysctl_stub(
            {
                "fs.inotify.max_user_instances": 8192,
                "fs.inotify.max_user_watches": None,
                "vm.swappiness": 10,
            }
        ),
    )
    result = check_sysctl(_cfg())
    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert "unreadable" in result.message
    assert "fs.inotify.max_user_watches" in result.message


def test_check_git_global_config_both_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both user.email and user.name configured -> PASS at BLOCK severity."""
    responses = {
        "user.email": _mock_run("anon@example.com\n"),
        "user.name": _mock_run("Anon User\n"),
    }

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        return responses[cmd[-1]]

    monkeypatch.setattr("bspctl.diagnostics.subprocess.run", fake_run)
    result = check_git_global_config(_cfg())
    assert result.status is Status.PASS
    assert result.severity is Severity.BLOCK
    assert "anon@example.com" in result.message


def test_check_git_global_config_email_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty user.email stdout -> FAIL at BLOCK with email named in message."""
    responses = {
        "user.email": _mock_run("\n"),
        "user.name": _mock_run("Anon User\n"),
    }

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        return responses[cmd[-1]]

    monkeypatch.setattr("bspctl.diagnostics.subprocess.run", fake_run)
    result = check_git_global_config(_cfg())
    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "user.email" in result.message
    assert result.fix_hint is not None
    assert "user.email" in result.fix_hint


def test_check_git_global_config_name_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-zero return for user.name (git's unset signal) -> FAIL at BLOCK."""
    responses = {
        "user.email": _mock_run("anon@example.com\n"),
        "user.name": _mock_run("", returncode=1),
    }

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        return responses[cmd[-1]]

    monkeypatch.setattr("bspctl.diagnostics.subprocess.run", fake_run)
    result = check_git_global_config(_cfg())
    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "user.name" in result.message
    assert result.fix_hint is not None
    assert "user.name" in result.fix_hint


def test_check_git_global_config_git_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """FileNotFoundError from subprocess.run -> FAIL at BLOCK (git absent)."""

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("git")

    monkeypatch.setattr("bspctl.diagnostics.subprocess.run", fake_run)
    result = check_git_global_config(_cfg())
    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "user.email" in result.message
    assert "user.name" in result.message
