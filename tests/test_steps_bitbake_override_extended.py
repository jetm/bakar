"""Extended unit tests for bakar.steps.bitbake_override.

The companion ``test_bitbake_override.py`` exercises the happy-path
state machine with a real-git fixture. This file covers the remainder
of the module surface that the original suite leaves untested:

* the ``BAKAR_BITBAKE_OVERRIDE=0`` short-circuit on ``apply`` (with
  observable side effects: no clone, no symlink swap).
* branch resolution precedence (``--branch`` > env var > auto).
* override-repo path resolution from ``BAKAR_BITBAKE_OVERRIDE_REPO``.
* ``revert()`` removing the symlink and the no-op branch.
* ``status()`` stale-detail branches (real dir, symlink-elsewhere,
  non-existent path).
* internal helpers used by the parser-compat check
  (``_override_directive_arity``, ``_scan_directive_calls``,
  ``_check_parser_compat``, ``_read_bb_version`` OSError path,
  ``_is_correct_symlink``).

Tests are hermetic: subprocess.run is patched (so no git is invoked)
and the on-disk fixtures live under ``tmp_path``.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from bakar.config import BuildConfig
from bakar.steps import bitbake_override as bbo

pytestmark = pytest.mark.unit

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _cfg(workspace: Path, family: str = "nxp") -> BuildConfig:
    """Construct a BuildConfig matching the existing test suite's shape."""
    if family == "ti":
        return BuildConfig(
            workspace=workspace,
            bsp_family="ti",
            machine="am62x-var-som",
            distro="arago",
            image="var-thin-image",
            manifest="processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
            repo_url="https://example.invalid/none.git",
            repo_branch="scarthgap",
            container_image="jetm/kas-build-env:latest",
        )
    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",
        machine="imx95-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.6.52-2.2.2.xml",
        repo_url="https://example.invalid/none.git",
        repo_branch="scarthgap",
        container_image="jetm/kas-build-env:latest",
    )


def _write_bsp_bitbake(cfg: BuildConfig, version: str) -> Path:
    """Build the BSP-bundled bitbake tree with ``__version__`` set."""
    bsp_bitbake = cfg.bsp_bitbake_path
    init_py = bsp_bitbake / "lib" / "bb" / "__init__.py"
    init_py.parent.mkdir(parents=True, exist_ok=True)
    init_py.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    return bsp_bitbake


def _fake_git_ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["git"], returncode=0, stdout=stdout, stderr="")


# ---------------------------------------------------------------------------
# BAKAR_BITBAKE_OVERRIDE=0 short-circuit (line 466-468)
# ---------------------------------------------------------------------------


def test_apply_disabled_env_is_noop_no_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With BAKAR_BITBAKE_OVERRIDE=0, apply must not touch git or filesystem."""
    monkeypatch.setenv("BAKAR_BITBAKE_OVERRIDE", "0")

    cfg = _cfg(tmp_path, "nxp")
    bsp_bitbake = _write_bsp_bitbake(cfg, "2.8.0")

    # Any subprocess call here is a failure: apply should bail before _git.
    fake_run = MagicMock(side_effect=AssertionError("subprocess.run must not be called"))
    monkeypatch.setattr(bbo.subprocess, "run", fake_run)

    result = bbo.apply(cfg, log=None)

    assert result.state == "disabled"
    assert fake_run.call_count == 0
    # Marker (the symlink) was not created; the real dir is intact.
    assert bsp_bitbake.is_dir()
    assert not bsp_bitbake.is_symlink()
    assert not (cfg.bsp_root / "upstream-bitbake").exists()


def test_apply_disabled_logs_step_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled apply with a log emits step_skip with the env-var reason."""
    monkeypatch.setenv("BAKAR_BITBAKE_OVERRIDE", "0")

    cfg = _cfg(tmp_path, "nxp")
    _write_bsp_bitbake(cfg, "2.8.0")

    log = MagicMock()
    bbo.apply(cfg, log=log)

    log.step_skip.assert_called_once()
    args, kwargs = log.step_skip.call_args
    assert args[0] == "bitbake_override"
    assert kwargs.get("reason") == "BAKAR_BITBAKE_OVERRIDE=0"


# ---------------------------------------------------------------------------
# Full apply -> status -> revert cycle (subprocess patched)
# ---------------------------------------------------------------------------


def test_apply_creates_symlink_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A patched-git apply must create the upstream symlink (the marker)."""
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE", raising=False)
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE_BRANCH", raising=False)

    cfg = _cfg(tmp_path, "nxp")
    bsp_bitbake = _write_bsp_bitbake(cfg, "2.8.0")

    # Simulate the source repo existing on disk (clone source) and the
    # post-clone upstream-bitbake checkout. _ensure_clone is patched out
    # so it does not actually clone; instead we populate the upstream-bitbake
    # tree by hand to mimic a successful clone.
    source_repo = tmp_path / "source-bb"
    source_repo.mkdir()

    def fake_ensure_clone(upstream_dir: Path, src: Path, branch: str) -> None:
        # mirror what a real clone produces: a directory with bb version info
        init_py = upstream_dir / "lib" / "bb" / "__init__.py"
        init_py.parent.mkdir(parents=True, exist_ok=True)
        init_py.write_text('__version__ = "2.8.1"\n', encoding="utf-8")

    monkeypatch.setattr(bbo, "_ensure_clone", fake_ensure_clone)
    monkeypatch.setattr(bbo, "_head_sha", lambda _p: "abc1234")

    result = bbo.apply(cfg, log=None, branch="br-2.8", repo_path=source_repo)

    assert result.state == "active"
    # The "marker" is the symlink swap:
    assert bsp_bitbake.is_symlink()
    assert os.readlink(bsp_bitbake) == "../../upstream-bitbake"
    assert (cfg.bsp_root / "upstream-bitbake").is_dir()
    assert result.branch == "br-2.8"
    assert result.sha == "abc1234"
    assert result.upstream_version == "2.8.1"
    assert result.bsp_version == "2.8.0"


def test_revert_removes_symlink_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """revert() removes the symlink installed by apply()."""
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE", raising=False)

    cfg = _cfg(tmp_path, "nxp")
    bsp_bitbake = cfg.bsp_bitbake_path
    upstream_dir = cfg.bsp_root / "upstream-bitbake"
    upstream_dir.mkdir(parents=True)

    # Synthesise "applied" state: symlink in place of the BSP dir.
    bsp_bitbake.parent.mkdir(parents=True, exist_ok=True)
    bsp_bitbake.symlink_to("../../upstream-bitbake")
    assert bsp_bitbake.is_symlink()  # precondition

    log = MagicMock()
    bbo.revert(cfg, log=log)

    assert not bsp_bitbake.exists()  # the marker (symlink) is gone
    log.info.assert_called_once()


def test_revert_noop_when_not_a_symlink(
    tmp_path: Path,
) -> None:
    """revert() on a non-symlink path logs an info message and exits."""
    cfg = _cfg(tmp_path, "nxp")
    bsp_bitbake = _write_bsp_bitbake(cfg, "2.8.0")
    assert bsp_bitbake.is_dir() and not bsp_bitbake.is_symlink()  # precondition

    log = MagicMock()
    bbo.revert(cfg, log=log)

    # Real dir was not touched.
    assert bsp_bitbake.is_dir()
    assert not bsp_bitbake.is_symlink()
    log.info.assert_called_once()
    assert "not a symlink" in log.info.call_args.args[0]


def test_revert_without_log_does_not_raise(
    tmp_path: Path,
) -> None:
    """revert() with log=None must not raise on either branch."""
    cfg = _cfg(tmp_path, "nxp")
    bsp_bitbake = cfg.bsp_bitbake_path
    bsp_bitbake.parent.mkdir(parents=True)
    bsp_bitbake.symlink_to("../../upstream-bitbake")

    bbo.revert(cfg, log=None)  # symlink path, no log
    assert not bsp_bitbake.exists()

    _write_bsp_bitbake(cfg, "2.8.0")
    bbo.revert(cfg, log=None)  # non-symlink path, no log
    # Real dir untouched.
    assert bsp_bitbake.is_dir()


# ---------------------------------------------------------------------------
# Branch resolution (lines 142-154)
# ---------------------------------------------------------------------------


def test_resolve_branch_uses_explicit_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``override`` arg wins over env and auto-detect."""
    monkeypatch.setenv("BAKAR_BITBAKE_OVERRIDE_BRANCH", "env-branch")
    cfg = _cfg(tmp_path, "nxp")
    _write_bsp_bitbake(cfg, "2.10.0")

    assert bbo.resolve_branch(cfg, override="mybranch") == "mybranch"


def test_resolve_branch_uses_env_var(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without override arg, BAKAR_BITBAKE_OVERRIDE_BRANCH is used."""
    monkeypatch.setenv("BAKAR_BITBAKE_OVERRIDE_BRANCH", "mybranch")
    cfg = _cfg(tmp_path, "nxp")
    _write_bsp_bitbake(cfg, "2.10.0")

    assert bbo.resolve_branch(cfg) == "mybranch"


def test_resolve_branch_auto_from_bsp_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-detect reads ``__version__`` and emits ``br-<major>.<minor>``."""
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE_BRANCH", raising=False)
    cfg = _cfg(tmp_path, "nxp")
    _write_bsp_bitbake(cfg, "2.12.5")

    assert bbo.resolve_branch(cfg) == "br-2.12"


def test_resolve_branch_auto_falls_back_to_upstream_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When BSP tree is absent, auto reads from the upstream clone instead."""
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE_BRANCH", raising=False)
    cfg = _cfg(tmp_path, "nxp")
    upstream = cfg.bsp_root / "upstream-bitbake"
    init_py = upstream / "lib" / "bb" / "__init__.py"
    init_py.parent.mkdir(parents=True, exist_ok=True)
    init_py.write_text('__version__ = "2.14.0"\n', encoding="utf-8")

    assert bbo.resolve_branch(cfg) == "br-2.14"


def test_resolve_branch_raises_when_no_version_anywhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No BSP tree and no clone: auto-detect fails with RuntimeError."""
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE_BRANCH", raising=False)
    cfg = _cfg(tmp_path, "nxp")
    cfg.bsp_root.mkdir(parents=True, exist_ok=True)

    with pytest.raises(RuntimeError, match="could not auto-detect"):
        bbo.resolve_branch(cfg)


def test_resolve_branch_handles_single_segment_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A version string without a minor segment is returned as-is."""
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE_BRANCH", raising=False)
    cfg = _cfg(tmp_path, "nxp")
    bsp_bitbake = cfg.bsp_bitbake_path
    init = bsp_bitbake / "lib" / "bb" / "__init__.py"
    init.parent.mkdir(parents=True, exist_ok=True)
    # _VERSION_RE requires \d+\.\d+ so we exercise _major_minor's fallback
    # branch separately by hitting it directly.
    assert bbo._major_minor("2") == "2"


# ---------------------------------------------------------------------------
# Override repo path resolution (lines 98-100)
# ---------------------------------------------------------------------------


def test_override_repo_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the env var, the default path is returned."""
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE_REPO", raising=False)
    assert bbo._override_repo() == bbo.DEFAULT_OVERRIDE_REPO


def test_override_repo_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """BAKAR_BITBAKE_OVERRIDE_REPO overrides the default path."""
    custom = tmp_path / "custom-bb-repo"
    monkeypatch.setenv("BAKAR_BITBAKE_OVERRIDE_REPO", str(custom))
    assert bbo._override_repo() == custom


def test_override_repo_env_var_expanduser(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env var is expanded with ~ -> $HOME."""
    monkeypatch.setenv("BAKAR_BITBAKE_OVERRIDE_REPO", "~/custom-bb")
    result = bbo._override_repo()
    assert str(result).startswith(os.path.expanduser("~"))
    assert result.name == "custom-bb"


# ---------------------------------------------------------------------------
# status() stale-detail branches (lines 434-439)
# ---------------------------------------------------------------------------


def test_status_stale_real_dir_branch(tmp_path: Path) -> None:
    """A real BSP dir, no symlink => stale with the 'real directory' detail."""
    cfg = _cfg(tmp_path, "nxp")
    _write_bsp_bitbake(cfg, "2.8.0")
    st = bbo.status(cfg)
    assert st.state == "stale"
    assert "real directory" in st.detail


def test_status_stale_symlink_elsewhere(tmp_path: Path) -> None:
    """A symlink that points to the wrong place is reported as stale."""
    cfg = _cfg(tmp_path, "nxp")
    bsp_bitbake = cfg.bsp_bitbake_path
    bsp_bitbake.parent.mkdir(parents=True)
    bsp_bitbake.symlink_to("/nonexistent/path")
    # upstream-bitbake also missing -> "missing" branch wins, so create it
    (cfg.bsp_root / "upstream-bitbake").mkdir()

    st = bbo.status(cfg)
    assert st.state == "stale"
    assert "symlink" in st.detail


def test_status_disabled_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BAKAR_BITBAKE_OVERRIDE=0 short-circuits status() to 'disabled'."""
    monkeypatch.setenv("BAKAR_BITBAKE_OVERRIDE", "0")
    cfg = _cfg(tmp_path, "nxp")
    _write_bsp_bitbake(cfg, "2.8.0")

    st = bbo.status(cfg)
    assert st.state == "disabled"
    assert st.bsp_version == "2.8.0"
    assert st.detail == "BAKAR_BITBAKE_OVERRIDE=0"


# ---------------------------------------------------------------------------
# Internal helpers: _read_bb_version, _is_correct_symlink, _swap_to_symlink
# ---------------------------------------------------------------------------


def test_read_bb_version_missing_file_returns_none(tmp_path: Path) -> None:
    """OSError reading __init__.py yields None, not an exception."""
    # Path that has no lib/bb/__init__.py:
    assert bbo._read_bb_version(tmp_path) is None


def test_read_bb_version_no_version_in_file_returns_none(tmp_path: Path) -> None:
    """A file present but without __version__ yields None."""
    init = tmp_path / "lib" / "bb" / "__init__.py"
    init.parent.mkdir(parents=True)
    init.write_text("# no version here\n", encoding="utf-8")
    assert bbo._read_bb_version(tmp_path) is None


def test_is_correct_symlink_false_for_real_dir(tmp_path: Path) -> None:
    """A real directory is not a correct symlink."""
    cfg = _cfg(tmp_path, "nxp")
    _write_bsp_bitbake(cfg, "2.8.0")
    upstream = cfg.bsp_root / "upstream-bitbake"
    upstream.mkdir(parents=True)
    assert not bbo._is_correct_symlink(cfg.bsp_bitbake_path, upstream)


def test_is_correct_symlink_true_after_swap(tmp_path: Path) -> None:
    """A correctly pointing symlink to an existing dir returns True."""
    cfg = _cfg(tmp_path, "nxp")
    upstream = cfg.bsp_root / "upstream-bitbake"
    upstream.mkdir(parents=True)
    poky_bb = cfg.bsp_bitbake_path
    poky_bb.parent.mkdir(parents=True)
    poky_bb.symlink_to("../../upstream-bitbake")
    assert bbo._is_correct_symlink(poky_bb, upstream)


def test_swap_to_symlink_fresh_returns_linked_fresh(tmp_path: Path) -> None:
    """A symlink already pointing correctly yields 'linked-fresh' no-op."""
    cfg = _cfg(tmp_path, "nxp")
    upstream = cfg.bsp_root / "upstream-bitbake"
    upstream.mkdir(parents=True)
    poky_bb = cfg.bsp_bitbake_path
    poky_bb.parent.mkdir(parents=True)
    poky_bb.symlink_to("../../upstream-bitbake")

    action, bsp_version = bbo._swap_to_symlink(poky_bb, upstream)
    assert action == "linked-fresh"
    assert bsp_version is None


def test_swap_to_symlink_broken_replaces(tmp_path: Path) -> None:
    """An existing symlink to the wrong place is replaced with the right one."""
    cfg = _cfg(tmp_path, "nxp")
    upstream = cfg.bsp_root / "upstream-bitbake"
    upstream.mkdir(parents=True)
    poky_bb = cfg.bsp_bitbake_path
    poky_bb.parent.mkdir(parents=True)
    poky_bb.symlink_to("/some/other/place")

    action, bsp_version = bbo._swap_to_symlink(poky_bb, upstream)
    assert action == "linked-broken"
    assert bsp_version is None
    assert poky_bb.is_symlink()
    assert os.readlink(poky_bb) == "../../upstream-bitbake"


# ---------------------------------------------------------------------------
# _override_directive_arity, _scan_directive_calls (lines 273-297)
# ---------------------------------------------------------------------------


def test_override_directive_arity_missing_file(tmp_path: Path) -> None:
    """Non-existent handler file returns None."""
    assert bbo._override_directive_arity(tmp_path / "absent.py", "addfragments") is None


def test_override_directive_arity_directive_present(tmp_path: Path) -> None:
    """A regex with two ``(.+)`` groups reports arity 2."""
    handler = tmp_path / "ConfHandler.py"
    handler.write_text(
        '__addfragments_regexp__ = re.compile(r"addfragments\\s+(.+)\\s+(.+)")\n',
        encoding="utf-8",
    )
    assert bbo._override_directive_arity(handler, "addfragments") == 2


def test_override_directive_arity_directive_absent(tmp_path: Path) -> None:
    """A handler without the target directive returns None."""
    handler = tmp_path / "ConfHandler.py"
    handler.write_text(
        '__include_regexp__ = re.compile(r"include\\s+(.+)")\n',
        encoding="utf-8",
    )
    assert bbo._override_directive_arity(handler, "addfragments") is None


def test_scan_directive_calls_missing_file(tmp_path: Path) -> None:
    """Non-existent conf file returns an empty list."""
    assert bbo._scan_directive_calls(tmp_path / "absent.conf", "addfragments") == []


def test_scan_directive_calls_counts_tokens(tmp_path: Path) -> None:
    """Each call site is reported with its (lineno, arg_count) tuple."""
    conf = tmp_path / "bitbake.conf"
    conf.write_text(
        '# comment\naddfragments a b c\n  addfragments x y\nINHERIT += "foo"\naddfragments p q r s\n',
        encoding="utf-8",
    )
    result = bbo._scan_directive_calls(conf, "addfragments")
    assert result == [(2, 3), (3, 2), (5, 4)]


# ---------------------------------------------------------------------------
# _check_parser_compat: warns on arity mismatch and missing directive
# ---------------------------------------------------------------------------


def test_check_parser_compat_no_log_is_noop(tmp_path: Path) -> None:
    """Without a logger, the function is a fast no-op."""
    cfg = _cfg(tmp_path, "nxp")
    bbo._check_parser_compat(cfg, tmp_path / "upstream", log=None)
    # No exception, no side effects. Nothing else to assert.


def test_check_parser_compat_warns_on_missing_directive(tmp_path: Path) -> None:
    """A BSP conf that calls a directive the override lacks emits a warn."""
    cfg = _cfg(tmp_path, "nxp")
    # Write a BSP bitbake.conf that uses addfragments...
    bitbake_conf = cfg.bsp_bitbake_conf
    bitbake_conf.parent.mkdir(parents=True)
    bitbake_conf.write_text("addfragments a b c\n", encoding="utf-8")
    # ...but the override has no ConfHandler.py at all (missing-file path).
    upstream = cfg.bsp_root / "upstream-bitbake"
    upstream.mkdir()

    log = MagicMock()
    bbo._check_parser_compat(cfg, upstream, log)

    log.warn.assert_called_once()
    msg = log.warn.call_args.args[0]
    assert "no 'addfragments' directive support" in msg


def test_check_parser_compat_warns_on_arity_mismatch(tmp_path: Path) -> None:
    """Override regex expects 2 args but BSP conf passes 3 -> warn per site."""
    cfg = _cfg(tmp_path, "nxp")
    bitbake_conf = cfg.bsp_bitbake_conf
    bitbake_conf.parent.mkdir(parents=True)
    bitbake_conf.write_text("addfragments a b c\n", encoding="utf-8")

    upstream = cfg.bsp_root / "upstream-bitbake"
    handler = bbo._override_conf_handler(upstream)
    handler.parent.mkdir(parents=True)
    handler.write_text(
        '__addfragments_regexp__ = re.compile(r"addfragments\\s+(.+)\\s+(.+)")\n',
        encoding="utf-8",
    )

    log = MagicMock()
    bbo._check_parser_compat(cfg, upstream, log)

    log.warn.assert_called_once()
    kwargs = log.warn.call_args.kwargs
    assert kwargs["expected_args"] == 2
    assert kwargs["actual_args"] == 3


def test_check_parser_compat_quiet_when_no_calls(tmp_path: Path) -> None:
    """BSP conf without the directive: no warning regardless of override."""
    cfg = _cfg(tmp_path, "nxp")
    bitbake_conf = cfg.bsp_bitbake_conf
    bitbake_conf.parent.mkdir(parents=True)
    bitbake_conf.write_text("# nothing relevant here\n", encoding="utf-8")

    log = MagicMock()
    bbo._check_parser_compat(cfg, cfg.bsp_root / "upstream-bitbake", log)
    log.warn.assert_not_called()


# ---------------------------------------------------------------------------
# apply error path: poky/ parent missing -> step_skip
# ---------------------------------------------------------------------------


def test_apply_skips_when_poky_parent_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``sources/poky/`` does not exist, apply skips without running git."""
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE", raising=False)
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE_BRANCH", raising=False)

    cfg = _cfg(tmp_path, "nxp")
    cfg.bsp_root.mkdir(parents=True)  # only the workspace root exists
    # Need a branch since auto-detect would fail; but apply checks parent first,
    # so any branch arg keeps resolve_branch happy.
    fake_run = MagicMock(side_effect=AssertionError("git must not be invoked"))
    monkeypatch.setattr(bbo.subprocess, "run", fake_run)

    log = MagicMock()
    result = bbo.apply(cfg, log=log, branch="br-2.8")

    assert result.state == "missing"
    log.step_skip.assert_called_once()
    assert "pre-bootstrap" in log.step_skip.call_args.kwargs["reason"]
    fake_run.assert_not_called()


# ---------------------------------------------------------------------------
# apply: BSP/upstream major.minor mismatch -> warn (lines 505-515)
# ---------------------------------------------------------------------------


def test_apply_warns_on_major_minor_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply emits a warn when displaced BSP version differs from upstream."""
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE", raising=False)
    monkeypatch.delenv("BAKAR_BITBAKE_OVERRIDE_BRANCH", raising=False)

    cfg = _cfg(tmp_path, "nxp")
    _write_bsp_bitbake(cfg, "2.8.0")

    source_repo = tmp_path / "source-bb"
    source_repo.mkdir()

    def fake_ensure_clone(upstream_dir: Path, _src: Path, _branch: str) -> None:
        init_py = upstream_dir / "lib" / "bb" / "__init__.py"
        init_py.parent.mkdir(parents=True, exist_ok=True)
        init_py.write_text('__version__ = "2.14.0"\n', encoding="utf-8")

    monkeypatch.setattr(bbo, "_ensure_clone", fake_ensure_clone)
    monkeypatch.setattr(bbo, "_head_sha", lambda _p: "abc1234")

    log = MagicMock()
    result = bbo.apply(cfg, log=log, branch="br-2.14", repo_path=source_repo)

    assert result.state == "active"
    # Both step_start and step_ok fired:
    log.step_start.assert_called_once()
    log.step_ok.assert_called_once()
    # ...and the major.minor mismatch warning fired exactly once:
    assert log.warn.call_count == 1
    assert "mismatch" in log.warn.call_args.args[0]
