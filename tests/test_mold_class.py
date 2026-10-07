"""Static invariants of mold.bbclass that only show up as a failed Yocto build.

The class runs inside bitbake, so these tests read its text. They pin the parts
where a plausible edit silently disables a fix.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import bakar

_CLASS = Path(bakar.__file__).parent / "overlays/meta-bakar-mold/classes/mold.bbclass"


@pytest.mark.unit
def test_gcs_report_flag_is_stripped_for_mold() -> None:
    """mold has no -z gcs*; meson's --fatal-warnings turns its warning into a failed link probe."""
    text = _CLASS.read_text()
    default = re.search(r'^MOLD_UNSUPPORTED_LDFLAGS \?= "(.*)"$', text, re.MULTILINE)
    assert default, "MOLD_UNSUPPORTED_LDFLAGS default not found"
    assert "-Wl,-z,gcs-report-dynamic=none" in default.group(1).split()


@pytest.mark.unit
def test_ldflags_remove_uses_setvar_not_appendvar() -> None:
    """appendVar on a ':remove' name is a silent no-op in bitbake's datastore, so the flag would stay."""
    text = _CLASS.read_text()
    assert re.search(r"d\.setVar\('LDFLAGS:remove'", text)
    assert not re.search(r"d\.appendVar\('LDFLAGS:remove'", text)
