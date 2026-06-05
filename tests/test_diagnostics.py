"""Unit tests for bakar.diagnostics.

Focuses on ``check_container_os``: verifies the BLOCK escalation for
container Python 3.13.x and 3.14.x, the PASS path on 3.12 and earlier
3.11/3.10, and the WARN-skip behaviour when docker is unreachable.
``subprocess.run`` is patched so no real container is spawned.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bakar.config import BuildConfig
from bakar.diagnostics import (
    _DOCKER_CHECKS,
    _REQUIRED_TOOLS_BY_FAMILY,
    SHARED_CHECKS,
    Severity,
    Status,
    _read_sysctl,
    check_bbsetup_config_sources,
    check_bitbake_locks,
    check_ccache_health,
    check_container_os,
    check_docker_storage_driver,
    check_docker_version,
    check_git_global_config,
    check_host_tools,
    check_kas_yaml_syntax,
    check_psi_support,
    check_sysctl,
    check_workspace_filesystem,
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
    with patch("bakar.diagnostics.subprocess.run", return_value=_mock_run(stdout)):
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
    with patch("bakar.diagnostics.subprocess.run", return_value=_mock_run(stdout)):
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
    with patch("bakar.diagnostics.subprocess.run", return_value=_mock_run(stdout)):
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
        "bakar.diagnostics.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=20),
    ):
        result = check_container_os(_cfg())
    assert result.status == Status.SKIP
    assert result.severity == Severity.WARN


def test_docker_missing_skips_at_warn() -> None:
    with patch(
        "bakar.diagnostics.subprocess.run",
        side_effect=FileNotFoundError("docker"),
    ):
        result = check_container_os(_cfg())
    assert result.status == Status.SKIP
    assert result.severity == Severity.WARN


def test_nonzero_returncode_skips_at_warn() -> None:
    with patch(
        "bakar.diagnostics.subprocess.run",
        return_value=_mock_run("", returncode=1),
    ):
        result = check_container_os(_cfg())
    assert result.status == Status.SKIP
    assert result.severity == Severity.WARN


def test_empty_output_skips_at_warn() -> None:
    with patch(
        "bakar.diagnostics.subprocess.run",
        return_value=_mock_run("\n"),
    ):
        result = check_container_os(_cfg())
    assert result.status == Status.SKIP
    assert result.severity == Severity.WARN


def test_unparseable_python_line_passes() -> None:
    """If the python3 --version line is malformed, fall through to PASS
    rather than producing a false BLOCK."""
    with patch(
        "bakar.diagnostics.subprocess.run",
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
    # Patch subprocess.run so Docker/kas checks return immediately rather than
    # hanging on docker info / image pulls / kas dump in CI environments.
    with patch("bakar.diagnostics.subprocess.run", return_value=_mock_run("")):
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
    monkeypatch.setattr("bakar.diagnostics._read_psi_avg10", lambda _r: 12.5)
    cfg = _psi_cfg(pressure_max_cpu=60, pressure_max_io=45, pressure_max_memory=20)

    result = check_psi_support(cfg)

    assert result.status == Status.PASS
    assert result.severity == Severity.INFO
    assert "cpu=60" in result.message


def test_psi_available_unconfigured_is_skip_info(monkeypatch) -> None:
    """PSI readable but no thresholds set yields SKIP/INFO (optional tuning, not a failure)."""
    monkeypatch.setattr("bakar.diagnostics._read_psi_avg10", lambda _r: 0.0)
    cfg = _psi_cfg()

    result = check_psi_support(cfg)

    assert result.status == Status.SKIP
    assert result.severity == Severity.INFO
    assert "psi_autocalibrate" in result.message


def test_psi_available_autocalibrate_on_does_not_suggest_enabling_it(monkeypatch) -> None:
    """With psi_autocalibrate already on, the SKIP message must not tell the user to set it."""
    monkeypatch.setattr("bakar.diagnostics._read_psi_avg10", lambda _r: 0.0)
    cfg = _psi_cfg(psi_autocalibrate=True)

    result = check_psi_support(cfg)

    assert result.status == Status.SKIP
    assert result.severity == Severity.INFO
    assert "set [build] psi_autocalibrate" not in result.message
    assert "auto-calibration on" in result.message


def test_psi_unavailable_unconfigured_is_skip(monkeypatch) -> None:
    """PSI unreadable + no thresholds set yields SKIP/INFO (silent)."""
    monkeypatch.setattr("bakar.diagnostics._read_psi_avg10", lambda _r: None)
    cfg = _psi_cfg()

    result = check_psi_support(cfg)

    assert result.status == Status.SKIP
    assert result.severity == Severity.INFO


def test_psi_unavailable_configured_is_fail_warn(monkeypatch) -> None:
    """PSI unreadable + threshold set yields FAIL/WARN (misconfigured host)."""
    monkeypatch.setattr("bakar.diagnostics._read_psi_avg10", lambda _r: None)
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
        "bakar.steps.kas_build.clear_stale_bitbake_locks",
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
        "bakar.diagnostics._read_sysctl",
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
        "bakar.diagnostics._read_sysctl",
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
        "bakar.diagnostics._read_sysctl",
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

    monkeypatch.setattr("bakar.diagnostics.subprocess.run", fake_run)
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

    monkeypatch.setattr("bakar.diagnostics.subprocess.run", fake_run)
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

    monkeypatch.setattr("bakar.diagnostics.subprocess.run", fake_run)
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

    monkeypatch.setattr("bakar.diagnostics.subprocess.run", fake_run)
    result = check_git_global_config(_cfg())
    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "user.email" in result.message
    assert "user.name" in result.message


def _kas_yaml_cfg(yaml_path: Path) -> BuildConfig:
    """BuildConfig whose ``kas_yaml`` resolves to ``yaml_path`` via override."""
    return BuildConfig(
        workspace=yaml_path.parent,
        bsp_family="generic",
        machine="qemux86-64",
        distro="poky",
        image="core-image-minimal",
        manifest="kas.yml",
        repo_url="https://example.invalid/none.git",
        repo_branch="scarthgap",
        container_image="jetm/kas-build-env:latest",
        kas_yaml_override=yaml_path,
    )


def test_check_kas_yaml_syntax_missing_file(tmp_path: Path) -> None:
    """No kas YAML on disk -> SKIP at BLOCK severity."""
    missing = tmp_path / "not-generated.yml"
    cfg = _kas_yaml_cfg(missing)
    result = check_kas_yaml_syntax(cfg)
    assert result.status is Status.SKIP
    assert result.severity is Severity.BLOCK
    assert "not yet generated" in result.message
    assert str(missing) in result.message


def test_check_kas_yaml_syntax_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Mocked ``kas dump`` exits 0 -> PASS at BLOCK severity with the path in the message."""
    yaml_path = tmp_path / "kas.yml"
    yaml_path.write_text("header:\n  version: 14\n")
    cfg = _kas_yaml_cfg(yaml_path)
    monkeypatch.setattr(
        "bakar.diagnostics.subprocess.run",
        lambda *a, **kw: _mock_run("", returncode=0),
    )
    monkeypatch.setattr("bakar.diagnostics.shutil.which", lambda _name: "/usr/bin/kas")
    result = check_kas_yaml_syntax(cfg)
    assert result.status is Status.PASS
    assert result.severity is Severity.BLOCK
    assert str(yaml_path) in result.message


def test_check_kas_yaml_syntax_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Mocked ``kas dump`` exits 1 -> FAIL at BLOCK; first stderr line surfaces in message."""
    yaml_path = tmp_path / "broken.yml"
    yaml_path.write_text("header: !!!broken\n")
    cfg = _kas_yaml_cfg(yaml_path)

    def fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="line 5: invalid token\nTraceback dropped\n",
        )

    monkeypatch.setattr("bakar.diagnostics.subprocess.run", fake_run)
    monkeypatch.setattr("bakar.diagnostics.shutil.which", lambda _name: "/usr/bin/kas")
    result = check_kas_yaml_syntax(cfg)
    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "line 5: invalid token" in result.message
    assert result.fix_hint is not None
    assert str(yaml_path) in result.fix_hint


def test_check_kas_yaml_syntax_git_state_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """kas dump exits 1 with a branch/commit error -> SKIP (not FAIL).

    kas validates git history in addition to YAML syntax. A branch/commit
    mismatch is a workspace sync issue, not a YAML problem, and must not
    block the build.
    """
    yaml_path = tmp_path / "qemux86-64.yml"
    yaml_path.write_text("header:\n  version: 14\nmachine: qemux86-64\n")
    cfg = _kas_yaml_cfg(yaml_path)

    def fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                "2026-05-28 09:58:58 - INFO     - kas 5.2 started on CachyOS Linux n/a\n"
                "2026-05-28 09:58:59 - INFO     - Repository bitbake updated\n"
                '2026-05-28 09:58:59 - ERROR    - Branch "2.8-avocado" in repository '
                '"bitbake" does not contain commit "3cc471212"\n'
            ),
        )

    monkeypatch.setattr("bakar.diagnostics.subprocess.run", fake_run)
    monkeypatch.setattr("bakar.diagnostics.shutil.which", lambda _name: "/usr/bin/kas")
    result = check_kas_yaml_syntax(cfg)
    assert result.status is Status.SKIP
    assert result.severity is Severity.BLOCK
    assert "git-state mismatch" in result.message
    assert "does not contain commit" in result.message


def test_check_kas_yaml_syntax_kas_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Container-mode workspace with no host ``kas`` binary -> SKIP at BLOCK."""
    yaml_path = tmp_path / "kas.yml"
    yaml_path.write_text("header:\n  version: 14\n")
    cfg = _kas_yaml_cfg(yaml_path)
    assert cfg.host_mode is False
    monkeypatch.setattr("bakar.diagnostics.shutil.which", lambda _name: None)
    result = check_kas_yaml_syntax(cfg)
    assert result.status is Status.SKIP
    assert result.severity is Severity.BLOCK
    assert "kas binary not on host PATH" in result.message


def _fs_cfg(workspace: Path) -> BuildConfig:
    """BuildConfig pinning ``workspace`` so ``cfg.workspace.resolve()`` is deterministic."""
    return BuildConfig(
        workspace=workspace,
        bsp_family="generic",
        machine="qemux86-64",
        distro="poky",
        image="core-image-minimal",
        manifest="kas.yml",
        repo_url="https://example.invalid/none.git",
        repo_branch="scarthgap",
        container_image="jetm/kas-build-env:latest",
    )


def _patch_proc_mounts(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    """Patch ``Path.read_text`` so reads of ``/proc/mounts`` return ``content``.

    Other paths in the test (``tmp_path``, kas YAML files, etc.) keep their
    real ``read_text`` behavior. ``Path.read_text`` is monkeypatched on the
    class itself, then dispatches on ``self``.
    """
    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(self) == "/proc/mounts":
            return content
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)


def test_check_workspace_filesystem_ext4(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Workspace on ext4 -> PASS at WARN severity, message names the fstype."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    mounts = f"none / overlay rw 0 0\nnone {workspace.resolve()} ext4 rw 0 0\n"
    _patch_proc_mounts(monkeypatch, mounts)
    result = check_workspace_filesystem(_fs_cfg(workspace))
    assert result.status is Status.PASS
    assert result.severity is Severity.WARN
    assert "ext4" in result.message


def test_check_workspace_filesystem_nfs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Workspace on nfs -> FAIL at WARN severity with a fix_hint."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    mounts = f"none / overlay rw 0 0\nserver:/export {workspace.resolve()} nfs rw 0 0\n"
    _patch_proc_mounts(monkeypatch, mounts)
    result = check_workspace_filesystem(_fs_cfg(workspace))
    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert "nfs" in result.message
    assert result.fix_hint is not None
    assert "ext4" in result.fix_hint or "btrfs" in result.fix_hint or "xfs" in result.fix_hint


def test_check_workspace_filesystem_unknown_fs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Workspace on tmpfs -> PASS at WARN with an 'unrecognized' message."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    mounts = f"none / overlay rw 0 0\ntmpfs {workspace.resolve()} tmpfs rw 0 0\n"
    _patch_proc_mounts(monkeypatch, mounts)
    result = check_workspace_filesystem(_fs_cfg(workspace))
    assert result.status is Status.PASS
    assert result.severity is Severity.WARN
    assert "unrecognized" in result.message
    assert "tmpfs" in result.message


def test_check_workspace_filesystem_unreadable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``/proc/mounts`` raises ``OSError`` -> SKIP at WARN severity."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(self) == "/proc/mounts":
            raise OSError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    result = check_workspace_filesystem(_fs_cfg(workspace))
    assert result.status is Status.SKIP
    assert result.severity is Severity.WARN
    assert "unreadable" in result.message


def test_check_docker_version_modern_passes() -> None:
    """Modern Docker (>= 20.10) -> PASS at WARN severity."""
    with patch(
        "bakar.diagnostics.subprocess.run",
        return_value=_mock_run("24.0.5\n"),
    ):
        result = check_docker_version(_cfg())
    assert result.status is Status.PASS
    assert result.severity is Severity.WARN
    assert "24.0.5" in result.message


def test_check_docker_version_old_fails() -> None:
    """Docker older than 20.10 -> FAIL at WARN severity with fix_hint."""
    with patch(
        "bakar.diagnostics.subprocess.run",
        return_value=_mock_run("19.03.15\n"),
    ):
        result = check_docker_version(_cfg())
    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert "19.03.15" in result.message
    assert result.fix_hint is not None


def test_check_docker_version_handles_suffix() -> None:
    """Suffix like ``-ce`` must be stripped before numeric parse."""
    with patch(
        "bakar.diagnostics.subprocess.run",
        return_value=_mock_run("20.10.21-ce\n"),
    ):
        result = check_docker_version(_cfg())
    assert result.status is Status.PASS
    assert result.severity is Severity.WARN


def test_check_docker_version_skips_when_unreachable() -> None:
    """``docker`` binary missing -> SKIP at WARN severity (BLOCK is the daemon check's job)."""
    with patch(
        "bakar.diagnostics.subprocess.run",
        side_effect=FileNotFoundError("docker"),
    ):
        result = check_docker_version(_cfg())
    assert result.status is Status.SKIP
    assert result.severity is Severity.WARN


def test_check_docker_storage_driver_overlay2_passes() -> None:
    """``overlay2`` driver -> PASS at WARN severity."""
    with patch(
        "bakar.diagnostics.subprocess.run",
        return_value=_mock_run("overlay2\n"),
    ):
        result = check_docker_storage_driver(_cfg())
    assert result.status is Status.PASS
    assert result.severity is Severity.WARN
    assert "overlay2" in result.message


def test_check_docker_storage_driver_devicemapper_fails() -> None:
    """``devicemapper`` driver -> FAIL at WARN severity with fix_hint."""
    with patch(
        "bakar.diagnostics.subprocess.run",
        return_value=_mock_run("devicemapper\n"),
    ):
        result = check_docker_storage_driver(_cfg())
    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert "devicemapper" in result.message
    assert result.fix_hint is not None
    assert "overlay2" in result.fix_hint


def test_check_docker_storage_driver_btrfs_fails() -> None:
    """``btrfs`` driver -> FAIL at WARN severity with fix_hint."""
    with patch(
        "bakar.diagnostics.subprocess.run",
        return_value=_mock_run("btrfs\n"),
    ):
        result = check_docker_storage_driver(_cfg())
    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert "btrfs" in result.message
    assert result.fix_hint is not None


def test_check_docker_storage_driver_skips_when_unreachable() -> None:
    """``docker`` binary missing -> SKIP at WARN severity (BLOCK is the daemon check's job)."""
    with patch(
        "bakar.diagnostics.subprocess.run",
        side_effect=FileNotFoundError("docker"),
    ):
        result = check_docker_storage_driver(_cfg())
    assert result.status is Status.SKIP
    assert result.severity is Severity.WARN


def _ccache_cfg(workspace: Path) -> BuildConfig:
    """BuildConfig pinning ``workspace`` so ``cfg.workspace / 'ccache'`` is deterministic."""
    return BuildConfig(
        workspace=workspace,
        bsp_family="generic",
        machine="qemux86-64",
        distro="poky",
        image="core-image-minimal",
        manifest="kas.yml",
        repo_url="https://example.invalid/none.git",
        repo_branch="scarthgap",
        container_image="jetm/kas-build-env:latest",
    )


def test_check_ccache_health_absent(tmp_path: Path) -> None:
    """ccache directory not yet populated -> SKIP at WARN severity."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # No ccache/ subdir created.
    result = check_ccache_health(_ccache_cfg(workspace))
    assert result.status is Status.SKIP
    assert result.severity is Severity.WARN
    assert "absent" in result.message
    assert "populates on first build" in result.message


def test_check_ccache_health_ccache_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ccache directory exists but ``ccache`` binary missing -> SKIP at WARN severity."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "ccache").mkdir()
    monkeypatch.setattr("bakar.diagnostics.shutil.which", lambda _name: None)
    result = check_ccache_health(_ccache_cfg(workspace))
    assert result.status is Status.SKIP
    assert result.severity is Severity.WARN


def test_check_ccache_health_under_threshold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Cache <90% full -> PASS at WARN severity."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "ccache").mkdir()
    monkeypatch.setattr("bakar.diagnostics.shutil.which", lambda _name: "/usr/bin/ccache")
    stats = "cache_size_kibibyte 500000\nmax_size_kibibyte 1000000\n"
    with patch(
        "bakar.diagnostics.subprocess.run",
        return_value=_mock_run(stats),
    ):
        result = check_ccache_health(_ccache_cfg(workspace))
    assert result.status is Status.PASS
    assert result.severity is Severity.WARN
    assert "50%" in result.message


def test_check_ccache_health_at_threshold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Cache >=90% full -> FAIL at WARN severity with a fix_hint."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "ccache").mkdir()
    monkeypatch.setattr("bakar.diagnostics.shutil.which", lambda _name: "/usr/bin/ccache")
    stats = "cache_size_kibibyte 950000\nmax_size_kibibyte 1000000\n"
    with patch(
        "bakar.diagnostics.subprocess.run",
        return_value=_mock_run(stats),
    ):
        result = check_ccache_health(_ccache_cfg(workspace))
    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert "95%" in result.message
    assert result.fix_hint is not None
    assert "--max-size" in result.fix_hint or "ccache -C" in result.fix_hint


def test_check_ccache_health_uncapped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``max_size_kibibyte=0`` (uncapped) -> PASS at WARN without ZeroDivisionError."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "ccache").mkdir()
    monkeypatch.setattr("bakar.diagnostics.shutil.which", lambda _name: "/usr/bin/ccache")
    stats = "cache_size_kibibyte 500000\nmax_size_kibibyte 0\n"
    with patch(
        "bakar.diagnostics.subprocess.run",
        return_value=_mock_run(stats),
    ):
        result = check_ccache_health(_ccache_cfg(workspace))
    assert result.status is Status.PASS
    assert result.severity is Severity.WARN
    assert "uncapped" in result.message


def test_check_ccache_health_modern_max_cache_size_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ccache 4.x renamed max_size_kibibyte -> max_cache_size_kibibyte; both parse."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "ccache").mkdir()
    monkeypatch.setattr("bakar.diagnostics.shutil.which", lambda _name: "/usr/bin/ccache")
    stats = "cache_size_kibibyte 500000\nmax_cache_size_kibibyte 1000000\n"
    with patch(
        "bakar.diagnostics.subprocess.run",
        return_value=_mock_run(stats),
    ):
        result = check_ccache_health(_ccache_cfg(workspace))
    assert result.status is Status.PASS
    assert result.severity is Severity.WARN
    assert "50%" in result.message


def test_check_ccache_health_default_cap_not_configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No cap configured: max_size is ccache's built-in default -> PASS, no false fail."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "ccache").mkdir()
    monkeypatch.setattr("bakar.diagnostics.shutil.which", lambda _name: "/usr/bin/ccache")
    # 9 GiB cached against the 5 GiB built-in default would be 179% "full".
    stats = "cache_size_kibibyte 9437184\nmax_cache_size_kibibyte 5242880\n"
    show_config = "(default) max_size = 5.0 GiB\n"
    with patch(
        "bakar.diagnostics.subprocess.run",
        side_effect=[_mock_run(stats), _mock_run(show_config)],
    ):
        result = check_ccache_health(_ccache_cfg(workspace))
    assert result.status is Status.PASS
    assert result.severity is Severity.WARN
    assert "not configured" in result.message
    assert "%" not in result.message


def test_check_ccache_health_old_ccache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ccache <4.0 lacks the kibibyte keys -> SKIP at WARN severity."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "ccache").mkdir()
    monkeypatch.setattr("bakar.diagnostics.shutil.which", lambda _name: "/usr/bin/ccache")
    stats = "summary: 0 files\n"
    with patch(
        "bakar.diagnostics.subprocess.run",
        return_value=_mock_run(stats),
    ):
        result = check_ccache_health(_ccache_cfg(workspace))
    assert result.status is Status.SKIP
    assert result.severity is Severity.WARN
    assert "missing size keys" in result.message


def test_shared_checks_contains_new_checks() -> None:
    """The six new checks must all be registered in SHARED_CHECKS."""
    from bakar.diagnostics import (
        SHARED_CHECKS,
        check_ccache_health,
        check_docker_storage_driver,
        check_docker_version,
        check_git_global_config,
        check_kas_yaml_syntax,
        check_workspace_filesystem,
    )

    assert check_git_global_config in SHARED_CHECKS
    assert check_kas_yaml_syntax in SHARED_CHECKS
    assert check_workspace_filesystem in SHARED_CHECKS
    assert check_docker_version in SHARED_CHECKS
    assert check_docker_storage_driver in SHARED_CHECKS
    assert check_ccache_health in SHARED_CHECKS


def test_docker_checks_membership_invariant() -> None:
    """Only the two ``docker info``-backed checks belong in _DOCKER_CHECKS.

    The other four new checks exercise host-side resources (git config,
    host kas binary, host /proc/mounts, host ccache) reachable in both
    container and host mode, so they must NOT be filtered out in host
    mode.
    """
    from bakar.diagnostics import (
        _DOCKER_CHECKS,
        check_ccache_health,
        check_docker_storage_driver,
        check_docker_version,
        check_git_global_config,
        check_kas_yaml_syntax,
        check_workspace_filesystem,
    )

    assert check_docker_version in _DOCKER_CHECKS
    assert check_docker_storage_driver in _DOCKER_CHECKS
    assert check_git_global_config not in _DOCKER_CHECKS
    assert check_kas_yaml_syntax not in _DOCKER_CHECKS
    assert check_workspace_filesystem not in _DOCKER_CHECKS
    assert check_ccache_health not in _DOCKER_CHECKS


# ---------------------------------------------------------------------------
# check_hashserv tests
# ---------------------------------------------------------------------------


def _hashserv_cfg(workspace: Path, *, use_hashequiv: bool = True) -> BuildConfig:
    """BuildConfig with cfg.bsp_root = workspace/nxp (NXP family default)."""
    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="imx8mp-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        repo_url="https://example.invalid/repo.git",
        repo_branch="imx-6.6.52-2.2.2",
        container_image="jetm/kas-build-env:5.2-f40",
        use_hashequiv=use_hashequiv,
    )


class _FakeSocket:
    """Minimal socket stand-in: only ``close()`` is exercised by the check."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_check_hashserv_skip_when_disabled(tmp_path: Path) -> None:
    """use_hashequiv=False short-circuits to SKIP / INFO without probing anything."""
    from bakar.diagnostics import check_hashserv

    cfg = _hashserv_cfg(tmp_path, use_hashequiv=False)
    result = check_hashserv(cfg)

    assert result.status == Status.SKIP
    assert result.severity == Severity.INFO
    assert "not configured" in result.message
    assert "[build] hashserv = false" in result.message


def test_check_hashserv_pass_when_running_and_port_listens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """is_running True + port file present + TCP probe succeeds -> PASS / WARN."""
    from bakar import diagnostics
    from bakar.diagnostics import check_hashserv

    bsp_root = tmp_path / "nxp"
    state_dir = bsp_root / ".bakar"
    state_dir.mkdir(parents=True)
    (state_dir / "hashserv.pid").write_text("12345\n")
    (state_dir / "hashserv.port").write_text("50001\n")

    monkeypatch.setattr("bakar.hashserv.is_running", lambda _root: True)
    fake_sock = _FakeSocket()
    monkeypatch.setattr(
        diagnostics.socket,
        "create_connection",
        lambda _addr, timeout=None: fake_sock,
    )

    cfg = _hashserv_cfg(tmp_path, use_hashequiv=True)
    result = check_hashserv(cfg)

    assert result.status == Status.PASS
    assert result.severity == Severity.WARN
    assert "ws://localhost:50001" in result.message
    assert "12345" in result.message
    assert fake_sock.closed is True


def test_check_hashserv_fail_when_configured_but_not_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """use_hashequiv=True + is_running=False -> FAIL / WARN with not-running message."""
    from bakar.diagnostics import check_hashserv

    monkeypatch.setattr("bakar.hashserv.is_running", lambda _root: False)

    cfg = _hashserv_cfg(tmp_path, use_hashequiv=True)
    result = check_hashserv(cfg)

    assert result.status == Status.FAIL
    assert result.severity == Severity.WARN
    assert "not running" in result.message
    assert result.fix_hint is not None
    assert "bakar hashserv start" in result.fix_hint


def test_check_hashserv_fail_when_port_unreachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """is_running True but TCP probe raises ConnectionRefusedError -> FAIL / WARN."""
    from bakar import diagnostics
    from bakar.diagnostics import check_hashserv

    bsp_root = tmp_path / "nxp"
    state_dir = bsp_root / ".bakar"
    state_dir.mkdir(parents=True)
    (state_dir / "hashserv.pid").write_text("99999\n")
    (state_dir / "hashserv.port").write_text("50002\n")

    monkeypatch.setattr("bakar.hashserv.is_running", lambda _root: True)

    def _raise(*_args, **_kwargs):
        raise ConnectionRefusedError("nothing on that port")

    monkeypatch.setattr(diagnostics.socket, "create_connection", _raise)

    cfg = _hashserv_cfg(tmp_path, use_hashequiv=True)
    result = check_hashserv(cfg)

    assert result.status == Status.FAIL
    assert result.severity == Severity.WARN
    assert "TCP probe failed" in result.message
    assert "ws://localhost:50002" in result.message
    assert "99999" in result.message
    assert result.fix_hint == "bakar hashserv stop && bakar hashserv start"


def test_check_hashserv_fail_when_port_file_deleted_mid_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """is_running True but port file absent (concurrent stop) -> FAIL, no exception."""
    from bakar.diagnostics import check_hashserv

    monkeypatch.setattr("bakar.hashserv.is_running", lambda _root: True)

    cfg = _hashserv_cfg(tmp_path, use_hashequiv=True)
    # State dir does not exist; port file read will raise FileNotFoundError.
    result = check_hashserv(cfg)

    assert result.status == Status.FAIL
    assert result.severity == Severity.WARN
    assert "not running" in result.message
    assert result.fix_hint is not None
    assert "bakar hashserv start" in result.fix_hint


def test_check_hashserv_in_shared_not_in_docker() -> None:
    """check_hashserv runs everywhere - the daemon is host-side in both modes."""
    from bakar.diagnostics import (
        _DOCKER_CHECKS,
        SHARED_CHECKS,
        check_hashserv,
    )

    assert check_hashserv in SHARED_CHECKS
    assert check_hashserv not in _DOCKER_CHECKS


_TUNING_LINES = """\
BB_NUMBER_THREADS = "${@os.environ.get('NPROC', '16')}"
PARALLEL_MAKE = "-j ${@os.environ.get('NPROC', '16')}"
BB_NUMBER_PARSE_THREADS = "${@os.environ.get('NPROC', '16')}"
"""


class TestCheckNproc:
    def _cfg_with_local_conf(self, tmp_path: Path, content: str | None) -> BuildConfig:
        cfg = dataclasses.replace(_cfg(), workspace=tmp_path)
        if content is not None:
            conf_dir = tmp_path / "ti" / "build" / "conf"
            conf_dir.mkdir(parents=True)
            (conf_dir / "local.conf").write_text(content)
        return cfg

    def test_env_value_reports_derived_thread_settings(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from bakar.diagnostics import check_nproc

        monkeypatch.setenv("NPROC", "33")
        result = check_nproc(self._cfg_with_local_conf(tmp_path, None))
        assert result.status is Status.PASS
        assert result.severity is Severity.INFO
        assert "NPROC=33 (from environment)" in result.message
        assert "33 bitbake tasks, 33 parse threads, make -j 33" in result.message

    def test_auto_detected_reports_cpu_count(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from bakar.diagnostics import check_nproc

        monkeypatch.delenv("NPROC", raising=False)
        monkeypatch.setattr("bakar.diagnostics.os.cpu_count", lambda: 8)
        result = check_nproc(self._cfg_with_local_conf(tmp_path, None))
        assert "NPROC=8 (auto-detected; override with $NPROC)" in result.message
        assert "8 bitbake tasks, 8 parse threads, make -j 8" in result.message

    def test_user_override_after_tuning_section_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from bakar.diagnostics import check_nproc

        monkeypatch.setenv("NPROC", "33")
        conf = _TUNING_LINES + 'BB_NUMBER_THREADS = "8"\nPARALLEL_MAKE = "-j 4"\n'
        result = check_nproc(self._cfg_with_local_conf(tmp_path, conf))
        assert "8 (local.conf override) bitbake tasks" in result.message
        assert "33 parse threads" in result.message
        assert "make -j 4 (local.conf override)" in result.message

    def test_user_assignment_before_tuning_section_is_not_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from bakar.diagnostics import check_nproc

        monkeypatch.setenv("NPROC", "33")
        conf = 'BB_NUMBER_THREADS = "8"\n' + _TUNING_LINES
        result = check_nproc(self._cfg_with_local_conf(tmp_path, conf))
        assert "override" not in result.message
        assert "33 bitbake tasks, 33 parse threads, make -j 33" in result.message

    def test_weak_and_append_operators_are_not_overrides(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from bakar.diagnostics import check_nproc

        monkeypatch.setenv("NPROC", "33")
        conf = _TUNING_LINES + 'BB_NUMBER_THREADS ?= "8"\nPARALLEL_MAKE += "-l 50"\n'
        result = check_nproc(self._cfg_with_local_conf(tmp_path, conf))
        assert "override" not in result.message

    def test_commented_assignment_is_not_an_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from bakar.diagnostics import check_nproc

        monkeypatch.setenv("NPROC", "33")
        conf = _TUNING_LINES + '# BB_NUMBER_THREADS = "8"\n'
        result = check_nproc(self._cfg_with_local_conf(tmp_path, conf))
        assert "override" not in result.message
