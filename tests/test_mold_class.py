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


@pytest.mark.unit
def test_marking_check_defaults_to_a_warning() -> None:
    """A loss of BTI/PAC marking must be visible by default but must not break existing builds."""
    text = _CLASS.read_text()
    assert re.search(r'^MOLD_MARKING_CHECK \?\?= "warn"$', text, re.MULTILINE)


@pytest.mark.unit
def test_marking_check_is_hooked_into_aarch64_images_only() -> None:
    """Per-recipe QA is an sstate task, so a cached recipe never reports; the scan belongs on the image."""
    text = _CLASS.read_text()
    hook = re.search(r"python \(\) \{\n(    if not bb\.data\.inherits_class\('image', d\):.*?)\n\}", text, re.DOTALL)
    assert hook, "image hook not found"
    body = hook.group(1)
    assert "TARGET_ARCH') != 'aarch64'" in body
    assert "ROOTFS_POSTPROCESS_COMMAND" in body
    assert "mold_report_marking" in body
    assert re.search(r"^python mold_report_marking \(\) \{$", text, re.MULTILINE)


@pytest.mark.unit
def test_marking_check_finds_its_helper_next_to_the_classes_directory() -> None:
    """The class adds <classes>/../lib to sys.path itself, so the helper has to live exactly there."""
    text = _CLASS.read_text()
    assert "os.path.join(d.getVar('MOLD_CLASSDIR') or '', '..', 'lib')" in text
    assert "addpylib" not in text
    assert (_CLASS.parent.parent / "lib/bakar_mold/__init__.py").is_file()
    assert (_CLASS.parent.parent / "lib/bakar_mold/aarch64_marking.py").is_file()


def _debt_markers(text: str) -> list[str]:
    """Return each ``devtool-debt:`` comment block in the class, joined into one string."""
    blocks: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("# devtool-debt:"):
            continue
        block = [line.removeprefix("# ")]
        for follow in lines[index + 1 :]:
            if not follow.startswith("# "):
                break
            block.append(follow.removeprefix("# "))
        blocks.append(" ".join(block))
    return blocks


@pytest.mark.unit
def test_debt_markers_name_a_ceiling_and_an_upgrade_trigger() -> None:
    """A marker without a trigger never gets replaced, and one without a ceiling cannot be judged."""
    markers = _debt_markers(_CLASS.read_text())
    assert len(markers) == 2, "expected one marker for the flag strip and one for the marking check"
    for marker in markers:
        assert "Ceiling:" in marker, marker[:60]
        assert "Upgrade trigger:" in marker, marker[:60]
        assert "rui314/mold#1725" in marker, marker[:60]


@pytest.mark.unit
def test_marking_comment_points_at_the_commit_that_reverted_the_support() -> None:
    """The next reader should find why mold lacks the marking without redoing the research."""
    assert "6c0d688966" in _CLASS.read_text()
