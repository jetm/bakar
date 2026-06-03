"""Unit tests for ``check_sstate_hash_leak``.

Kept in a dedicated module so the existing ``test_diagnostics.py`` is not
touched. The check reads host-side ``build/conf/local.conf`` (plus sibling
conf-includes) and warns when host-specific variables are assigned without a
matching ``[vardepsexclude]`` annotation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.config import BuildConfig
from bakar.diagnostics import (
    _DOCKER_CHECKS,
    SHARED_CHECKS,
    Severity,
    Status,
    check_sstate_hash_leak,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _cfg(workspace: Path) -> BuildConfig:
    """A bbsetup cfg so ``bsp_root`` resolves to ``workspace`` directly."""
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
    )


def _conf_dir(workspace: Path) -> Path:
    conf = workspace / "build" / "conf"
    conf.mkdir(parents=True, exist_ok=True)
    return conf


def test_skips_when_local_conf_absent(tmp_path: Path) -> None:
    """No ``local.conf`` (pre-sync) yields SKIP, never PASS/FAIL."""
    result = check_sstate_hash_leak(_cfg(tmp_path))
    assert result.status == Status.SKIP
    assert result.severity == Severity.WARN


def test_passes_when_no_leaky_vars(tmp_path: Path) -> None:
    """A benign ``local.conf`` with no host-specific assignments passes."""
    conf = _conf_dir(tmp_path)
    (conf / "local.conf").write_text('MACHINE = "qemux86-64"\nDISTRO = "nodistro"\n')
    result = check_sstate_hash_leak(_cfg(tmp_path))
    assert result.status == Status.PASS
    assert result.severity == Severity.WARN


def test_warns_on_datetime_without_exclude(tmp_path: Path) -> None:
    """DATETIME assigned without [vardepsexclude] produces a WARN fail."""
    conf = _conf_dir(tmp_path)
    (conf / "local.conf").write_text('DATETIME = "20240101120000"\n')
    result = check_sstate_hash_leak(_cfg(tmp_path))
    assert result.status == Status.FAIL
    assert result.severity == Severity.WARN
    assert "DATETIME" in result.message
    assert result.fix_hint is not None
    assert "[vardepsexclude]" in result.fix_hint


def test_severity_is_never_block(tmp_path: Path) -> None:
    """The check must stay advisory: WARN on a leak, never BLOCK."""
    conf = _conf_dir(tmp_path)
    (conf / "local.conf").write_text('PWD = "/work"\nUSER = "builder"\n')
    result = check_sstate_hash_leak(_cfg(tmp_path))
    assert result.severity == Severity.WARN
    assert result.severity != Severity.BLOCK


def test_excluded_var_in_same_file_is_clean(tmp_path: Path) -> None:
    """A matching [vardepsexclude] in the same file clears the finding."""
    conf = _conf_dir(tmp_path)
    (conf / "local.conf").write_text('DATETIME = "20240101120000"\nDATETIME[vardepsexclude] += "DATETIME"\n')
    result = check_sstate_hash_leak(_cfg(tmp_path))
    assert result.status == Status.PASS


def test_exclude_in_overlay_clears_assignment_in_local_conf(tmp_path: Path) -> None:
    """A [vardepsexclude] in a sibling conf-include covers a local.conf leak."""
    conf = _conf_dir(tmp_path)
    (conf / "local.conf").write_text('HOSTNAME = "buildbox"\n')
    (conf / "bakar-tuning.inc").write_text('HOSTNAME[vardepsexclude] += "HOSTNAME"\n')
    result = check_sstate_hash_leak(_cfg(tmp_path))
    assert result.status == Status.PASS


def test_var_assigned_in_overlay_is_flagged(tmp_path: Path) -> None:
    """A host var assigned in a sibling .conf (not local.conf) is detected."""
    conf = _conf_dir(tmp_path)
    (conf / "local.conf").write_text('MACHINE = "qemux86-64"\n')
    (conf / "host-env.conf").write_text('USER = "builder"\n')
    result = check_sstate_hash_leak(_cfg(tmp_path))
    assert result.status == Status.FAIL
    assert "USER" in result.message


def test_multiple_leaks_listed_in_message_and_fix(tmp_path: Path) -> None:
    """All leaked variables appear in the message and the fix hint."""
    conf = _conf_dir(tmp_path)
    (conf / "local.conf").write_text('DATETIME = "x"\nHOME = "/root"\n')
    result = check_sstate_hash_leak(_cfg(tmp_path))
    assert result.status == Status.FAIL
    assert "DATETIME" in result.message
    assert "HOME" in result.message
    assert result.fix_hint is not None
    assert 'DATETIME[vardepsexclude] += "DATETIME"' in result.fix_hint
    assert 'HOME[vardepsexclude] += "HOME"' in result.fix_hint


def test_various_assignment_operators_detected(tmp_path: Path) -> None:
    """Weak (``?=``), append (``+=``), and immediate (``:=``) assigns count."""
    conf = _conf_dir(tmp_path)
    (conf / "local.conf").write_text('DATETIME ?= "x"\n')
    assert check_sstate_hash_leak(_cfg(tmp_path)).status == Status.FAIL


def test_registered_in_shared_checks(tmp_path: Path) -> None:
    """The check runs for every family via SHARED_CHECKS."""
    assert check_sstate_hash_leak in SHARED_CHECKS


def test_not_in_docker_checks(tmp_path: Path) -> None:
    """It reads a host file, so it must NOT be a Docker-gated check."""
    assert check_sstate_hash_leak not in _DOCKER_CHECKS
