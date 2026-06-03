"""Unit tests for bakar.pin_state.

The kas lockfile and manifest XML are real files under ``tmp_path``. The
git-HEAD fallback and ``commit_distance`` paths are exercised by stubbing the
``subprocess.run`` calls (CI has no synced source checkouts), the pattern used
by ``tests/test_manifest_diff.py``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bakar import pin_state
from bakar.pin_state import commit_distance, parse_kas_lockfile, read_pins

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.unit


# 40-hex SHAs so parse_manifest_pins' _HEX40_RE accepts them.
_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40


class _Completed:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def _write_manifest(path: Path, projects: list[tuple[str, str]]) -> None:
    lines = ["<manifest>"]
    for proj_path, rev in projects:
        lines.append(f'  <project path="{proj_path}" revision="{rev}"/>')
    lines.append("</manifest>")
    path.write_text("\n".join(lines) + "\n")


def _write_lockfile(path: Path, repos: dict[str, str]) -> None:
    doc = {"repos": {name: {"commit": sha} for name, sha in repos.items()}}
    path.write_text(json.dumps(doc))


# ---------------------------------------------------------------------------
# parse_kas_lockfile
# ---------------------------------------------------------------------------


def test_parse_kas_lockfile_returns_name_to_commit(tmp_path: Path) -> None:
    lock = tmp_path / "kas.lock"
    _write_lockfile(lock, {"poky": _SHA_A, "meta-foo": _SHA_B})

    assert parse_kas_lockfile(lock) == {"poky": _SHA_A, "meta-foo": _SHA_B}


def test_parse_kas_lockfile_missing_repos_key_raises(tmp_path: Path) -> None:
    """Falsifier: lockfile without a top-level 'repos' key must raise."""
    lock = tmp_path / "kas.lock"
    lock.write_text(json.dumps({"header": {"version": 14}}))

    with pytest.raises(ValueError, match="repos"):
        parse_kas_lockfile(lock)


def test_parse_kas_lockfile_invalid_json_raises(tmp_path: Path) -> None:
    lock = tmp_path / "kas.lock"
    lock.write_text("{not json")

    with pytest.raises(ValueError, match="invalid JSON"):
        parse_kas_lockfile(lock)


def test_parse_kas_lockfile_unreadable_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot read"):
        parse_kas_lockfile(tmp_path / "absent.lock")


def test_parse_kas_lockfile_skips_repos_without_commit(tmp_path: Path) -> None:
    lock = tmp_path / "kas.lock"
    lock.write_text(json.dumps({"repos": {"poky": {"commit": _SHA_A}, "no-commit": {"url": "x"}}}))

    assert parse_kas_lockfile(lock) == {"poky": _SHA_A}


# ---------------------------------------------------------------------------
# read_pins - manifest families
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family", ["nxp", "ti"])
def test_read_pins_manifest_family_reads_xml(family: str, tmp_path: Path) -> None:
    manifest = tmp_path / "m.xml"
    _write_manifest(manifest, [("sources/poky", _SHA_A), ("sources/meta-bsp", _SHA_B)])

    pins = read_pins(family, manifest=manifest)

    assert pins == {"sources/poky": _SHA_A, "sources/meta-bsp": _SHA_B}


def test_read_pins_manifest_family_without_manifest_raises() -> None:
    with pytest.raises(ValueError, match="requires a manifest"):
        read_pins("nxp")


def test_read_pins_bbsetup_does_not_use_manifest(tmp_path: Path) -> None:
    """Falsifier: a bbsetup workspace must read lockfile pins, not manifest pins."""
    manifest = tmp_path / "m.xml"
    _write_manifest(manifest, [("sources/poky", _SHA_C)])
    lock = tmp_path / "kas.lock"
    _write_lockfile(lock, {"poky": _SHA_A})

    pins = read_pins("bbsetup", manifest=manifest, lockfile=lock)

    assert pins == {"poky": _SHA_A}


# ---------------------------------------------------------------------------
# read_pins - lockfile families
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family", ["bbsetup", "generic"])
def test_read_pins_lockfile_family_reads_lockfile(family: str, tmp_path: Path) -> None:
    lock = tmp_path / "kas.lock"
    _write_lockfile(lock, {"poky": _SHA_A})

    assert read_pins(family, lockfile=lock) == {"poky": _SHA_A}


def test_read_pins_falls_back_to_git_head_when_no_lockfile(tmp_path: Path) -> None:
    """No lockfile -> per-source git HEAD under workspace/sources."""
    src = tmp_path / "sources" / "poky"
    src.mkdir(parents=True)
    (src / ".git").mkdir()

    def fake_run(argv, **kwargs):
        assert "rev-parse" in argv
        assert "HEAD" in argv
        return _Completed(0, f"{_SHA_B}\n")

    with patch("bakar.pin_state.subprocess.run", side_effect=fake_run):
        pins = read_pins("bbsetup", workspace=tmp_path)

    assert pins == {"poky": _SHA_B}


def test_read_pins_git_head_fallback_skips_failed_repo(tmp_path: Path) -> None:
    src = tmp_path / "sources" / "poky"
    src.mkdir(parents=True)
    (src / ".git").mkdir()

    with patch("bakar.pin_state.subprocess.run", return_value=_Completed(128, "")):
        pins = read_pins("bbsetup", workspace=tmp_path)

    assert pins == {}


def test_read_pins_prefers_existing_lockfile_over_workspace(tmp_path: Path) -> None:
    lock = tmp_path / "kas.lock"
    _write_lockfile(lock, {"poky": _SHA_A})
    # A workspace is also provided but must be ignored when the lockfile exists.
    (tmp_path / "sources" / "poky").mkdir(parents=True)

    with patch("bakar.pin_state.subprocess.run") as run:
        pins = read_pins("bbsetup", lockfile=lock, workspace=tmp_path)

    assert pins == {"poky": _SHA_A}
    run.assert_not_called()


def test_read_pins_lockfile_family_without_inputs_raises() -> None:
    with pytest.raises(ValueError, match="lockfile or a workspace"):
        read_pins("bbsetup")


def test_read_pins_absent_lockfile_falls_back_to_workspace(tmp_path: Path) -> None:
    """A lockfile path that does not exist defers to the workspace fallback."""
    missing_lock = tmp_path / "absent.lock"
    src = tmp_path / "sources" / "poky"
    src.mkdir(parents=True)
    (src / ".git").mkdir()

    with patch("bakar.pin_state.subprocess.run", return_value=_Completed(0, _SHA_A)):
        pins = read_pins("bbsetup", lockfile=missing_lock, workspace=tmp_path)

    assert pins == {"poky": _SHA_A}


# ---------------------------------------------------------------------------
# commit_distance
# ---------------------------------------------------------------------------


def test_commit_distance_computes_old_to_new(tmp_path: Path) -> None:
    """Falsifier: distance must be measured old..new, not new..old."""
    checkout = tmp_path / "poky"
    checkout.mkdir()

    def fake_run(argv, **kwargs):
        assert "rev-list" in argv
        assert "--count" in argv
        assert f"{_SHA_A}..{_SHA_B}" in argv
        return _Completed(0, "7\n")

    with patch("bakar.manifest_diff.subprocess.run", side_effect=fake_run):
        assert commit_distance(checkout, _SHA_A, _SHA_B) == 7


def test_commit_distance_missing_checkout_returns_none(tmp_path: Path) -> None:
    assert commit_distance(tmp_path / "absent", _SHA_A, _SHA_B) is None


def test_commit_distance_failed_git_returns_none(tmp_path: Path) -> None:
    checkout = tmp_path / "poky"
    checkout.mkdir()

    with patch("bakar.manifest_diff.subprocess.run", return_value=_Completed(1, "")):
        assert commit_distance(checkout, _SHA_A, _SHA_B) is None


def test_module_reuses_rev_list_count() -> None:
    """commit_distance delegates to manifest_diff._rev_list_count, not a copy."""
    from bakar import manifest_diff

    assert pin_state._rev_list_count is manifest_diff._rev_list_count
