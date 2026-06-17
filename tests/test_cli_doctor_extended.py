"""Extended CliRunner tests for ``bakar doctor``.

Patches ``bakar.commands.doctor.run_all`` with a fixed list of
``CheckResult`` objects to exercise the rendering and exit-code paths
of the doctor command without invoking real diagnostic checks (which
need docker, kas, /proc/pressure, etc.).

Companion to ``tests/test_cli_doctor.py``, which covers the pure
``_psi_recommendation`` helper and the PSI-unavailable early exit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from bakar.cli import app
from bakar.diagnostics import CheckResult, Severity, Status

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def nxp_workspace(tmp_path: Path) -> Path:
    """Workspace with ``.bakar.toml`` marker and an ``nxp/`` subdir.

    Doctor walks up from cwd looking for the marker file (or an nxp/ti
    subdir) when neither ``--workspace`` nor ``--manifest`` is passed.
    Tests invoke doctor with ``--workspace <this>`` and ``--manifest``
    so dispatch is deterministic regardless of pytest's cwd.
    """
    (tmp_path / ".bakar.toml").write_text("")
    (tmp_path / "nxp").mkdir()
    return tmp_path


def _invoke_doctor(workspace: Path, *extra: str):
    """Invoke ``bakar doctor`` with NXP dispatch pinned to the default manifest.

    Passing ``--manifest`` forces the dispatcher down ``_dispatch_bsp``
    rather than the bbsetup-detection branch, so the test does not
    depend on the cwd shape.
    """
    return runner.invoke(
        app,
        [
            "doctor",
            "--workspace",
            str(workspace),
            "--manifest",
            "imx-6.6.52-2.2.2.xml",
            *extra,
        ],
    )


# ---------------------------------------------------------------------------
# Rendering and exit-code paths
# ---------------------------------------------------------------------------


def test_all_pass_exits_zero(nxp_workspace: Path) -> None:
    """All-PASS results exit 0 with the ``N/N checks passed`` summary.

    The all-PASS branch in ``_print_diagnosis`` skips the per-check
    table and prints just the count, so individual check names do not
    appear in the rendered output. ``test_multiple_checks_all_rendered``
    covers the per-name rendering branch (FAIL/SKIP present).
    """
    results = [
        CheckResult(
            name="host-tools",
            severity=Severity.BLOCK,
            status=Status.PASS,
            message="ok",
        ),
        CheckResult(
            name="docker-daemon",
            severity=Severity.BLOCK,
            status=Status.PASS,
            message="server v25",
        ),
    ]
    with patch("bakar.commands.doctor.run_all", return_value=results):
        result = _invoke_doctor(nxp_workspace)

    assert result.exit_code == 0, result.output
    assert "2/2 checks passed" in result.output


def test_warn_only_exits_zero(nxp_workspace: Path) -> None:
    """A WARN+FAIL result exits 0 and the warning is rendered."""
    results = [
        CheckResult(
            name="sysctl",
            severity=Severity.WARN,
            status=Status.FAIL,
            message="fs.inotify.max_user_watches=8192 (<524288)",
            fix_hint="bump inotify watches",
        ),
    ]
    with patch("bakar.commands.doctor.run_all", return_value=results):
        result = _invoke_doctor(nxp_workspace)

    assert result.exit_code == 0, result.output
    assert "sysctl" in result.output
    assert "WARN" in result.output
    assert "FAIL" in result.output


def test_block_fail_exits_nonzero(nxp_workspace: Path) -> None:
    """A BLOCK+FAIL result exits non-zero with name and fix_hint visible."""
    results = [
        CheckResult(
            name="docker-daemon",
            severity=Severity.BLOCK,
            status=Status.FAIL,
            message="not reachable",
            fix_hint="sudo systemctl start docker",
        ),
    ]
    with patch("bakar.commands.doctor.run_all", return_value=results):
        result = _invoke_doctor(nxp_workspace)

    assert result.exit_code != 0
    assert result.exit_code == 2
    assert "docker-daemon" in result.output
    assert "BLOCK" in result.output
    assert "sudo systemctl start docker" in result.output


def test_multiple_checks_all_rendered(nxp_workspace: Path) -> None:
    """A mixed-severity list renders every name in the table."""
    results = [
        CheckResult(
            name="host-tools",
            severity=Severity.BLOCK,
            status=Status.PASS,
            message="present",
        ),
        CheckResult(
            name="sysctl",
            severity=Severity.WARN,
            status=Status.FAIL,
            message="inotify low",
            fix_hint="raise inotify limits",
        ),
        CheckResult(
            name="git-cache",
            severity=Severity.INFO,
            status=Status.PASS,
            message="500M across 12 repos",
        ),
        CheckResult(
            name="bitbake-override",
            severity=Severity.INFO,
            status=Status.SKIP,
            message="BAKAR_BITBAKE_OVERRIDE=0",
        ),
    ]
    with patch("bakar.commands.doctor.run_all", return_value=results):
        result = _invoke_doctor(nxp_workspace)

    # No BLOCK FAIL in the mix -> exit 0.
    assert result.exit_code == 0, result.output
    for name in ("host-tools", "sysctl", "git-cache", "bitbake-override"):
        assert name in result.output, f"missing {name!r} in output: {result.output}"


def test_skip_results_do_not_affect_exit_code(nxp_workspace: Path) -> None:
    """SKIP results render but exit code is driven solely by BLOCK FAIL counts."""
    results = [
        CheckResult(
            name="container-bitbake",
            severity=Severity.INFO,
            status=Status.SKIP,
            message="could not inspect: docker missing",
        ),
        CheckResult(
            name="psi_support",
            severity=Severity.INFO,
            status=Status.SKIP,
            message="PSI not available on this kernel",
        ),
        CheckResult(
            name="manifest",
            severity=Severity.INFO,
            status=Status.SKIP,
            message=".repo/ missing (first run)",
        ),
    ]
    with patch("bakar.commands.doctor.run_all", return_value=results):
        result = _invoke_doctor(nxp_workspace)

    assert result.exit_code == 0, result.output
    assert "container-bitbake" in result.output
    assert "psi_support" in result.output
    assert "manifest" in result.output
    assert "SKIP" in result.output


def test_block_fail_among_passes_still_exits_nonzero(nxp_workspace: Path) -> None:
    """One BLOCK FAIL among many PASSes still drives a non-zero exit."""
    results = [
        CheckResult(
            name="host-tools",
            severity=Severity.BLOCK,
            status=Status.PASS,
            message="present",
        ),
        CheckResult(
            name="disk-free",
            severity=Severity.BLOCK,
            status=Status.FAIL,
            message="workspace free=10G",
            fix_hint="free up space",
        ),
        CheckResult(
            name="memory",
            severity=Severity.WARN,
            status=Status.PASS,
            message="32G available",
        ),
    ]
    with patch("bakar.commands.doctor.run_all", return_value=results):
        result = _invoke_doctor(nxp_workspace)

    assert result.exit_code == 2
    assert "disk-free" in result.output
    assert "free up space" in result.output


# ---------------------------------------------------------------------------
# Flag handling
# ---------------------------------------------------------------------------


def test_kas_yaml_and_manifest_are_mutually_exclusive(nxp_workspace: Path, tmp_path: Path) -> None:
    """Passing both a positional kas YAML and --manifest exits 2 with a message."""
    kas_yaml = tmp_path / "build.yml"
    kas_yaml.write_text("header:\n  version: 14\n")

    result = runner.invoke(
        app,
        [
            "doctor",
            str(kas_yaml),
            "--manifest",
            "imx-6.6.52-2.2.2.xml",
            "--workspace",
            str(nxp_workspace),
        ],
    )

    assert result.exit_code == 2
    assert "either a positional kas YAML or --manifest" in result.output


# ---------------------------------------------------------------------------
# --json flag
# ---------------------------------------------------------------------------


def test_json_all_pass_exits_zero_and_valid_json(nxp_workspace: Path) -> None:
    """All-PASS results with --json: valid JSON, version==1, exit 0."""
    import json

    results = [
        CheckResult(
            name="host-tools",
            severity=Severity.BLOCK,
            status=Status.PASS,
            message="all present",
        ),
        CheckResult(
            name="docker-daemon",
            severity=Severity.BLOCK,
            status=Status.PASS,
            message="server v25",
        ),
    ]
    with patch("bakar.commands.doctor.run_all", return_value=results):
        result = _invoke_doctor(nxp_workspace, "--json")

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["version"] == 1
    assert isinstance(data["findings"], list)
    assert len(data["findings"]) == 2


def test_json_finding_has_all_five_keys(nxp_workspace: Path) -> None:
    """Each finding object has exactly the five required keys."""
    import json

    results = [
        CheckResult(
            name="sysctl",
            severity=Severity.WARN,
            status=Status.FAIL,
            message="inotify.max_user_watches low",
            fix_hint="raise inotify limits",
        ),
    ]
    with patch("bakar.commands.doctor.run_all", return_value=results):
        result = _invoke_doctor(nxp_workspace, "--json")

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    finding = data["findings"][0]
    assert set(finding.keys()) == {"check", "severity", "status", "message", "fix_hint"}
    assert finding["check"] == "sysctl"
    assert finding["severity"] == "WARN"
    assert finding["status"] == "FAIL"
    assert finding["message"] == "inotify.max_user_watches low"
    assert finding["fix_hint"] == "raise inotify limits"


def test_json_fix_hint_null_when_none(nxp_workspace: Path) -> None:
    """fix_hint is null in JSON (not absent) when CheckResult.fix_hint is None."""
    import json

    results = [
        CheckResult(
            name="host-tools",
            severity=Severity.BLOCK,
            status=Status.PASS,
            message="present",
            fix_hint=None,
        ),
    ]
    with patch("bakar.commands.doctor.run_all", return_value=results):
        result = _invoke_doctor(nxp_workspace, "--json")

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    finding = data["findings"][0]
    # fix_hint key must be present and explicitly null, not absent
    assert "fix_hint" in finding
    assert finding["fix_hint"] is None


def test_json_block_fail_exits_2(nxp_workspace: Path) -> None:
    """BLOCK+FAIL result with --json exits 2 and still produces valid JSON."""
    import json

    results = [
        CheckResult(
            name="docker-daemon",
            severity=Severity.BLOCK,
            status=Status.FAIL,
            message="not reachable",
            fix_hint="sudo systemctl start docker",
        ),
    ]
    with patch("bakar.commands.doctor.run_all", return_value=results):
        result = _invoke_doctor(nxp_workspace, "--json")

    assert result.exit_code == 2
    data = json.loads(result.output)
    assert data["version"] == 1
    assert len(data["findings"]) == 1
    assert data["findings"][0]["status"] == "FAIL"
    assert data["findings"][0]["severity"] == "BLOCK"
    assert data["findings"][0]["fix_hint"] == "sudo systemctl start docker"


def test_json_warn_fail_exits_zero(nxp_workspace: Path) -> None:
    """WARN+FAIL with --json exits 0 (only BLOCK failures drive exit 2)."""
    import json

    results = [
        CheckResult(
            name="sysctl",
            severity=Severity.WARN,
            status=Status.FAIL,
            message="inotify low",
        ),
    ]
    with patch("bakar.commands.doctor.run_all", return_value=results):
        result = _invoke_doctor(nxp_workspace, "--json")

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["findings"][0]["fix_hint"] is None
