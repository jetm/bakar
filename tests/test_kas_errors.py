"""Tests for the kas failure-phase classifier in ``bakar.kas_errors``.

The checkout fixtures use the verbatim runtime text kas emits, copied from
``kas/repos.py`` (the ``RepoRefError`` raised when a pinned commit is not on
the pinned branch). Paraphrasing it would let the test keep passing after kas
rewords the message, which is the one change that must break it.

Covers:

- The verbatim ``RepoRefError`` message classifies as ``checkout``.
- A bitbake parse error classifies as ``parse``.
- Empty and unrecognised text classify as ``undetermined``.
- A multi-line kas log carrying the checkout error among unrelated lines
  still classifies as ``checkout``.
"""

from __future__ import annotations

import pytest

from bakar.kas_errors import classify

pytestmark = pytest.mark.unit

# kas/repos.py:547-550 builds this from three f-string fragments; the value
# below is the concatenated string the user actually sees.
REPO_REF_ERROR = (
    'Branch "2.8-avocado" in repository "bitbake" does not contain commit "82cf21bc6d1a2941c2dd292ecea327031a247a8b"'
)


@pytest.mark.unit
def test_repo_ref_error_classifies_as_checkout() -> None:
    assert classify(REPO_REF_ERROR) == "checkout"


@pytest.mark.unit
def test_bitbake_parse_error_classifies_as_parse() -> None:
    text = "ERROR: ParseError at /work/meta-avocado/recipes-core/foo/foo.bb:12: unparsed line: 'inheri core-image'"
    assert classify(text) == "parse"


@pytest.mark.unit
def test_empty_text_is_undetermined() -> None:
    assert classify("") == "undetermined"


@pytest.mark.unit
def test_unrecognised_text_is_undetermined() -> None:
    assert classify("kas-container: exiting with status 1\nsomething went wrong\n") == "undetermined"


@pytest.mark.unit
def test_multiline_log_with_checkout_error_classifies_as_checkout() -> None:
    log = "\n".join(
        [
            "2026-08-04 17:02:11 - INFO     - kas 4.9 started",
            "2026-08-04 17:02:11 - INFO     - /work$ git rev-parse --show-toplevel",
            "2026-08-04 17:02:12 - INFO     - Repository bitbake already checked out",
            f"2026-08-04 17:02:13 - ERROR    - {REPO_REF_ERROR}",
            "2026-08-04 17:02:13 - ERROR    - kas failed with exit code 2",
        ]
    )
    assert classify(log) == "checkout"
