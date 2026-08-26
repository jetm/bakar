"""Locating a build's cve-check input and the report it produces.

The two paths here are read off the same ``DEPLOY_DIR`` the build wrote, and
they are separate functions because they answer different questions at
different moments. ``cve_data_dir`` is an INPUT check - it decides whether
running the report recipe is worth a bitbake invocation at all - while
``report_path`` names an OUTPUT that does not exist until that invocation has
run.

The input check is what keeps the post-build step honest. ``do_cve_report``
already fails with a clear message when ``CVE_CHECK_DIR`` is empty, but paying a
whole kas startup to be told so is a minute of the user's time spent learning
something a ``glob`` answers for free. Everything below is scoped to that: no
attempt to decide whether the CVE data is CORRECT, only whether it is there.

MACHINE is a parameter rather than read off the config, mirroring
``_finish_build``. The recipe derives both the directory and the report filename
from bitbake's ``${MACHINE}``, which is the value naming the ``deploy/images``
subdir - and on the bbsetup path that is a translated name rather than
``cfg.machine``. Reading the config here would put the report under one name and
the images under another on exactly the builds where they diverge.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from bakar import cve_report

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

MACHINE = "avocado-qemux86-64"


@pytest.fixture
def cfg(tmp_path: Path) -> SimpleNamespace:
    """The minimum the locators read off a BuildConfig."""
    return SimpleNamespace(resolved_tmpdir=tmp_path / "tmp")


def _write_cve_data(cve_dir: Path, *recipes: str) -> None:
    cve_dir.mkdir(parents=True, exist_ok=True)
    for recipe in recipes:
        (cve_dir / f"{recipe}_cve.json").write_text("{}", encoding="utf-8")


def test_cve_data_dir_is_machine_scoped(cfg) -> None:
    """The layer configures ``CVE_CHECK_DIR = ${DEPLOY_DIR}/cve/${MACHINE}``.

    Machine-scoped is not cosmetic: the recipe warns that an unscoped directory
    both leaks other machines' recipes into the report and drops CVEs for
    recipes they pinned to another version.
    """
    assert cve_report.cve_data_dir(cfg, MACHINE) == cfg.resolved_tmpdir / "deploy" / "cve" / MACHINE


def test_report_path_names_the_machine(cfg) -> None:
    """``AVOCADO_CVE_REPORT_FILE`` default, mirrored so the caller can print it."""
    expected = cfg.resolved_tmpdir / "deploy" / "avocado-cve" / f"avocado-cve-report-{MACHINE}.json"
    assert cve_report.report_path(cfg, MACHINE) == expected


def test_both_paths_use_the_machine_they_are_given(cfg) -> None:
    """Neither path may fall back to a machine read off the config.

    On the bbsetup path the deploy-tree machine is a translated name, so a
    locator reading ``cfg.machine`` would file the report under a different
    machine from the one whose CVE data it summarises - and the mismatch is
    silent, because both paths still exist.
    """
    other = "avocado-imx93-frdm"

    assert other in str(cve_report.cve_data_dir(cfg, other))
    assert other in cve_report.report_path(cfg, other).name


def test_has_cve_data_is_false_when_the_directory_is_absent(cfg) -> None:
    """A build with no cve-check inherited writes no directory at all."""
    assert cve_report.has_cve_data(cve_report.cve_data_dir(cfg, MACHINE)) is False


def test_has_cve_data_is_false_when_the_directory_is_empty(cfg) -> None:
    """Present but empty is the interrupted-build shape, not a scanned build."""
    cve_report.cve_data_dir(cfg, MACHINE).mkdir(parents=True)

    assert cve_report.has_cve_data(cve_report.cve_data_dir(cfg, MACHINE)) is False


def test_has_cve_data_is_false_when_nothing_matches_the_cve_suffix(cfg) -> None:
    """cve-check writes ``<PN>_cve.json``; the opt-out class writes
    ``<PN>_optout.json`` into the SAME directory.

    A check for "any *.json" would therefore read a build where every recipe
    opted out - and none was scanned - as a scanned build, and hand the recipe a
    directory it fails on.
    """
    cve_dir = cve_report.cve_data_dir(cfg, MACHINE)
    cve_dir.mkdir(parents=True)
    (cve_dir / "packagegroup-base_optout.json").write_text("{}", encoding="utf-8")

    assert cve_report.has_cve_data(cve_dir) is False


def test_has_cve_data_is_true_with_one_scanned_recipe(cfg) -> None:
    """One is enough. The recipe's own gate is ``if not cve_files``, so this
    reports what it will decide rather than imposing a stricter floor of its own.
    """
    _write_cve_data(cve_report.cve_data_dir(cfg, MACHINE), "glibc")

    assert cve_report.has_cve_data(cve_report.cve_data_dir(cfg, MACHINE)) is True


def test_has_cve_data_ignores_a_nested_directory_named_like_a_result(cfg) -> None:
    """A directory matching the glob is not a result file."""
    cve_dir = cve_report.cve_data_dir(cfg, MACHINE)
    (cve_dir / "glibc_cve.json").mkdir(parents=True)

    assert cve_report.has_cve_data(cve_dir) is False
