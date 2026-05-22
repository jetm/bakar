"""Unit tests for bspctl.diagnostics.

Focuses on ``check_container_os``: verifies the BLOCK escalation for
container Python 3.13.x and 3.14.x, the PASS path on 3.12 and earlier
3.11/3.10, and the WARN-skip behaviour when docker is unreachable.
``subprocess.run`` is patched so no real container is spawned.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bspctl.config import BuildConfig
from bspctl.diagnostics import (
    Severity,
    Status,
    check_container_os,
    check_host_tools,
    run_all,
)


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
        f"host-mode run_all returned Docker-dependent check names: "
        f"{names & docker_check_names}"
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
