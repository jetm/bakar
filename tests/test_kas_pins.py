"""Tests for bakar.steps.kas_pins: the release probe's kas dump."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from bakar.release_workspace import ReleaseRoutingError
from bakar.steps import kas_pins


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "ws" / "meta-avocado"
    kas_yaml = checkout / "kas" / "machine" / "m.yml"
    kas_yaml.parent.mkdir(parents=True)
    kas_yaml.write_text("header:\n  version: 16\n")
    return checkout, kas_yaml


class _Recorder:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        cwd = Path(kwargs["cwd"])
        work = Path(kwargs["env"]["KAS_WORK_DIR"])
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "work": work,
                "work_empty": work.is_dir() and not any(work.iterdir()),
                "wrapper": Path(argv[-1]).read_text(),
            }
        )
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def test_argv_carries_three_skips_and_isolated_work_dir(tmp_path: Path) -> None:
    checkout, kas_yaml = _layout(tmp_path)
    merged = {"repos": {"openembedded-core": {"branch": "wrynose-avocado"}}}
    rec = _Recorder(stdout=yaml.safe_dump(merged))
    with patch.object(kas_pins.subprocess, "run", rec):
        assert kas_pins.read_merged_config(kas_yaml, checkout=checkout) == merged

    (call,) = rec.calls
    argv = call["argv"]
    assert argv[:2] == ["kas", "dump"]
    for step in ("repos_checkout", "finish_setup_repos", "repos_apply_patches"):
        idx = argv.index(step)
        assert argv[idx - 1] == "--skip"
    work = call["work"]
    assert work.resolve() not in {checkout.resolve(), checkout.resolve().parent}
    assert call["work_empty"]
    assert call["cwd"] != work
    assert call["cwd"] == Path(argv[-1]).parent
    wrapper = yaml.safe_load(call["wrapper"])
    assert wrapper["repos"]["meta-avocado"]["path"] == str(checkout.resolve())
    assert wrapper["header"]["includes"] == [{"repo": "meta-avocado", "file": "kas/machine/m.yml"}]
    # Both temporary directories are gone after the call.
    assert not work.exists()
    assert not call["cwd"].exists()


def test_probe_release_strips_avocado_suffix(tmp_path: Path) -> None:
    checkout, kas_yaml = _layout(tmp_path)
    rec = _Recorder(stdout=yaml.safe_dump({"repos": {"openembedded-core": {"branch": "scarthgap-avocado"}}}))
    with patch.object(kas_pins.subprocess, "run", rec):
        assert kas_pins.probe_release(kas_yaml, checkout) == "scarthgap"


def test_missing_include_repos_are_named(tmp_path: Path) -> None:
    checkout, kas_yaml = _layout(tmp_path)
    stderr = (
        "2026-10-15 10:00:00 - INFO     - kas 5.5 started\n"
        "2026-10-15 10:00:00 - ERROR    - Could not fetch all repos needed by includes. "
        "Missing repos: meta-extra, meta-other\n"
    )
    rec = _Recorder(returncode=1, stderr=stderr)
    with patch.object(kas_pins.subprocess, "run", rec), pytest.raises(ReleaseRoutingError) as exc:
        kas_pins.read_merged_config(kas_yaml, checkout=checkout)
    assert "meta-extra, meta-other" in str(exc.value)


def test_other_failure_quotes_error_line(tmp_path: Path) -> None:
    checkout, kas_yaml = _layout(tmp_path)
    rec = _Recorder(
        returncode=1,
        stderr="2026-10-15 - INFO     - kas 5.5 started\n2026-10-15 - ERROR    - Unknown config key: frobnicate\n",
    )
    with patch.object(kas_pins.subprocess, "run", rec), pytest.raises(ReleaseRoutingError) as exc:
        kas_pins.read_merged_config(kas_yaml, checkout=checkout)
    assert "Unknown config key: frobnicate" in str(exc.value)
    assert str(kas_yaml) in str(exc.value)


def test_failure_without_error_marker_quotes_first_line(tmp_path: Path) -> None:
    checkout, kas_yaml = _layout(tmp_path)
    rec = _Recorder(returncode=2, stderr="usage: kas dump [-h]\nkas: error: bad flag\n")
    with patch.object(kas_pins.subprocess, "run", rec), pytest.raises(ReleaseRoutingError) as exc:
        kas_pins.read_merged_config(kas_yaml, checkout=checkout)
    assert "usage: kas dump [-h]" in str(exc.value)


def test_kas_not_installed_is_a_routing_error(tmp_path: Path) -> None:
    checkout, kas_yaml = _layout(tmp_path)
    with (
        patch.object(kas_pins.subprocess, "run", side_effect=FileNotFoundError("kas")),
        pytest.raises(ReleaseRoutingError, match="kas is not installed"),
    ):
        kas_pins.read_merged_config(kas_yaml, checkout=checkout)


def test_non_mapping_output_is_a_routing_error(tmp_path: Path) -> None:
    checkout, kas_yaml = _layout(tmp_path)
    rec = _Recorder(stdout="- just\n- a list\n")
    with patch.object(kas_pins.subprocess, "run", rec), pytest.raises(ReleaseRoutingError):
        kas_pins.read_merged_config(kas_yaml, checkout=checkout)


def _git(repo: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.mark.skipif(shutil.which("kas") is None, reason="kas is not on PATH")
def test_real_kas_probe_release(tmp_path: Path) -> None:
    checkout = tmp_path / "meta-avocado"
    kas_yaml = checkout / "kas" / "machine" / "m.yml"
    kas_yaml.parent.mkdir(parents=True)
    kas_yaml.write_text(
        "header:\n"
        "  version: 16\n"
        "machine: qemux86-64\n"
        "repos:\n"
        "  openembedded-core:\n"
        "    url: https://git.openembedded.org/openembedded-core\n"
        "    commit: 0123456789abcdef0123456789abcdef01234567\n"
        "    branch: wrynose-avocado\n"
        "    layers:\n"
        "      meta:\n"
    )
    _git(checkout, "init", "-q")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-q", "-m", "init")
    sibling = tmp_path / "openembedded-core"

    assert kas_pins.probe_release(kas_yaml, checkout) == "wrynose"
    # The throwaway work dir kept kas away from the checkout's parent.
    assert not sibling.exists()


# ---------------------------------------------------------------------------
# Include walk: a foreign repository is refused before kas runs
# ---------------------------------------------------------------------------


def _no_kas(*_args, **_kwargs):
    raise AssertionError("kas must not run when an include needs another repository")


def test_foreign_include_found_through_nested_includes_is_refused(tmp_path: Path) -> None:
    checkout, kas_yaml = _layout(tmp_path)
    kas_yaml.write_text("header:\n  version: 16\n  includes:\n    - kas/include/a.yml\n")
    (checkout / "kas" / "include").mkdir()
    (checkout / "kas" / "include" / "a.yml").write_text(
        "header:\n  version: 16\n  includes:\n    - repo: meta-avocado\n      file: kas/include/b.yml\n"
    )
    (checkout / "kas" / "include" / "b.yml").write_text(
        "header:\n  version: 16\n  includes:\n    - repo: meta-extra\n      file: conf.yml\n"
    )
    with patch.object(kas_pins.subprocess, "run", _no_kas), pytest.raises(ReleaseRoutingError) as exc:
        kas_pins.read_merged_config(kas_yaml, checkout=checkout)
    assert "meta-extra" in str(exc.value)
    assert str(kas_yaml) in str(exc.value)


def test_include_cycle_terminates_and_reaches_kas(tmp_path: Path) -> None:
    checkout, kas_yaml = _layout(tmp_path)
    kas_yaml.write_text("header:\n  version: 16\n  includes:\n    - kas/machine/other.yml\n")
    (checkout / "kas" / "machine" / "other.yml").write_text(
        "header:\n  version: 16\n  includes:\n    - kas/machine/m.yml\n"
    )
    rec = _Recorder(stdout=yaml.safe_dump({"repos": {}}))
    with patch.object(kas_pins.subprocess, "run", rec):
        assert kas_pins.read_merged_config(kas_yaml, checkout=checkout) == {"repos": {}}
    assert len(rec.calls) == 1


def test_file_relative_include_is_followed(tmp_path: Path) -> None:
    checkout, kas_yaml = _layout(tmp_path)
    kas_yaml.write_text("header:\n  version: 16\n  includes:\n    - sibling.yml\n")
    (checkout / "kas" / "machine" / "sibling.yml").write_text(
        "header:\n  version: 16\n  includes:\n    - repo: meta-extra\n      file: conf.yml\n"
    )
    with patch.object(kas_pins.subprocess, "run", _no_kas), pytest.raises(ReleaseRoutingError, match="meta-extra"):
        kas_pins.read_merged_config(kas_yaml, checkout=checkout)


def test_foreign_include_with_unreachable_url_is_refused_before_any_fetch(tmp_path: Path) -> None:
    """The include repo's url names a path that does not exist: kas would fail cloning it, so a
    refusal naming the repo proves the walk ran first and nothing was contacted."""
    checkout = tmp_path / "meta-avocado"
    kas_yaml = checkout / "kas" / "machine" / "m.yml"
    kas_yaml.parent.mkdir(parents=True)
    absent = tmp_path / "absent" / "meta-inc.git"
    kas_yaml.write_text(
        "header:\n"
        "  version: 16\n"
        "  includes:\n"
        "    - repo: meta-inc\n"
        "      file: kas/inc.yml\n"
        "repos:\n"
        "  meta-inc:\n"
        f"    url: file://{absent}\n"
        "    commit: 0123456789abcdef0123456789abcdef01234567\n"
    )
    _git(checkout, "init", "-q")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-q", "-m", "init")

    with pytest.raises(ReleaseRoutingError) as exc:
        kas_pins.read_merged_config(kas_yaml, checkout=checkout)

    assert "meta-inc" in str(exc.value)
    assert "other than its own checkout" in str(exc.value)
    assert not absent.parent.exists()
    assert not (tmp_path / "meta-inc").exists()


# ---------------------------------------------------------------------------
# extra_files: merged after the entry, in order
# ---------------------------------------------------------------------------


def test_extra_files_follow_the_wrapper_in_order(tmp_path: Path) -> None:
    checkout, kas_yaml = _layout(tmp_path)
    bsp = tmp_path / "ws" / "build-m"
    bsp.mkdir()
    first = bsp / "main.yml"
    first.write_text("header:\n  version: 16\nmachine: one\n")
    second = bsp / "sub" / "main.yml"
    second.parent.mkdir()
    second.write_text("header:\n  version: 16\nmachine: two\n")
    seen: list[list[str]] = []

    def run(argv, **kwargs):
        files = argv[-1].split(":")
        seen.append([Path(f).read_text() for f in files])
        assert all(Path(f).parent == Path(kwargs["cwd"]) for f in files)
        return subprocess.CompletedProcess(argv, 0, "repos: {}\n", "")

    with patch.object(kas_pins.subprocess, "run", run):
        kas_pins.read_merged_config(kas_yaml, checkout=checkout, extra_files=[first, second])

    ((wrapper, one, two),) = seen
    assert "repo: meta-avocado" in wrapper
    assert one == first.read_text()
    assert two == second.read_text()
