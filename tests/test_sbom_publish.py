"""Turning a build's per-image SPDX into something publishable.

The document a consumer of the feed wants is the flattened per-image inventory -
one ``software_Sbom`` root over what shipped - and it is NOT publishable as the
build emits it. Measured on a scarthgap qemux86-64 build: 9,732 nodes carrying
868 ``security_*`` vulnerability nodes and 303 distinct CVE identifiers. Feeding
that to a free feed publishes the assessment the paid tier sells.

``meta-avocado-sbom`` owns the filter that strips it. This module only locates
that filter and runs it, for the same reason ``feed.py`` shells out to
``render-pool-local.py`` rather than reimplementing repository rendering: two
implementations of one rule drift, and the one that drifts silently here leaks
vulnerability data.

The absent-filter case is a refusal rather than a skip, and that asymmetry
against ``--cve`` is deliberate. A build with no cve-check data has nothing to
report on, so skipping is honest. A checkout without the filter has a document
that must not ship and no way to make it safe - so continuing would either
publish it raw or silently publish nothing, and both are worse than stopping.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from bakar import sbom_publish

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

MACHINE = "avocado-qemux86-64"


@pytest.fixture
def cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(resolved_tmpdir=tmp_path / "tmp")


def _make_layer(root: Path, *, with_filter: bool = True) -> Path:
    """Build a meta-avocado-sbom lib tree shaped like the real one."""
    lib = root / "meta-avocado" / "meta-avocado-sbom" / "lib"
    pkg = lib / "avocado_sbom"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    if with_filter:
        (pkg / "publish.py").write_text("", encoding="utf-8")
    return lib


def test_images_dir_is_where_the_build_writes_the_flattened_document(cfg) -> None:
    """``do_create_image_sbom_spdx`` deploys to ``DEPLOY_DIR_IMAGE``."""
    assert sbom_publish.images_dir(cfg) == cfg.resolved_tmpdir / "deploy" / "images"


def test_has_filter_is_true_when_publish_module_is_present(tmp_path: Path) -> None:
    lib = _make_layer(tmp_path)

    assert sbom_publish.has_filter(lib) is True


def test_has_filter_is_false_when_the_checkout_predates_the_filter(tmp_path: Path) -> None:
    """The filter is a recent addition; an older meta-avocado has the package
    and not the module, so presence of ``avocado_sbom`` proves nothing.
    """
    lib = _make_layer(tmp_path, with_filter=False)

    assert sbom_publish.has_filter(lib) is False


def test_has_filter_is_false_when_the_lib_directory_is_absent(tmp_path: Path) -> None:
    assert sbom_publish.has_filter(tmp_path / "nope") is False


def test_find_image_sboms_matches_the_compound_suffix(cfg) -> None:
    """A build writes several JSON files per image - a manifest, testdata, a
    stone descriptor. Matching ``*.json`` would publish four files and call
    three of them inventories.
    """
    images = sbom_publish.images_dir(cfg) / MACHINE
    images.mkdir(parents=True)
    (images / "avocado-image-rootfs-qemux86-64.spdx.json").write_text("{}", encoding="utf-8")
    (images / "avocado-image-rootfs-qemux86-64.testdata.json").write_text("{}", encoding="utf-8")
    (images / "avocado-image-rootfs-qemux86-64.manifest").write_text("", encoding="utf-8")

    found = sbom_publish.find_image_sboms(sbom_publish.images_dir(cfg))

    assert [p.name for p in found] == ["avocado-image-rootfs-qemux86-64.spdx.json"]


def test_find_image_sboms_is_empty_before_the_distro_target_emits_one(cfg) -> None:
    """Until the image recipe's ``do_build`` is reached the glob matches nothing,
    which is what every distro build did before ``avocado-distro`` depended on it.
    """
    sbom_publish.images_dir(cfg).mkdir(parents=True)

    assert sbom_publish.find_image_sboms(sbom_publish.images_dir(cfg)) == []


def test_filter_command_runs_the_module_against_the_layer_lib(tmp_path: Path) -> None:
    """The filter is invoked as a module with the layer's lib on PYTHONPATH.

    Not imported into this process: it belongs to meta-avocado and its version
    tracks that checkout, so importing it would bind bakar's behaviour to
    whichever copy happened to be importable.
    """
    lib = _make_layer(tmp_path)
    out = tmp_path / "out"

    cmd, env = sbom_publish.filter_command(lib, tmp_path / "images", out)

    assert cmd[:3] == ["python3", "-m", "avocado_sbom.publish"]
    assert "--in" in cmd
    assert str(tmp_path / "images") in cmd
    assert str(out) in cmd
    assert str(lib) in env["PYTHONPATH"]


def test_filtered_documents_are_found_under_the_output_root(tmp_path: Path) -> None:
    """The filter mirrors its input tree, so the result is one level deeper."""
    out = tmp_path / "out" / MACHINE
    out.mkdir(parents=True)
    (out / "avocado-image-rootfs-qemux86-64.spdx.json").write_text("{}", encoding="utf-8")

    found = sbom_publish.find_image_sboms(tmp_path / "out")

    assert [p.name for p in found] == ["avocado-image-rootfs-qemux86-64.spdx.json"]


def test_assert_publishable_accepts_a_filtered_document(tmp_path: Path) -> None:
    """A document with no vulnerability surface passes."""
    doc = tmp_path / "clean.spdx.json"
    doc.write_text(
        json.dumps({"@graph": [{"type": "software_Package", "name": "zlib"}]}),
        encoding="utf-8",
    )

    assert sbom_publish.vulnerability_leaks(doc) == []


def test_assert_publishable_rejects_a_security_node(tmp_path: Path) -> None:
    """The exact shape the filter exists to remove."""
    doc = tmp_path / "raw.spdx.json"
    doc.write_text(
        json.dumps({"@graph": [{"type": "security_Vulnerability", "spdxId": "urn:x"}]}),
        encoding="utf-8",
    )

    assert sbom_publish.vulnerability_leaks(doc) != []


def test_assert_publishable_rejects_a_cve_identifier_anywhere(tmp_path: Path) -> None:
    """A 3.0.1 spdxId embeds the CVE it names, so an id is a leak even when the
    node type looks harmless. Measured: 303 distinct identifiers in an unfiltered
    per-image document.
    """
    doc = tmp_path / "raw.spdx.json"
    doc.write_text(
        json.dumps({"@graph": [{"type": "software_Package", "spdxId": ".../vulnerability/CVE-2021-42380"}]}),
        encoding="utf-8",
    )

    assert sbom_publish.vulnerability_leaks(doc) != []


def test_assert_publishable_reports_an_unreadable_document_rather_than_passing_it(tmp_path: Path) -> None:
    """Fail closed. An unparseable document is not a document with no CVEs in it,
    and treating the two alike is how an unchecked file reaches a public feed.
    """
    doc = tmp_path / "torn.spdx.json"
    doc.write_text("{ not json", encoding="utf-8")

    assert sbom_publish.vulnerability_leaks(doc) != []
