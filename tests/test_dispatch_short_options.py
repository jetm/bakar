"""Short-option handling shared by the two ``--on`` argv strippers.

Click reads a single-dash token letter by letter, and a value-taking letter
swallows the rest of the token. Both strippers used to ignore that: ``-tweston``
lost its ``w`` (``-t``), and ``-tmyimage`` lost its ``y`` (``-tmimage``).
"""

from __future__ import annotations

import click
import pytest
import typer

from bakar.cli import app
from bakar.steps import avocado_dispatch as ad
from bakar.steps import remote_dispatch as rd

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["-kw", "/ws", "k.yml"], ["-k", "k.yml"]),
        (["-kw/ws", "k.yml"], ["-k", "k.yml"]),
        (["-w/a", "k.yml"], ["k.yml"]),
        (["-w", "/a", "k.yml"], ["k.yml"]),
        (["--workspace=/a", "k.yml"], ["k.yml"]),
        (["--workspace", "/a", "k.yml"], ["k.yml"]),
        (["-t", "weston", "-w", "/a", "k.yml"], ["-t", "weston", "k.yml"]),
        (["-tweston", "k.yml"], ["-tweston", "k.yml"]),
        (["-twpa-supplicant", "-k", "k.yml"], ["-twpa-supplicant", "-k", "k.yml"]),
        (["-iwic-image", "-k", "k.yml"], ["-iwic-image", "-k", "k.yml"]),
        (["-tyocto", "k.yml"], ["-tyocto", "k.yml"]),
        (["-nk", "k.yml"], ["-nk", "k.yml"]),
    ],
)
def test_strip_workspace_option(args: list[str], expected: list[str]) -> None:
    assert ad.strip_workspace_option(["build", *args]) == ["build", *expected]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["-tmyimage", "k.yml"], ["-tmyimage", "k.yml"]),
        (["-tyocto", "k.yml"], ["-tyocto", "k.yml"]),
        (["-nky", "k.yml"], ["-nk", "k.yml"]),
        (["-y", "k.yml"], ["k.yml"]),
        (["-yy", "k.yml"], ["k.yml"]),
        (["-ky", "-tweston", "k.yml"], ["-k", "-tweston", "k.yml"]),
        (["--yes", "--on", "pc3", "k.yml"], ["k.yml"]),
    ],
)
def test_strip_dispatch_options(args: list[str], expected: list[str]) -> None:
    assert rd.strip_dispatch_options(["build", *args]) == ["build", *expected]


def test_yes_and_workspace_in_one_cluster_are_both_stripped() -> None:
    argv = ad.build_remote_argv(["build", "-yw", "/ws", "k.yml", "--on", "pc3"], "k.yml", "kas/k.yml")
    assert argv == ["build", "kas/k.yml"]


def test_kw_cluster_keeps_the_flag_and_drops_the_workspace() -> None:
    argv = ad.build_remote_argv(["build", "-kw", "/ws", "k.yml"], "k.yml", "kas/k.yml")
    assert argv == ["build", "-k", "kas/k.yml"]


def _short_options(params: list[click.Parameter]) -> tuple[set[str], set[str]]:
    flags: set[str] = set()
    valued: set[str] = set()
    for param in params:
        if not isinstance(param, click.Option):
            continue
        for opt in [*param.opts, *param.secondary_opts]:
            if len(opt) == 2 and opt.startswith("-") and opt != "--":
                (flags if param.is_flag else valued).add(opt[1])
    return flags, valued


def test_known_short_options_match_the_real_build_command() -> None:
    group = typer.main.get_command(app)
    assert isinstance(group, click.Group)
    build = group.commands["build"]
    flags, valued = _short_options(build.params)
    assert flags == rd.BOOL_SHORT, "bakar build's boolean short flags changed; update BOOL_SHORT"
    assert valued <= rd.VALUE_SHORT, f"value-taking short option(s) {valued - rd.VALUE_SHORT} unknown to the strippers"


def test_global_options_carry_no_short_form() -> None:
    # The forwarded argv starts with the global options, and the strippers walk
    # every single-dash token: a global short option would need classifying too.
    group = typer.main.get_command(app)
    assert _short_options(group.params) == (set(), set())
