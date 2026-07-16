"""bakar: practical kas wrapper for Yocto BSP development."""

from __future__ import annotations

import hashlib
from pathlib import Path

__version__ = "0.22.0"


def package_identity() -> str:
    """Return a short content hash of the installed bakar package.

    Hashes every file under the package directory (sources plus the ``overlays/``
    tree) except Python bytecode and caches, so two installs with byte-identical
    code and overlays produce the same id. ``bakar build --on <host>`` compares
    it against the remote's to refuse a dispatch when the remote bakar would
    build with different code or overlays (for example a stale meta-bakar-mold) -
    a drift the static ``__version__`` alone cannot detect.
    """
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:12]
