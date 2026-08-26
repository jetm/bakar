"""``bakar build --sbom``: filter the per-image SPDX into something publishable.

Hangs off ``_finish_build`` like ``--feed`` and ``--cve``, so "only on a build
that actually happened" is a property of where it sits rather than a guard each
call site remembers.

Where it deliberately differs from ``--cve`` is the missing-prerequisite case,
and the asymmetry is the point:

* ``--cve`` SKIPS when the build carries no cve-check data. A build without
  cve-check inherited is ordinary, nothing was scanned, and there is honestly
  nothing to report on.
* ``--sbom`` REFUSES BEFORE THE BUILD when the checkout has no filter. The
  document exists either way and must not ship unfiltered, so continuing would
  end in either publishing it raw or silently publishing nothing. Both are worse
  than exiting before an hour of build time is spent.

That second one is ``--feed``'s economics argument applied to a different
prerequisite: a missing filter costs milliseconds to detect now and a whole
build's wall clock to discover at the end.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from bakar import sbom_publish
from bakar.commands import build as build_mod

pytestmark = pytest.mark.unit

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

MACHINE = "avocado-qemux86-64"


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


@pytest.fixture
def cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        host_mode=True,
        resolved_tmpdir=tmp_path / "tmp",
        machine=MACHINE,
        workspace=tmp_path / "ws",
        kas_yaml=tmp_path / "ws" / "machine.yml",
        effective_feed_dir=tmp_path / "_feed",
    )


@pytest.fixture
def log_stub(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        run_id="r0",
        run_dir=tmp_path / "run",
        start_monotonic=time.monotonic(),
        step_skip=lambda *_a, **_kw: None,
        info=lambda *_a, **_kw: None,
    )


def _install_filter(cfg) -> Path:
    """Give the workspace a meta-avocado-sbom checkout that carries the filter."""
    lib = sbom_publish.sbom_lib_dir(cfg.workspace)
    (lib / "avocado_sbom").mkdir(parents=True)
    (lib / "avocado_sbom" / "publish.py").write_text("", encoding="utf-8")
    return lib


def _write_image_sbom(cfg, *, clean: bool = True) -> Path:
    images = sbom_publish.images_dir(cfg) / MACHINE
    images.mkdir(parents=True, exist_ok=True)
    doc = images / "avocado-image-rootfs-qemux86-64.spdx.json"
    node = {"type": "software_Package", "name": "zlib"} if clean else {"type": "security_Vulnerability"}
    doc.write_text(json.dumps({"@graph": [node]}), encoding="utf-8")
    return doc


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record the filter subprocess without running it, and fake its output."""
    recorded: list[list[str]] = []

    def record(cmd, **kwargs):
        recorded.append(cmd)
        # Mirror what the real filter does: write a cleaned document into --out.
        out = Path(cmd[cmd.index("-o") + 1]) / MACHINE
        out.mkdir(parents=True, exist_ok=True)
        (out / "avocado-image-rootfs-qemux86-64.spdx.json").write_text(
            json.dumps({"@graph": [{"type": "software_Package", "name": "zlib"}]}), encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build_mod.subprocess, "run", record)
    return recorded


def test_failed_build_does_not_filter(runs, cfg, log_stub) -> None:
    """A failed build's document describes a package set that never shipped."""
    _write_image_sbom(cfg)

    with pytest.raises(typer.Exit):
        build_mod._finish_build(cfg, log_stub, 1, MACHINE, sbom=build_mod._SbomRequest(workspace=cfg.workspace))

    assert runs == []


def test_successful_build_runs_the_filter(runs, cfg, log_stub) -> None:
    _install_filter(cfg)
    _write_image_sbom(cfg)

    build_mod._finish_build(cfg, log_stub, 0, MACHINE, sbom=build_mod._SbomRequest(workspace=cfg.workspace))

    assert len(runs) == 1
    assert runs[0][:3] == ["python3", "-m", "avocado_sbom.publish"]


def test_no_image_sbom_says_so_rather_than_filtering_nothing(
    runs, cfg, log_stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """Before avocado-distro depended on the image recipe's do_build, every
    distro build landed here - so the message has to name the cause rather than
    report an empty success.
    """
    _install_filter(cfg)
    sbom_publish.images_dir(cfg).mkdir(parents=True)

    build_mod._finish_build(cfg, log_stub, 0, MACHINE, sbom=build_mod._SbomRequest(workspace=cfg.workspace))

    assert runs == []
    assert "no per-image SBOM" in _plain(capsys.readouterr().err)


def test_a_leaking_filter_output_is_refused_rather_than_reported_as_published(
    monkeypatch: pytest.MonkeyPatch, cfg, log_stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """The independent check is the point of the whole step.

    If the filter's output still carries vulnerability data - a filter bug, a
    version mismatch, a document shape it did not anticipate - saying so beats
    printing a path that a later publish step will happily consume.
    """
    _install_filter(cfg)
    _write_image_sbom(cfg)

    def leaky(cmd, **_kw):
        out = Path(cmd[cmd.index("-o") + 1]) / MACHINE
        out.mkdir(parents=True, exist_ok=True)
        (out / "leaky.spdx.json").write_text(
            json.dumps({"@graph": [{"type": "security_Vulnerability"}]}), encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build_mod.subprocess, "run", leaky)

    build_mod._finish_build(cfg, log_stub, 0, MACHINE, sbom=build_mod._SbomRequest(workspace=cfg.workspace))

    err = _plain(capsys.readouterr().err)
    assert "not publishable" in err
    assert "security_" in err


def test_a_failing_filter_does_not_fail_the_build(
    monkeypatch: pytest.MonkeyPatch, cfg, log_stub, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same contract as --feed and --cve: the build's artifacts are on disk."""
    _install_filter(cfg)
    _write_image_sbom(cfg)
    monkeypatch.setattr(
        build_mod.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )

    build_mod._finish_build(cfg, log_stub, 0, MACHINE, sbom=build_mod._SbomRequest(workspace=cfg.workspace))

    assert "SBOM" in _plain(capsys.readouterr().err)


def test_no_request_runs_nothing(runs, cfg, log_stub) -> None:
    _install_filter(cfg)
    _write_image_sbom(cfg)

    build_mod._finish_build(cfg, log_stub, 0, MACHINE)

    assert runs == []


def test_dry_run_resolves_to_no_request(cfg, capsys: pytest.CaptureFixture[str]) -> None:
    _install_filter(cfg)

    assert build_mod._resolve_sbom_request(cfg, sbom=True, dry_run=True) is None
    assert "--sbom" in _plain(capsys.readouterr().err)


def test_the_flag_off_resolves_to_no_request(cfg) -> None:
    assert build_mod._resolve_sbom_request(cfg, sbom=False, dry_run=False) is None


def test_a_checkout_without_the_filter_refuses_before_the_build(cfg, capsys: pytest.CaptureFixture[str]) -> None:
    """Exits rather than returning None. Returning None would build for an hour
    and then print that the thing the user asked for was never possible.
    """
    with pytest.raises(typer.Exit) as exc:
        build_mod._resolve_sbom_request(cfg, sbom=True, dry_run=False)

    assert exc.value.exit_code == 2
    assert "filter" in _plain(capsys.readouterr().err)


def test_a_checkout_with_the_filter_resolves_to_a_request(cfg) -> None:
    _install_filter(cfg)

    request = build_mod._resolve_sbom_request(cfg, sbom=True, dry_run=False)

    assert request is not None
    assert request.workspace == cfg.workspace
