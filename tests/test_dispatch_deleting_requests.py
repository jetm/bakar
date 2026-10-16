"""``remote_dispatch.deleting_requests``: build requests that would delete on the host."""

from __future__ import annotations

import pytest

from bakar.steps.remote_dispatch import deleting_requests

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("target", "env", "expected"),
    [
        (None, {"KAS_TASK": "cleanall"}, ["KAS_TASK=cleanall"]),
        (None, {"KAS_TASK": "cleansstate"}, ["KAS_TASK=cleansstate"]),
        (None, {"KAS_TASK": "fetch"}, ["KAS_TASK=fetch"]),
        ("busybox:do_cleanall", {}, ["--target busybox:do_cleanall"]),
        ("busybox:cleansstate", {}, ["--target busybox:cleansstate"]),
        ("mc:x:busybox:do_clean", {}, ["--target mc:x:busybox:do_clean"]),
        (None, {"KAS_TARGET": "core-image-minimal busybox:do_clean"}, ["KAS_TARGET busybox:do_clean"]),
    ],
)
def test_deleting_requests_are_named(target: str | None, env: dict[str, str], expected: list[str]) -> None:
    assert deleting_requests(target, env) == expected


@pytest.mark.parametrize(
    ("target", "env"),
    [
        ("busybox", {}),
        ("busybox:do_build", {}),
        ("core-image-minimal", {}),
        (None, {"KAS_TASK": "build"}),
        (None, {"KAS_TASK": "do_build"}),
        (None, {"KAS_TASK": ""}),
        (None, {"KAS_TARGET": "core-image-minimal busybox"}),
        (None, {}),
    ],
)
def test_plain_builds_are_allowed(target: str | None, env: dict[str, str]) -> None:
    assert deleting_requests(target, env) == []
