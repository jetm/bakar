"""Tests for the mold ``.comment`` stamp gate (:mod:`bakar.mold_verify`)."""

from __future__ import annotations

import pytest

from bakar.mold_verify import (
    MoldCheck,
    assert_mold_absent,
    assert_mold_present,
    mold_stamp_present,
    verify_mold_stamps,
)

# A binary linked by mold: readelf -p .comment carries the ``mold <version>``
# stamp alongside the compiler stamp.
MOLD_COMMENT = "String dump of section '.comment':\n  [     0]  GCC: (GNU) 15.2.0\n  [    12]  mold 2.41.0\n"

# A binary linked by ld.bfd: only the compiler stamp, no mold string. ld.bfd
# writes no linker stamp of its own.
BFD_COMMENT = "String dump of section '.comment':\n  [     0]  GCC: (GNU) 15.2.0\n"


@pytest.mark.unit
def test_mold_stamp_present_detects_mold() -> None:
    assert mold_stamp_present(MOLD_COMMENT) is True


@pytest.mark.unit
def test_mold_stamp_present_absent_for_bfd() -> None:
    assert mold_stamp_present(BFD_COMMENT) is False


@pytest.mark.unit
def test_present_check_passes_on_mold_fixture() -> None:
    result = assert_mold_present("busybox", MOLD_COMMENT)
    assert result.passed is True
    assert result.violations == ()


@pytest.mark.unit
def test_present_check_fails_on_bfd_fixture() -> None:
    # Silent bfd fallback: mold expected but the link fell back to bfd.
    result = assert_mold_present("busybox", BFD_COMMENT)
    assert result.passed is False
    (violation,) = result.violations
    assert violation.label == "busybox"
    assert violation.expected_mold is True
    assert violation.found_mold is False


@pytest.mark.unit
def test_absent_check_passes_on_bfd_fixture() -> None:
    result = assert_mold_absent("vmlinux", BFD_COMMENT)
    assert result.passed is True
    assert result.violations == ()


@pytest.mark.unit
def test_absent_check_fails_on_mold_fixture() -> None:
    # Self-exclusion breach: vmlinux must never carry the mold stamp.
    result = assert_mold_absent("vmlinux", MOLD_COMMENT)
    assert result.passed is False
    (violation,) = result.violations
    assert violation.label == "vmlinux"
    assert violation.expected_mold is False
    assert violation.found_mold is True


@pytest.mark.unit
def test_verify_batch_mixed_pass() -> None:
    checks = {
        "busybox": MoldCheck(MOLD_COMMENT, expected_mold=True),
        "vmlinux": MoldCheck(BFD_COMMENT, expected_mold=False),
        "excluded-pn": MoldCheck(BFD_COMMENT, expected_mold=False),
    }
    result = verify_mold_stamps(checks)
    assert result.passed is True
    assert result.violations == ()


@pytest.mark.unit
def test_verify_batch_reports_both_violation_kinds() -> None:
    checks = {
        # Included sample that should have mold but does not (bfd fallback).
        "busybox": MoldCheck(BFD_COMMENT, expected_mold=True),
        # Kernel that must not have mold but does (self-exclusion breach).
        "vmlinux": MoldCheck(MOLD_COMMENT, expected_mold=False),
    }
    result = verify_mold_stamps(checks)
    assert result.passed is False
    violations = {v.label: v for v in result.violations}
    assert set(violations) == {"busybox", "vmlinux"}
    assert "silent bfd fallback" in violations["busybox"].reason
    assert "self-exclusion breach" in violations["vmlinux"].reason
