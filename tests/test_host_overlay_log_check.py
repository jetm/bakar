"""Tests for the host-mode overlay's log_check exclusion.

dnf-native cannot resolve the inherited locale inside the build environment and
falls back to C. The rootfs assembles fine, but oe/rootfs.py's log_check matches
the word "Failed" and promotes the warning to a hard do_rootfs failure, so a
build where every task succeeded still exits non-zero.

The exclusion rides the host-mode gate rather than the Arch+uninative gate used
by ``bakar-tuning-arch-probes``: nothing about this failure involves the
uninative loader, and a container build gets the image's own locale rather than
inheriting the host's.
"""

from __future__ import annotations

import pytest

from bakar.config import _overlay_dir

OVERLAY = "bakar-tuning-host.yml"

pytestmark = pytest.mark.unit


def _overlay_body() -> str:
    return (_overlay_dir() / OVERLAY).read_text(encoding="utf-8")


def test_overlay_excludes_the_locale_warning() -> None:
    """The host overlay must carry the log_check exclusion."""
    body = _overlay_body()

    assert "IMAGE_LOG_CHECK_EXCLUDES" in body
    assert "Failed.to.set.locale" in body


def test_exclusion_regex_carries_no_literal_space() -> None:
    """oe/rootfs.py splits IMAGE_LOG_CHECK_EXCLUDES on " ".

    A multi-word regex is therefore shredded into fragments that each match far
    more than intended, so the pattern has to spell its spaces as ".". This is
    the failure the value is most likely to regress into, since the message it
    matches is four words long.
    """
    body = _overlay_body()

    # Match the assignment, not the prose above it that names the same variable.
    value = next(line for line in body.splitlines() if "IMAGE_LOG_CHECK_EXCLUDES +=" in line)
    pattern = value.split("+=", 1)[1].strip().strip('"')

    assert pattern
    assert " " not in pattern
