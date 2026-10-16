"""Tests for the meta-avocado wrapper placement and the repository reuse policy.

Covers ``_write_meta_avocado_wrapper`` choosing an absolute ``path:`` for a
checkout outside ``cfg.workspace/meta-avocado``, and ``_build_kas_arg`` /
``_run_kas_dump`` under ``BAKAR_REPO_POLICY``: the reuse overlay is merged last
and the ``--skip repos_checkout`` retry is reserved for the ``kas`` policy.
Every kas and git call is mocked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from bakar.config import BuildConfig
from bakar.release_workspace import ReleaseRoutingError
from bakar.steps import kas_build
from bakar.steps.kas_build import _build_kas_arg, _run_kas_dump, _write_meta_avocado_wrapper
from bakar.steps.repo_reuse import RepoReuseError, RepoState

DUMP_YAML = "header:\n  version: 16\nrepos:\n  meta-foo:\n    url: https://example.invalid/foo.git\n    commit: abc\n"
PIN = "0123456789abcdef0123456789abcdef01234567"


def _cfg(workspace: Path, kas_yaml: Path) -> BuildConfig:
    cfg = BuildConfig(
        workspace=workspace,
        bsp_family="generic",  # type: ignore[arg-type]
        machine="generic",
        distro="generic",
        image="generic",
        manifest="",
        repo_url="",
        repo_branch="",
        kas_container_image="jetm/kas-build-env:latest",
        kas_yaml_override=kas_yaml,
    )
    cfg.bsp_root.mkdir(parents=True, exist_ok=True)
    return cfg


def _kas_file(checkout: Path) -> Path:
    kas_yaml = checkout / "kas" / "machine" / "m.yml"
    kas_yaml.parent.mkdir(parents=True, exist_ok=True)
    kas_yaml.write_text("header:\n  version: 16\n", encoding="utf-8")
    return kas_yaml


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["kas", "dump"], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def no_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BAKAR_REPO_POLICY", raising=False)


# ---------------------------------------------------------------------------
# _write_meta_avocado_wrapper
# ---------------------------------------------------------------------------


def test_wrapper_is_absolute_for_checkout_outside_workspace(tmp_path: Path) -> None:
    checkout = tmp_path / "release" / "meta-avocado"
    kas_yaml = _kas_file(checkout)
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = _cfg(ws, kas_yaml)

    wrapper = _write_meta_avocado_wrapper(cfg, kas_yaml)

    data = yaml.safe_load(wrapper.read_text(encoding="utf-8"))
    assert data["repos"] == {"meta-avocado": {"path": str(checkout.resolve())}}
    assert data["header"]["includes"] == [{"repo": "meta-avocado", "file": "kas/machine/m.yml"}]


def test_wrapper_is_relative_byte_for_byte_inside_workspace(tmp_path: Path) -> None:
    kas_yaml = _kas_file(tmp_path / "meta-avocado")
    cfg = _cfg(tmp_path, kas_yaml)

    wrapper = _write_meta_avocado_wrapper(cfg, kas_yaml)

    assert wrapper.read_text(encoding="utf-8") == (
        "header:\n"
        "  version: 16\n"
        "  includes:\n"
        "    - repo: meta-avocado\n"
        "      file: kas/machine/m.yml\n"
        "repos:\n"
        "  meta-avocado:\n"
        "    path: meta-avocado\n"
    )


def test_wrapper_is_relative_when_workspace_is_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    kas_yaml = _kas_file(real / "meta-avocado")
    link = tmp_path / "link"
    link.symlink_to(real)
    cfg = _cfg(link, link / "meta-avocado" / "kas" / "machine" / "m.yml")

    wrapper = _write_meta_avocado_wrapper(cfg, kas_yaml)

    assert yaml.safe_load(wrapper.read_text(encoding="utf-8"))["repos"]["meta-avocado"]["path"] == "meta-avocado"


# ---------------------------------------------------------------------------
# _build_kas_arg under BAKAR_REPO_POLICY
# ---------------------------------------------------------------------------


def _meta_avocado_setup(tmp_path: Path) -> tuple[BuildConfig, Path, Path]:
    kas_yaml = _kas_file(tmp_path / "meta-avocado")
    cfg = _cfg(tmp_path, kas_yaml)
    overlay = tmp_path / "overlay.yml"
    overlay.write_text("header:\n  version: 16\n", encoding="utf-8")
    return cfg, kas_yaml, overlay


def test_reuse_policy_merges_reuse_overlay_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, kas_yaml, overlay = _meta_avocado_setup(tmp_path)
    side = tmp_path / ".bakar" / "pins" / "meta-foo" / PIN[:12]
    states = [
        RepoState("poky", tmp_path / "poky", PIN, "frozen"),
        RepoState("meta-foo", tmp_path / "meta-foo", PIN, "side", side, False),
        RepoState("meta-bar", tmp_path / "meta-bar", PIN, "side", tmp_path / "bar-side", True),
    ]
    merged = {"repos": {"poky": {"url": "u", "commit": PIN}}}

    with (
        patch.object(kas_build.kas_pins, "read_merged_config", return_value=merged) as read,
        patch.object(kas_build.repo_reuse, "classify", return_value=states) as classify,
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)) as run,
    ):
        _build_kas_arg(cfg, kas_yaml, overlay)

    assert read.call_args.kwargs["checkout"].resolve() == (tmp_path / "meta-avocado").resolve()
    classify.assert_called_once_with(merged["repos"], cfg.workspace)
    argv = run.call_args.args[0]
    assert argv[:2] == ["kas", "dump"]
    assert "--skip" not in argv
    assert argv[-1].split(":")[-1] == "avocado-reuse.yml"
    overlay_data = yaml.safe_load((cfg.bsp_root / "avocado-reuse.yml").read_text(encoding="utf-8"))
    assert overlay_data["repos"]["poky"] == {"url": None}
    assert overlay_data["repos"]["meta-foo"] == {"path": str(side.absolute())}
    err = capsys.readouterr().err
    assert f"side checkout meta-foo at {PIN[:12]}: {side}" in err
    assert "meta-bar" not in err


@pytest.mark.parametrize("exc", [RepoReuseError("shared clone is dirty"), ReleaseRoutingError("cannot read pins")])
def test_reuse_errors_fail_the_build_with_their_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, kas_yaml, overlay = _meta_avocado_setup(tmp_path)

    with (
        patch.object(kas_build.kas_pins, "read_merged_config", side_effect=exc),
        patch("bakar.steps.kas_build.subprocess.run") as run,
        pytest.raises(RuntimeError, match=str(exc)),
    ):
        _build_kas_arg(cfg, kas_yaml, overlay)

    run.assert_not_called()


def test_reuse_without_meta_avocado_checkout_falls_back_to_kas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sibling-repo entry has no meta-avocado checkout: one skip line, then the kas policy."""
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    (tmp_path / "meta-avocado").mkdir()
    entry = tmp_path / "meta-avocado-cve" / "cve.yml"
    entry.parent.mkdir()
    entry.write_text("header:\n  version: 16\n", encoding="utf-8")
    cfg = _cfg(tmp_path, entry)
    overlay = tmp_path / "overlay.yml"
    overlay.write_text("header:\n  version: 16\n", encoding="utf-8")

    with (
        patch.object(kas_build.kas_pins, "read_merged_config") as read,
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)) as run,
    ):
        _build_kas_arg(cfg, entry, overlay)

    read.assert_not_called()
    assert "avocado-reuse.yml" not in run.call_args.args[0][-1]
    assert not (cfg.bsp_root / "avocado-reuse.yml").exists()
    assert capsys.readouterr().err.count("reuse skipped") == 1


def test_default_policy_adds_no_reuse_overlay(tmp_path: Path, no_policy: None) -> None:
    cfg, kas_yaml, overlay = _meta_avocado_setup(tmp_path)

    with (
        patch.object(kas_build.kas_pins, "read_merged_config") as read,
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)) as run,
    ):
        _build_kas_arg(cfg, kas_yaml, overlay)

    read.assert_not_called()
    assert "avocado-reuse.yml" not in run.call_args.args[0][-1]
    assert not (cfg.bsp_root / "avocado-reuse.yml").exists()


# ---------------------------------------------------------------------------
# _run_kas_dump retry
# ---------------------------------------------------------------------------

_REBASED = "error: origin/scarthgap does not contain commit deadbeef"


def _dump_inputs(tmp_path: Path) -> tuple[BuildConfig, Path]:
    cfg, _kas_yaml, _overlay = _meta_avocado_setup(tmp_path)
    wrapper = cfg.bsp_root / "avocado-wrapper.yml"
    wrapper.write_text("header:\n  version: 16\n", encoding="utf-8")
    return cfg, wrapper


def test_reuse_policy_does_not_retry_with_skip(tmp_path: Path) -> None:
    cfg, wrapper = _dump_inputs(tmp_path)

    with (
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(1, stderr=_REBASED)) as run,
        pytest.raises(RuntimeError, match="does not contain commit"),
    ):
        _run_kas_dump(cfg, wrapper, Path("o.yml"), [Path("avocado-reuse.yml")])

    assert run.call_count == 1


def test_kas_policy_still_retries_with_skip(tmp_path: Path) -> None:
    cfg, wrapper = _dump_inputs(tmp_path)
    responses = [_completed(1, stderr=_REBASED), _completed(0, stdout=DUMP_YAML)]

    with patch("bakar.steps.kas_build.subprocess.run", side_effect=responses) as run:
        _run_kas_dump(cfg, wrapper, Path("o.yml"))

    assert run.call_count == 2
    assert run.call_args_list[1].args[0][:4] == ["kas", "dump", "--skip", "repos_checkout"]
