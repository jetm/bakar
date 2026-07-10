"""Verify that binaries were (or were not) linked by mold, from ``.comment``.

The post-build gate for the mold-default-linker change keys on a single
falsifiable signal: whether mold wrote its version stamp into a binary's
``.comment`` section. mold emits a ``mold <version>`` string (e.g.
``mold 2.41.0``) whenever it links a file; GNU ``ld.bfd`` writes **no** linker
stamp of its own. So the predicate is the *presence* (or *absence*) of the mold
stamp - never a "GNU ld" string, which ld.bfd does not produce.

Two failure modes this catches:

- **Silent bfd fallback**: an included userspace binary that should carry the
  mold stamp does not. mold was expected but the link fell back to bfd.
- **Self-exclusion breach**: ``vmlinux`` or any excluded / non-included ``PN``
  carries the mold stamp when it must not.

This module is pure text parsing. The actual ``readelf -p .comment`` invocation
against the OE image tree is the caller's concern - this module only decides
pass/fail given the captured output text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# mold stamps binaries with ``mold <version>`` (e.g. ``mold 2.41.0``). ld.bfd
# writes no stamp of its own, so the version-suffixed form is the discriminator.
_MOLD_STAMP_RE = re.compile(r"\bmold\s+\d+(?:\.\d+)*")


def mold_stamp_present(comment_text: str) -> bool:
    """Return True if ``comment_text`` carries a ``mold <version>`` stamp.

    ``comment_text`` is the raw text of ``readelf -p .comment <binary>``. A
    binary linked by mold contains a ``mold 2.41.0``-style string; one linked by
    ld.bfd contains only compiler stamps (``GCC: (GNU) ...``) and no mold string.
    """
    return _MOLD_STAMP_RE.search(comment_text) is not None


@dataclass(frozen=True, slots=True)
class MoldCheck:
    """One binary's captured ``.comment`` text and whether mold is expected."""

    comment_text: str
    expected_mold: bool


@dataclass(frozen=True, slots=True)
class MoldViolation:
    """A binary whose observed mold state contradicts what was expected."""

    label: str
    expected_mold: bool
    found_mold: bool

    @property
    def reason(self) -> str:
        if self.expected_mold:
            return "mold stamp absent but expected (silent bfd fallback)"
        return "mold stamp present but forbidden (self-exclusion breach)"


@dataclass(frozen=True, slots=True)
class MoldVerifyResult:
    """Outcome of a batch verification: overall pass plus any violations."""

    passed: bool
    violations: tuple[MoldViolation, ...]


def verify_mold_stamps(checks: Mapping[str, MoldCheck]) -> MoldVerifyResult:
    """Verify a batch of ``{label -> MoldCheck}`` against the mold-stamp rule.

    For each entry, the observed stamp state (from :func:`mold_stamp_present`)
    must match ``expected_mold``. Any mismatch is a violation. The gate passes
    only when every entry agrees with its expectation.
    """
    violations = tuple(
        MoldViolation(label=label, expected_mold=check.expected_mold, found_mold=found)
        for label, check in checks.items()
        if (found := mold_stamp_present(check.comment_text)) != check.expected_mold
    )
    return MoldVerifyResult(passed=not violations, violations=violations)


def assert_mold_present(label: str, comment_text: str) -> MoldVerifyResult:
    """Assert an included userspace binary carries the mold stamp.

    Fails (``passed is False``) when mold is absent - the silent-bfd-fallback
    case that must break the gate.
    """
    return verify_mold_stamps({label: MoldCheck(comment_text, expected_mold=True)})


def assert_mold_absent(label: str, comment_text: str) -> MoldVerifyResult:
    """Assert ``vmlinux`` / an excluded PN does **not** carry the mold stamp.

    Fails (``passed is False``) when mold is present - the self-exclusion-breach
    case that must break the gate.
    """
    return verify_mold_stamps({label: MoldCheck(comment_text, expected_mold=False)})
