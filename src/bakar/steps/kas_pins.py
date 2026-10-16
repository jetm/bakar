"""Read a meta-avocado kas file's merged pins without touching any workspace.

This module is the only place in bakar that may pass ``finish_setup_repos``
to kas. Skipping it is safe here because the dump runs in a throwaway, empty
``KAS_WORK_DIR``: kas has nothing to move, and an include needing a repository
other than the kas file's own checkout is reported as a refusal instead of
being cloned. A build must never skip it, since kas would then stop cloning
any missing repository that no include names.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import yaml

from bakar import bsp_detect
from bakar.release_workspace import ReleaseRoutingError, release_key_from_merged

_SKIPS = ("repos_checkout", "finish_setup_repos", "repos_apply_patches")
_MISSING_MARKER = "Could not fetch all repos needed by includes"
_MISSING_PREFIX = "Missing repos:"
_DUMP_TIMEOUT_S = 120


def _dump_argv(wrapper: Path) -> list[str]:
    argv = ["kas", "dump"]
    for step in _SKIPS:
        argv += ["--skip", step]
    argv.append(str(wrapper))
    return argv


def _missing_repos(stderr: str) -> str:
    for line in stderr.splitlines():
        if _MISSING_PREFIX in line:
            return line.split(_MISSING_PREFIX, 1)[1].strip()
    return ""


def _first_error_line(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    # kas logs INFO/WARNING lines before the ERROR that explains the exit.
    for line in lines:
        if "ERROR" in line:
            return line
    return lines[0] if lines else "(no stderr)"


def read_merged_config(kas_yaml: Path, *, checkout: Path) -> dict:
    """Return the merged kas config of ``kas_yaml`` read from ``checkout``.

    Raises ReleaseRoutingError when kas cannot produce it.
    """
    with (
        tempfile.TemporaryDirectory(prefix="bakar-kas-pins-") as wrap_dir,
        tempfile.TemporaryDirectory(prefix="bakar-kas-pins-work-") as work_dir,
    ):
        wrapper = Path(wrap_dir) / "avocado-wrapper.yml"
        wrapper.write_text(bsp_detect.meta_avocado_wrapper_text(checkout, kas_yaml))
        try:
            out = subprocess.run(
                _dump_argv(wrapper),
                capture_output=True,
                text=True,
                timeout=_DUMP_TIMEOUT_S,
                check=False,
                cwd=wrap_dir,
                env={**os.environ, "KAS_WORK_DIR": work_dir},
            )
        except FileNotFoundError as exc:
            raise ReleaseRoutingError(f"cannot read the kas pins of {kas_yaml}: kas is not installed ({exc})") from exc
        except subprocess.TimeoutExpired as exc:
            raise ReleaseRoutingError(
                f"cannot read the kas pins of {kas_yaml}: kas dump did not finish within {_DUMP_TIMEOUT_S}s"
            ) from exc

    if out.returncode != 0:
        if _MISSING_MARKER in out.stderr:
            missing = _missing_repos(out.stderr) or "(kas did not name them)"
            raise ReleaseRoutingError(
                f"{kas_yaml} includes files from repositories other than its own checkout ({missing}); "
                "the release cannot be read without fetching them"
            )
        line = _first_error_line(out.stderr).replace(str(wrapper), str(kas_yaml))
        raise ReleaseRoutingError(f"kas dump of {kas_yaml} failed: {line}")

    try:
        config = yaml.safe_load(out.stdout)
    except yaml.YAMLError as exc:
        raise ReleaseRoutingError(f"kas dump of {kas_yaml} printed unparseable YAML: {exc}") from exc
    if not isinstance(config, dict):
        raise ReleaseRoutingError(f"kas dump of {kas_yaml} did not print a mapping")
    return config


def probe_release(kas_yaml: Path, checkout: Path) -> str:
    """Return the release key (``wrynose``) pinned by ``kas_yaml``."""
    return release_key_from_merged(read_merged_config(kas_yaml, checkout=checkout))
