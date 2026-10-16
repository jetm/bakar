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


@pytest.mark.parametrize(
    ("image", "env", "expected"),
    [
        ("core-image-minimal:do_cleanall", {}, ["--image core-image-minimal:do_cleanall"]),
        ("core-image-minimal:cleansstate", {}, ["--image core-image-minimal:cleansstate"]),
        (None, {"BAKAR_IMAGE": "core-image-minimal:do_clean"}, ["BAKAR_IMAGE core-image-minimal:do_clean"]),
        (None, {"BAKAR_IMAGE": "mc:x:core-image-minimal:cleanall"}, ["BAKAR_IMAGE mc:x:core-image-minimal:cleanall"]),
        ("core-image-minimal", {}, []),
        ("core-image-minimal:do_build", {"BAKAR_IMAGE": "var-thin-image"}, []),
    ],
    ids=["image-do-cleanall", "image-cleansstate", "bakar-image", "bakar-image-mc", "plain-image", "plain-both"],
)
def test_image_sources_are_checked(image: str | None, env: dict[str, str], expected: list[str]) -> None:
    # The manifest flow's generated kas file uses the image as its target, so
    # a clean task there deletes on the host exactly like one in --target.
    assert deleting_requests(None, env, image=image) == expected


def test_every_source_is_named_in_order() -> None:
    env = {"KAS_TASK": "cleanall", "BAKAR_IMAGE": "a:do_clean", "KAS_TARGET": "b:cleansstate"}
    assert deleting_requests("c:do_cleanall", env, image="d:clean") == [
        "KAS_TASK=cleanall",
        "--target c:do_cleanall",
        "--image d:clean",
        "BAKAR_IMAGE a:do_clean",
        "KAS_TARGET b:cleansstate",
    ]
