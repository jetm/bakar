"""``read_task_hash``: the bitbake taskhash read from stamp file names.

Layout mirrors the spike measurement (pc2, htop 3.5.0):
``stamps/<pkgarch>/<recipe>/<PV>.<task>.<taskhash>``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar import feed_delta

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

H_PACKAGE = "b3cffa92d9f178a18cdc0e018cb576db594dd17aa8fdc29f582e5ad60b6cf090"
H_SETSCENE = "c493710d6ae2528e9e2d0d18a77bd3fadb8bc7d0246b2fc302ca386fee103505"
H_EXEC = "a" * 64
H_OTHER = "d" * 64


def _stamp(stamps: Path, name: str, *, arch: str = "core2-64-avocado-linux", recipe: str = "htop") -> None:
    d = stamps / arch / recipe
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("")


def test_task_hash_matches_spike_source(tmp_path: Path) -> None:
    _stamp(tmp_path, f"3.5.0.do_package.{H_PACKAGE}")
    _stamp(tmp_path, f"3.5.0.do_package.{H_PACKAGE}.sigdata.{'f' * 64}")
    _stamp(tmp_path, f"3.5.0.do_package_write_rpm_setscene.{H_SETSCENE}")
    assert feed_delta.read_task_hash(tmp_path, "htop", "3.5.0") == H_SETSCENE


def test_executed_rpm_write_stamp_wins_over_setscene(tmp_path: Path) -> None:
    _stamp(tmp_path, f"3.5.0.do_package_write_rpm.{H_EXEC}")
    _stamp(tmp_path, f"3.5.0.do_package_write_rpm_setscene.{H_SETSCENE}")
    assert feed_delta.read_task_hash(tmp_path, "htop", "3.5.0") == H_EXEC


def test_do_package_is_the_fallback(tmp_path: Path) -> None:
    _stamp(tmp_path, f"3.5.0.do_package.{H_PACKAGE}")
    assert feed_delta.read_task_hash(tmp_path, "htop", "3.5.0") == H_PACKAGE


def test_sigdata_only_is_not_a_hash(tmp_path: Path) -> None:
    _stamp(tmp_path, f"3.5.0.do_package_write_rpm.sigdata.{H_EXEC}")
    assert feed_delta.read_task_hash(tmp_path, "htop", "3.5.0") is None


def test_other_pv_is_ignored(tmp_path: Path) -> None:
    _stamp(tmp_path, f"3.4.0.do_package_write_rpm.{H_EXEC}")
    assert feed_delta.read_task_hash(tmp_path, "htop", "3.5.0") is None


def test_pv_prefix_does_not_match_longer_pv(tmp_path: Path) -> None:
    _stamp(tmp_path, f"3.5.0.1.do_package_write_rpm.{H_EXEC}")
    assert feed_delta.read_task_hash(tmp_path, "htop", "3.5.0") is None


def test_other_recipe_is_ignored(tmp_path: Path) -> None:
    _stamp(tmp_path, f"3.5.0.do_package_write_rpm.{H_EXEC}", recipe="htop-extra")
    assert feed_delta.read_task_hash(tmp_path, "htop", "3.5.0") is None


def test_ambiguous_hashes_in_best_tier_return_none(tmp_path: Path) -> None:
    _stamp(tmp_path, f"3.5.0.do_package_write_rpm.{H_EXEC}")
    _stamp(tmp_path, f"3.5.0.do_package_write_rpm.{H_OTHER}")
    _stamp(tmp_path, f"3.5.0.do_package.{H_PACKAGE}")
    assert feed_delta.read_task_hash(tmp_path, "htop", "3.5.0") is None


def test_same_hash_across_pkgarch_dirs_is_not_ambiguous(tmp_path: Path) -> None:
    _stamp(tmp_path, f"3.5.0.do_package_write_rpm.{H_EXEC}", arch="core2-64-avocado-linux")
    _stamp(tmp_path, f"3.5.0.do_package_write_rpm.{H_EXEC}", arch="qemux86-64-avocado-linux")
    assert feed_delta.read_task_hash(tmp_path, "htop", "3.5.0") == H_EXEC


def test_missing_stamps_dir_returns_none(tmp_path: Path) -> None:
    assert feed_delta.read_task_hash(tmp_path / "absent", "htop", "3.5.0") is None


def test_unresolved_hash_keeps_explicit_null_and_fallback_key(tmp_path: Path) -> None:
    entry = feed_delta.entry_from_package_files(
        recipe="htop",
        package_files=["htop-3.5.0-r0.1.core2_64.rpm"],
        packages=["htop"],
        layer="meta-openembedded",
        epoch=0,
        srcrev=None,
        layer_commit="f28a144aa0a0",
        machine="qemux86-64",
        release="r1",
        task_hash=feed_delta.read_task_hash(tmp_path, "htop", "3.5.0"),
    )
    assert entry.task_hash is None
    assert entry.delta_key() == ("0", "3.5.0", "r0.1", "", "f28a144aa0a0")


def test_git_recipe_pkgv_resolves_against_pv_stamp(tmp_path: Path) -> None:
    _stamp(tmp_path, f"3.5.0+git.do_package_write_rpm.{H_EXEC}")
    assert feed_delta.read_task_hash(tmp_path, "htop", "3.5.0+git0+0123abcd") == H_EXEC


def test_git_recipe_falls_back_to_base_before_plus(tmp_path: Path) -> None:
    _stamp(tmp_path, f"3.5.0.do_package_write_rpm.{H_EXEC}")
    assert feed_delta.read_task_hash(tmp_path, "htop", "3.5.0+git0+0123abcd") == H_EXEC


def test_exact_pkgv_stamp_is_preferred_over_derived_candidates(tmp_path: Path) -> None:
    _stamp(tmp_path, f"3.5.0+git0+0123abcd.do_package_write_rpm.{H_EXEC}")
    _stamp(tmp_path, f"3.5.0+git.do_package_write_rpm.{H_OTHER}")
    assert feed_delta.read_task_hash(tmp_path, "htop", "3.5.0+git0+0123abcd") == H_EXEC


def test_ambiguous_hashes_within_the_matching_candidate_return_none(tmp_path: Path) -> None:
    _stamp(tmp_path, f"3.5.0+git.do_package_write_rpm.{H_EXEC}")
    _stamp(tmp_path, f"3.5.0+git.do_package_write_rpm.{H_OTHER}")
    _stamp(tmp_path, f"3.5.0.do_package_write_rpm.{H_PACKAGE}")
    assert feed_delta.read_task_hash(tmp_path, "htop", "3.5.0+git0+0123abcd") is None
