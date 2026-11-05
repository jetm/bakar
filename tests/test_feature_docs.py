"""Doc-coverage checks for the init feature catalog."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bakar.setup.features import CATALOG

DOCS = Path(__file__).resolve().parent.parent / "docs"


@pytest.mark.unit
@pytest.mark.parametrize("feature", CATALOG, ids=lambda f: f.id)
def test_catalog_guide_exists_with_host_needs(feature) -> None:
    rel = feature.guide.split("#", 1)[0]
    assert rel.startswith("docs/")
    page = DOCS.parent / rel
    assert page.is_file(), f"{feature.id}: guide {rel} is missing"
    assert "## What your host needs" in page.read_text()


@pytest.mark.unit
def test_features_index_links_every_feature_page() -> None:
    index = (DOCS / "features" / "index.md").read_text()
    pages = {Path(f.guide.split("#", 1)[0]).name for f in CATALOG}
    for page in sorted(pages):
        assert re.search(rf"\]\({re.escape(page)}[)#]", index), f"index.md does not link {page}"


@pytest.mark.unit
@pytest.mark.parametrize("option", ["--feature", "--disable", "--set", "--yes", "--explain"])
def test_init_doc_names_option(option: str) -> None:
    assert option in (DOCS / "init.md").read_text()


@pytest.mark.unit
@pytest.mark.parametrize("check", ["sstate-mirror-reachable", "dl-mirror-reachable", "scheduler", "central-endpoints"])
def test_doctor_doc_names_check(check: str) -> None:
    assert check in (DOCS / "doctor.md").read_text()


@pytest.mark.unit
def test_docs_index_links_features_index() -> None:
    assert "features/index.md" in (DOCS / "index.md").read_text()
