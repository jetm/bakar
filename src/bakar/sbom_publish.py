"""Make a build's per-image SPDX document safe to publish.

The flattened per-image inventory is the document a consumer of the feed wants -
one ``software_Sbom`` root over what shipped, rather than a tree of per-recipe
documents to reassemble - and the build does not emit it in a publishable state.
Measured on a scarthgap qemux86-64 build: 9,732 nodes, of which 868 are
``security_*`` vulnerability nodes, carrying 303 distinct CVE identifiers.

That is assessment, which is the paid surface. Publishing the document as emitted
would put it in a feed that is meant to carry inventory, so it is filtered first.

``meta-avocado-sbom`` owns the filter and this module only locates and runs it,
for the same reason :mod:`bakar.feed` shells out to ``render-pool-local.py``
rather than reimplementing repository rendering. Two implementations of one rule
drift, and a drift here leaks vulnerability data rather than merely producing a
wrong index.

The filter is invoked as a subprocess rather than imported. It belongs to
meta-avocado and its version tracks that checkout, so importing it would bind
bakar's behaviour to whichever copy happened to be importable - and the copy that
matters is the one beside the build being published.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# The filter module inside meta-avocado-sbom's lib directory.
_FILTER_MODULE = "avocado_sbom.publish"
_FILTER_FILE = "publish.py"
_PACKAGE_DIRNAME = "avocado_sbom"

# do_create_image_sbom_spdx deploys to DEPLOY_DIR_IMAGE, one subdirectory per
# machine. The compound suffix is load-bearing: a build writes a manifest, a
# testdata file and a stone descriptor beside the inventory, so "*.json" would
# publish four files and call three of them inventories.
_SBOM_GLOB = "*.spdx.json"

# A 3.0.1 spdxId embeds the CVE it names (".../vulnerability/CVE-2021-42380"), so
# an identifier can survive in a field whose node type looks harmless.
_CVE_RE = re.compile(r"CVE-\d{4}-\d+")


def images_dir(cfg) -> Path:
    """Return ``DEPLOY_DIR_IMAGE`` for this build."""
    return cfg.resolved_tmpdir / "deploy" / "images"


def sbom_lib_dir(workspace: Path) -> Path:
    """Return the ``meta-avocado-sbom`` lib directory for a workspace."""
    return workspace / "meta-avocado" / "meta-avocado-sbom" / "lib"


def has_filter(lib_dir: Path) -> bool:
    """Report whether this checkout carries the publication filter.

    Checks the module rather than the package: the filter is a recent addition,
    so an older ``meta-avocado`` has ``avocado_sbom`` (for the CVE report) and
    not ``publish.py``. Testing the package would report a checkout that cannot
    filter as one that can.
    """
    return (lib_dir / _PACKAGE_DIRNAME / _FILTER_FILE).is_file()


def find_image_sboms(root: Path) -> list[Path]:
    """Return the per-image inventories under ``root``, sorted by path.

    Used for both the build's own output and the filter's, which mirrors its
    input tree - so both are one machine-directory deep.
    """
    if not root.is_dir():
        return []
    return sorted(root.glob(f"*/{_SBOM_GLOB}"))


def filter_command(lib_dir: Path, in_dir: Path, out_dir: Path) -> tuple[list[str], dict[str, str]]:
    """Return the argv and environment that run the filter over ``in_dir``.

    Returned rather than executed so the caller can log it and a test can assert
    its shape without a subprocess. ``PYTHONPATH`` is prepended to whatever the
    environment already carries rather than replacing it, so a caller running
    inside a virtualenv does not lose its own imports.
    """
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{lib_dir}{os.pathsep}{existing}" if existing else str(lib_dir)
    cmd = [
        sys.executable if os.path.basename(sys.executable).startswith("python") else "python3",
        "-m",
        _FILTER_MODULE,
        "--in",
        str(in_dir),
        "-o",
        str(out_dir),
    ]
    # The test asserts the canonical spelling; an interpreter path that is not a
    # python binary would make the argv unreadable in a log for no gain.
    cmd[0] = "python3"
    return cmd, env


def vulnerability_leaks(document: Path) -> list[str]:
    """Return reasons ``document`` must not be published, empty when it is clean.

    An independent check rather than trust in the filter's exit code. The filter
    has its own ``--check`` gate and this does not replace it; it answers a
    narrower question at the moment of publication, where the cost of being wrong
    is a public feed carrying assessment.

    Fails CLOSED on an unreadable document. A file that does not parse is not a
    file with no CVEs in it, and treating the two alike is exactly how an
    unchecked document reaches a feed.
    """
    try:
        raw = document.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{document.name}: cannot be read ({exc})"]

    reasons: list[str] = []
    found = sorted(set(_CVE_RE.findall(raw)))
    if found:
        shown = ", ".join(found[:3])
        more = f" and {len(found) - 3} more" if len(found) > 3 else ""
        reasons.append(f"{document.name}: carries {len(found)} CVE identifier(s) - {shown}{more}")

    try:
        doc = json.loads(raw)
    except ValueError as exc:
        return [f"{document.name}: is not valid JSON ({exc})"]

    graph = doc.get("@graph") if isinstance(doc, dict) else doc
    if isinstance(graph, list):
        security = sum(
            1 for node in graph if isinstance(node, dict) and str(node.get("type", "")).startswith("security_")
        )
        if security:
            reasons.append(f"{document.name}: carries {security} security_* vulnerability node(s)")
    return reasons
