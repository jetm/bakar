"""Tests for bakar.steps.kas_pins: the release probe's kas dump."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from bakar import bsp_detect
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

    ((wrapper, one, two, pin),) = seen
    assert "repo: meta-avocado" in wrapper
    assert one == first.read_text()
    assert two == second.read_text()
    assert pin == bsp_detect.meta_avocado_pin_text(checkout)


# ---------------------------------------------------------------------------
# wrap_parent: the build's probe sits beside its own wrapper
# ---------------------------------------------------------------------------


def test_wrap_parent_names_extras_in_place_beside_the_probe(tmp_path: Path) -> None:
    checkout, kas_yaml = _layout(tmp_path)
    bsp = tmp_path / "ws" / "build-m"
    overlay = bsp / ".bakar" / "overlays" / "main.yml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("header:\n  version: 16\n")
    seen: list[tuple[list[str], Path, str]] = []

    def run(argv, **kwargs):
        files = argv[-1].split(":")
        seen.append((files, Path(kwargs["cwd"]), Path(files[0]).read_text()))
        return subprocess.CompletedProcess(argv, 0, "repos: {}\n", "")

    with patch.object(kas_pins.subprocess, "run", run):
        kas_pins.read_merged_config(kas_yaml, checkout=checkout, extra_files=[overlay], wrap_parent=bsp)

    ((files, cwd, wrapper_text),) = seen
    wrapper = Path(files[0])
    # Directly in bsp_root, not a subdirectory: outside git kas roots string
    # includes at the first file's directory.
    assert wrapper.parent == bsp
    assert wrapper.name.startswith(kas_pins.PROBE_PREFIX)
    assert "repo: meta-avocado" in wrapper_text
    assert files[1:-1] == [str(overlay)]
    pin = Path(files[-1])
    assert pin.parent == bsp
    assert pin.name.startswith(kas_pins.PROBE_PREFIX)
    assert cwd == bsp
    assert not wrapper.exists()
    assert list(bsp.glob(f"{kas_pins.PROBE_PREFIX}*")) == []


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess([], 1, "", "ERROR - Configuration file not found: x.yml\n"),
        subprocess.TimeoutExpired(["kas"], 120),
        FileNotFoundError("kas"),
    ],
)
def test_wrap_parent_probe_is_removed_after_a_failure(tmp_path: Path, outcome: object) -> None:
    checkout, kas_yaml = _layout(tmp_path)
    bsp = tmp_path / "ws" / "build-m"
    bsp.mkdir()

    def run(argv, **_kwargs):
        assert len(list(bsp.glob(f"{kas_pins.PROBE_PREFIX}*"))) == 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    with patch.object(kas_pins.subprocess, "run", run), pytest.raises(ReleaseRoutingError):
        kas_pins.read_merged_config(kas_yaml, checkout=checkout, wrap_parent=bsp)

    assert list(bsp.iterdir()) == []


# ---------------------------------------------------------------------------
# Pin file: merged after the extras so an overlay cannot move meta-avocado
# ---------------------------------------------------------------------------


class _FilesRecorder:
    """Read every file the dump names; ``_Recorder`` reads only the last argument as one path."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        paths = [Path(f) for f in argv[-1].split(":")]
        self.calls.append({"paths": paths, "texts": [p.read_text() for p in paths], "cwd": Path(kwargs["cwd"])})
        return subprocess.CompletedProcess(argv, 0, "repos: {}\n", "")


def _expected_pin(checkout: Path) -> dict:
    return {"header": {"version": 16}, "repos": {"meta-avocado": {"path": str(checkout.resolve())}}}


def test_pinfile_follows_the_extras_in_the_temporary_layout(tmp_path: Path) -> None:
    checkout, kas_yaml = _layout(tmp_path)
    first = tmp_path / "one.yml"
    first.write_text("header:\n  version: 16\nmachine: one\n")
    second = tmp_path / "two.yml"
    second.write_text("header:\n  version: 16\nmachine: two\n")
    rec = _FilesRecorder()
    with patch.object(kas_pins.subprocess, "run", rec):
        kas_pins.read_merged_config(kas_yaml, checkout=checkout, extra_files=[first, second])

    (call,) = rec.calls
    paths, texts = call["paths"], call["texts"]
    assert [p.name for p in paths] == ["avocado-wrapper.yml", "extra-00-one.yml", "extra-01-two.yml", "avocado-pin.yml"]
    assert texts[1:3] == [first.read_text(), second.read_text()]
    assert yaml.safe_load(texts[-1]) == _expected_pin(checkout)
    assert len({p.parent for p in paths}) == 1
    assert not paths[0].parent.exists()


def test_pinfile_sits_beside_the_wrap_parent_probe(tmp_path: Path) -> None:
    checkout, kas_yaml = _layout(tmp_path)
    bsp = tmp_path / "ws" / "build-m"
    overlay = bsp / ".bakar" / "overlays" / "main.yml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("header:\n  version: 16\n")
    rec = _FilesRecorder()
    with patch.object(kas_pins.subprocess, "run", rec):
        kas_pins.read_merged_config(kas_yaml, checkout=checkout, extra_files=[overlay], wrap_parent=bsp)

    (call,) = rec.calls
    wrapper, named, pin = call["paths"]
    assert named == overlay
    assert wrapper.parent == bsp
    assert pin.parent == bsp
    assert wrapper.name.startswith(kas_pins.PROBE_PREFIX)
    assert pin.name.startswith(kas_pins.PROBE_PREFIX)
    assert wrapper != pin
    assert yaml.safe_load(call["texts"][-1]) == _expected_pin(checkout)
    assert list(bsp.glob(f"{kas_pins.PROBE_PREFIX}*")) == []


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess([], 0, "repos: {}\n", ""),
        subprocess.CompletedProcess([], 1, "", "ERROR - Configuration file not found: x.yml\n"),
        subprocess.TimeoutExpired(["kas"], 120),
        FileNotFoundError("kas"),
    ],
)
def test_pinfile_is_removed_from_the_build_dir_after_every_outcome(tmp_path: Path, outcome: object) -> None:
    checkout, kas_yaml = _layout(tmp_path)
    bsp = tmp_path / "ws" / "build-m"
    bsp.mkdir()
    extra = tmp_path / "extra.yml"
    extra.write_text("header:\n  version: 16\n")

    def run(argv, **_kwargs):
        # The wrapper and the pin, nothing else.
        assert len(list(bsp.glob(f"{kas_pins.PROBE_PREFIX}*"))) == 2
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    failing = not (isinstance(outcome, subprocess.CompletedProcess) and outcome.returncode == 0)
    with patch.object(kas_pins.subprocess, "run", run):
        if failing:
            with pytest.raises(ReleaseRoutingError):
                kas_pins.read_merged_config(kas_yaml, checkout=checkout, extra_files=[extra], wrap_parent=bsp)
        else:
            kas_pins.read_merged_config(kas_yaml, checkout=checkout, extra_files=[extra], wrap_parent=bsp)

    assert list(bsp.iterdir()) == []


@pytest.mark.parametrize("in_build_dir", [False, True])
def test_pinfile_is_not_merged_without_extras(tmp_path: Path, in_build_dir: bool) -> None:
    checkout, kas_yaml = _layout(tmp_path)
    bsp = tmp_path / "ws" / "build-m"
    bsp.mkdir()
    rec = _FilesRecorder()
    with patch.object(kas_pins.subprocess, "run", rec):
        kas_pins.read_merged_config(kas_yaml, checkout=checkout, wrap_parent=bsp if in_build_dir else None)

    (call,) = rec.calls
    (wrapper,) = call["paths"]
    assert "repo: meta-avocado" in call["texts"][0]
    if in_build_dir:
        assert wrapper.parent == bsp
    else:
        assert wrapper.name == "avocado-wrapper.yml"


def _real_checkout(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "meta-avocado"
    entry = checkout / "kas" / "machine" / "m.yml"
    entry.parent.mkdir(parents=True)
    entry.write_text(
        "header:\n"
        "  version: 16\n"
        "machine: qemux86-64\n"
        "repos:\n"
        "  meta-avocado:\n"
        "    path: meta-avocado\n"
        "    layers:\n"
        "      meta-x:\n"
    )
    _git(checkout, "init", "-q")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-q", "-m", "init")
    return checkout, entry


@pytest.mark.skipif(shutil.which("kas") is None, reason="kas is not on PATH")
@pytest.mark.parametrize("in_build_dir", [True, False])
def test_real_kas_pinfile_keeps_meta_avocado_on_the_checkout(tmp_path: Path, in_build_dir: bool) -> None:
    checkout, entry = _real_checkout(tmp_path)
    bsp = tmp_path / "build-m"
    bsp.mkdir()
    overlay = bsp / "overlay.yml"
    overlay.write_text(
        "header:\n  version: 16\nrepos:\n  meta-avocado:\n    path: meta-avocado\n    layers:\n      meta-extra:\n"
    )

    merged = kas_pins.read_merged_config(
        entry, checkout=checkout, extra_files=[overlay], wrap_parent=bsp if in_build_dir else None
    )

    repo = merged["repos"]["meta-avocado"]
    assert repo["path"] == str(checkout.resolve())
    assert {"meta-x", "meta-extra"} <= set(repo["layers"])


@pytest.mark.skipif(shutil.which("kas") is None, reason="kas is not on PATH")
def test_real_kas_pinfile_leaves_an_unrelated_overlay_unchanged(tmp_path: Path) -> None:
    checkout, entry = _real_checkout(tmp_path)
    overlay = tmp_path / "overlay.yml"
    overlay.write_text('header:\n  version: 16\nlocal_conf_header:\n  pinfile-test: |\n    FOO = "1"\n')

    merged = kas_pins.read_merged_config(entry, checkout=checkout, extra_files=[overlay])

    manual = tmp_path / "manual"
    manual.mkdir()
    work = tmp_path / "manual-work"
    work.mkdir()
    wrapper = manual / "avocado-wrapper.yml"
    wrapper.write_text(bsp_detect.meta_avocado_wrapper_text(checkout, entry))
    copy = manual / "overlay.yml"
    shutil.copyfile(overlay, copy)
    out_path = tmp_path / "manual.out"
    err_path = tmp_path / "manual.err"
    argv = ["kas", "dump", "--skip", "repos_checkout", "--skip", "finish_setup_repos", "--skip", "repos_apply_patches"]
    with out_path.open("w") as out, err_path.open("w") as err:
        proc = subprocess.run(
            [*argv, f"{wrapper}:{copy}"],
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            cwd=manual,
            env={**os.environ, "KAS_WORK_DIR": str(work)},
            check=False,
            start_new_session=True,
        )
    assert proc.returncode == 0, err_path.read_text()
    assert merged == yaml.safe_load(out_path.read_text())
    assert merged["local_conf_header"]["pinfile-test"] == 'FOO = "1"\n'
