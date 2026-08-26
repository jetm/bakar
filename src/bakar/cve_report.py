"""Where a build leaves its cve-check results, and where the report lands.

``meta-avocado-sbom`` produces one document joining every runtime package to the
unpatched CVEs of the recipe that built it. Producing it is a second bitbake
invocation - ``avocado-cve-report`` carries ``EXCLUDE_FROM_WORLD = "1"``, so no
image build reaches it - and this module holds the two paths that invocation
reads and writes, so the caller can decide whether it is worth running and can
name the result afterwards.

Both are derived from ``${MACHINE}``, which is why it is a parameter rather than
read off the config: the recipe interpolates bitbake's value into both
``CVE_CHECK_DIR`` and ``AVOCADO_CVE_REPORT_FILE``, and that is the same name the
``deploy/images`` subdir carries - not necessarily ``cfg.machine``.

Nothing here parses a report or judges its contents. The recipe already verifies
its own output against ``meta-avocado-sbom``'s frozen contract and fails the task
on a violation, so a second opinion computed here would be a copy of that check
drifting against it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# The recipe's own default for AVOCADO_CVE_REPORT_DIR, relative to DEPLOY_DIR.
_REPORT_DIRNAME = "avocado-cve"

# kas/feature/cve-check.yml scopes CVE_CHECK_DIR to ${DEPLOY_DIR}/cve/${MACHINE}.
# cve-check's own default is unscoped; the recipe warns when it finds one,
# because a shared directory both admits other machines' recipes and drops CVEs
# for recipes they pinned to a different version.
_CVE_DIRNAME = "cve"

# cve-check writes one of these per scanned recipe. The suffix is load-bearing:
# avocado-cve-optout.bbclass writes <PN>_optout.json into the SAME directory, so
# a "*.json" glob reports a build where every recipe opted out - and none was
# scanned - as a scanned build.
_CVE_RESULT_GLOB = "*_cve.json"


def cve_data_dir(cfg, machine: str) -> Path:
    """Return ``CVE_CHECK_DIR`` for ``machine`` under this build's deploy tree."""
    return cfg.resolved_tmpdir / "deploy" / _CVE_DIRNAME / machine


def report_path(cfg, machine: str) -> Path:
    """Return the report the recipe writes for ``machine``."""
    return cfg.resolved_tmpdir / "deploy" / _REPORT_DIRNAME / f"avocado-cve-report-{machine}.json"


def has_cve_data(cve_dir: Path) -> bool:
    """Report whether ``cve_dir`` holds any cve-check result to summarise.

    Mirrors the recipe's own ``if not cve_files`` gate rather than imposing a
    floor of its own, so a build this accepts is one the recipe will accept.
    That keeps the two from disagreeing: a stricter check here would refuse to
    run a report the recipe would have produced.

    Existence only. Whether the results are complete is not decidable from a
    listing, and the recipe already fails on a directory of truncated files.
    """
    return any(entry.is_file() for entry in cve_dir.glob(_CVE_RESULT_GLOB))
