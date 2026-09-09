"""Tests for the ``uninative-cluster-ceiling`` cluster-consistency check.

The check publishes this node's ``UNINATIVE_MAXGLIBCVERSION`` into a shared
rendezvous directory under the cluster's sstate (or downloads) export, then reads
every sibling record and compares. The ordering is the behaviour under test: a
node whose ceiling just moved must block ITSELF rather than report PASS on the
strength of a successful write.

DESTRUCTIVE-TEST GUARD: ``_uninative_ceiling_root`` resolves the shared root as
``os.environ.get("SSTATE_DIR") or cfg.sstate_dir`` (falling back to DL_DIR the
same way) - the environment wins over the config object - and the check WRITES a
record there. With ``SSTATE_DIR``/``DL_DIR`` exported in the operator's shell, a
test that pointed only ``cfg.sstate_dir`` at a fixture tree would write a real
ceiling record into the real shared cache. The module-scope
``_neutralise_cache_env`` fixture below is ``autouse=True`` and unsets both before
every test in this file, so no test can reach the real export even if it forgets
to point SSTATE_DIR anywhere.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from bakar import diagnostics
from bakar.config import BuildConfig, ResolveRequest, resolve
from bakar.diagnostics import Severity, Status, check_uninative_cluster_consistency, run_all
from bakar.user_config import UserConfig
from bakar.workspace_config import WorkspaceConfig

_CEILING = "2.44"
_CHECK_NAME = "uninative-cluster-ceiling"


@pytest.fixture(autouse=True)
def _neutralise_cache_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset the real cache environment before every test in this module.

    See the module docstring: without this, the check under test would publish a
    ceiling record into the host's real shared sstate export.
    """
    monkeypatch.delenv("SSTATE_DIR", raising=False)
    monkeypatch.delenv("DL_DIR", raising=False)
    monkeypatch.delenv("BAKAR_CLUSTER", raising=False)


def _cfg(*, host_mode: bool = True, uninative: bool = True, cluster: bool = True) -> BuildConfig:
    """Return a minimal BuildConfig for the cluster-consistency check."""
    return BuildConfig(
        workspace=Path("/tmp"),
        bsp_family="nxp",  # type: ignore[arg-type]
        machine="m",
        distro="d",
        image="i",
        manifest="x.xml",
        repo_url="https://example.com",
        repo_branch="main",
        kas_container_image="img:latest",
        host_mode=host_mode,
        uninative=uninative,
        cluster=cluster,
    )


def _patch_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, ceiling: str = _CEILING) -> None:
    """Point the check at an Arch-family os-release and a fixture fragment.

    The gate (``_uninative_gate``) requires an Arch-family host, so without the
    os-release fixture every assertion below would be vacuous against a SKIP.
    """
    release = tmp_path / "os-release"
    release.write_text('ID=arch\nID_LIKE=""\n', encoding="utf-8")
    monkeypatch.setattr(diagnostics, "_UNINATIVE_OS_RELEASE", release)

    mirror = tmp_path / "mirror"
    mirror.mkdir(parents=True, exist_ok=True)
    fragment = tmp_path / "uninative.inc"
    fragment.write_text(
        f'UNINATIVE_URL = "file://{mirror}/"\n'
        'UNINATIVE_VERSION:forcevariable = "2.44+r5+g7cba77790f32"\n'
        f'UNINATIVE_CHECKSUM[x86_64] = "{"ab" * 32}"\n'
        f'UNINATIVE_MAXGLIBCVERSION:forcevariable = "{ceiling}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(diagnostics, "_UNINATIVE_FRAGMENT", fragment)


def _set_sstate_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point SSTATE_DIR at an EXISTING fixture dir; return the ceilings dir.

    ``_uninative_ceiling_root`` returns None unless the resolved directory is
    already on disk, so the ``mkdir`` here is what separates "shared location
    available" from the no-location WARN branch.
    """
    sstate = tmp_path / "sstate"
    sstate.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SSTATE_DIR", str(sstate))
    return sstate / diagnostics._UNINATIVE_CEILING_DIRNAME


def _peer_record(ceilings: Path, node: str, ceiling: str) -> Path:
    """Write a peer's ceiling record into the rendezvous dir."""
    ceilings.mkdir(parents=True, exist_ok=True)
    record = ceilings / f"{node}.json"
    record.write_text(
        json.dumps({"node": node, "ceiling": ceiling, "written": "2026-08-12T00:00:00+0000"}),
        encoding="utf-8",
    )
    return record


@pytest.mark.unit
def test_agreement_across_two_records_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A peer reporting the same ceiling -> PASS at BLOCK, message names the peer."""
    _patch_host(monkeypatch, tmp_path)
    ceilings = _set_sstate_dir(monkeypatch, tmp_path)
    _peer_record(ceilings, "peer-node", _CEILING)

    result = check_uninative_cluster_consistency(_cfg())

    assert result.status == Status.PASS
    assert result.severity == Severity.BLOCK
    assert "peer-node" in result.message


@pytest.mark.unit
def test_peer_disagreement_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A peer on a different ceiling -> FAIL at BLOCK naming both values.

    This is the detection command for the threat model: a node that published its
    own record successfully must still block itself when a peer disagrees, because
    the ceiling feeds native task hashes and both nodes share one sstate export.
    """
    _patch_host(monkeypatch, tmp_path)
    ceilings = _set_sstate_dir(monkeypatch, tmp_path)
    _peer_record(ceilings, "peer-node", "2.41")

    result = check_uninative_cluster_consistency(_cfg())

    assert result.status == Status.FAIL
    assert result.severity == Severity.BLOCK
    assert "peer-node" in result.message
    assert "2.41" in result.message
    assert _CEILING in result.message


@pytest.mark.unit
def test_lone_record_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No peer has reported yet -> PASS, and this node's record is on disk."""
    _patch_host(monkeypatch, tmp_path)
    ceilings = _set_sstate_dir(monkeypatch, tmp_path)

    result = check_uninative_cluster_consistency(_cfg())

    assert result.status == Status.PASS
    assert result.severity == Severity.BLOCK
    own = ceilings / f"{socket.gethostname()}.json"
    assert json.loads(own.read_text(encoding="utf-8"))["ceiling"] == _CEILING


@pytest.mark.unit
def test_unreadable_peer_record_warns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A malformed peer record -> WARN naming it, never silent agreement."""
    _patch_host(monkeypatch, tmp_path)
    ceilings = _set_sstate_dir(monkeypatch, tmp_path)
    ceilings.mkdir(parents=True, exist_ok=True)
    (ceilings / "broken-node.json").write_text("{not json", encoding="utf-8")

    result = check_uninative_cluster_consistency(_cfg())

    assert result.status == Status.FAIL
    assert result.severity == Severity.WARN
    assert "broken-node.json" in result.message


@pytest.mark.unit
def test_rerun_overwrites_own_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Two runs leave exactly one record for this node, holding the current ceiling.

    A second record (or an appended one) would read as a peer disagreeing with
    itself on the next node to check.
    """
    _patch_host(monkeypatch, tmp_path, ceiling="2.41")
    ceilings = _set_sstate_dir(monkeypatch, tmp_path)
    assert check_uninative_cluster_consistency(_cfg()).status == Status.PASS

    _patch_host(monkeypatch, tmp_path, ceiling=_CEILING)
    assert check_uninative_cluster_consistency(_cfg()).status == Status.PASS

    node = socket.gethostname()
    own = sorted(p.name for p in ceilings.iterdir() if p.name.startswith(node))
    assert own == [f"{node}.json"]
    assert json.loads((ceilings / f"{node}.json").read_text(encoding="utf-8"))["ceiling"] == _CEILING


@pytest.mark.unit
def test_no_shared_location_warns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Neither dir resolving to an existing directory -> WARN, nothing published.

    ``tmp_path / "sstate"`` is deliberately NOT created here: with no shared
    location the check cannot make a claim either way, and must say so rather than
    pass.
    """
    _patch_host(monkeypatch, tmp_path)
    monkeypatch.setenv("SSTATE_DIR", str(tmp_path / "absent-sstate"))

    result = check_uninative_cluster_consistency(_cfg())

    assert result.status == Status.FAIL
    assert result.severity == Severity.WARN
    assert not (tmp_path / "absent-sstate").exists()


def _workspace(tmp_path: Path) -> Path:
    """A workspace path with the nxp subdir present (resolve() needs it)."""
    (tmp_path / "nxp").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _run_all_cfg(tmp_path: Path, *, cluster: bool) -> BuildConfig:
    """Resolve a config for the run_all membership assertions.

    ``sstate_dir``/``dl_dir`` are pinned to None so a configured host export
    cannot become the rendezvous root during a full ``run_all`` sweep.
    """
    return resolve(
        ResolveRequest(
            workspace=_workspace(tmp_path),
            bsp_family="nxp",
            user_config=UserConfig(cluster=cluster, sstate_dir=None, dl_dir=None),
            workspace_config=WorkspaceConfig(),
        )
    )


@pytest.mark.unit
def test_gating_ceiling_check_absent_when_cluster_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """cluster=False: run_all lists no uninative-cluster-ceiling check."""
    _patch_host(monkeypatch, tmp_path)
    monkeypatch.setattr(diagnostics, "_UNINATIVE_FRAGMENT", tmp_path / "absent-uninative.inc")
    assert _CHECK_NAME not in {r.name for r in run_all(_run_all_cfg(tmp_path, cluster=False))}


@pytest.mark.unit
def test_gating_ceiling_check_present_when_cluster_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """cluster=True: the uninative-cluster-ceiling check appears.

    The fragment is pointed at an absent path so the check skips-with-info instead
    of publishing a record while every other check in ``run_all`` runs for real.
    """
    _patch_host(monkeypatch, tmp_path)
    monkeypatch.setattr(diagnostics, "_UNINATIVE_FRAGMENT", tmp_path / "absent-uninative.inc")
    assert _CHECK_NAME in {r.name for r in run_all(_run_all_cfg(tmp_path, cluster=True))}
