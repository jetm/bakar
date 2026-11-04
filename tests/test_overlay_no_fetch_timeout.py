"""No tuning overlay may assign BB_FETCH_TIMEOUT (nothing in bitbake reads it)."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
OVERLAYS = REPO / "src" / "bakar" / "overlays"

SHIPPED = [
    OVERLAYS / "bakar-tuning-generic.yml",
    OVERLAYS / "bakar-tuning-nxp.yml",
    OVERLAYS / "bakar-tuning-ti.yml",
]
# examples/.bakar is gitignored: present only in checkouts that carry local copies.
EXAMPLE = REPO / "examples" / ".bakar" / "overlays" / "bakar-tuning-generic.yml"


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.name)
def test_shipped_overlay_has_no_fetch_timeout(path: Path) -> None:
    assert "BB_FETCH_TIMEOUT" not in path.read_text(encoding="utf-8")


def test_example_overlay_has_no_fetch_timeout() -> None:
    if not EXAMPLE.is_file():
        pytest.skip("examples/.bakar is gitignored and absent in this checkout")
    assert "BB_FETCH_TIMEOUT" not in EXAMPLE.read_text(encoding="utf-8")
