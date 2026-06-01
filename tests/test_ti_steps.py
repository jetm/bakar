"""Unit tests for bakar.steps.ti_layertool and bakar.steps.ti_setup_env.

Covers the extracted argv/text helpers (``_build_layertool_cmd``,
``_strip_dl_dir``), the small filesystem helpers (``_record_active_config``,
``reset_sources``), the ``_set_or_replace`` regex-driven text transform in
``ti_setup_env``, and the full ``ti_setup_env.run`` flow on a ``tmp_path``
workspace.

All tests are hermetic: no subprocess is invoked anywhere (``ti_setup_env.run``
makes no subprocess call by design, and the layertool helpers under test here
do not invoke the script - they only build argv or rewrite local.conf).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from bakar.config import BuildConfig
from bakar.steps import ti_layertool, ti_setup_env

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ti_cfg(
    workspace: Path,
    *,
    machine: str = "am62x-var-som",
    distro: str = "arago",
    manifest: str = "processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
) -> BuildConfig:
    """Construct a TI BuildConfig matching the existing test suite's shape."""
    return BuildConfig(
        workspace=workspace,
        bsp_family="ti",
        machine=machine,
        distro=distro,
        image="var-thin-image",
        manifest=manifest,
        repo_url="https://example.invalid/none.git",
        repo_branch="scarthgap_11.00.09.04_var01",
        container_image="jetm/kas-build-env:latest",
    )


# ---------------------------------------------------------------------------
# _build_layertool_cmd
# ---------------------------------------------------------------------------


def test_build_layertool_cmd_no_force_init(tmp_path: Path) -> None:
    """Without force_init, argv is exactly the eight tokens; no ``-r``."""
    cfg = _ti_cfg(tmp_path)

    cmd = ti_layertool._build_layertool_cmd(cfg, force_init=False)

    assert cmd[0] == "bash"
    assert cmd[1] == "./oe-layertool-setup.sh"
    assert cmd[2] == "-f"
    # ``-f`` value is relative to the layertool checkout, i.e. configs/variscite/<manifest>
    assert cmd[3] == f"configs/variscite/{cfg.manifest}"
    assert cmd[4] == "-b"
    assert cmd[5] == str(tmp_path / "ti")
    assert cmd[6] == "-d"
    # ``-d`` defaults to /tmp/yocto-downloads when DL_DIR env is unset
    assert cmd[7]  # value present
    assert "-r" not in cmd
    assert len(cmd) == 8


def test_build_layertool_cmd_force_init_appends_r(tmp_path: Path) -> None:
    """``force_init=True`` adds ``-r`` as the final argv token."""
    cfg = _ti_cfg(tmp_path)

    cmd = ti_layertool._build_layertool_cmd(cfg, force_init=True)

    assert cmd[-1] == "-r"
    assert len(cmd) == 9


def test_build_layertool_cmd_honors_dl_dir_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``-d`` reflects the ``DL_DIR`` environment variable when set."""
    monkeypatch.setenv("DL_DIR", "/srv/yocto/downloads")
    cfg = _ti_cfg(tmp_path)

    cmd = ti_layertool._build_layertool_cmd(cfg)

    assert cmd[6] == "-d"
    assert cmd[7] == "/srv/yocto/downloads"


def test_build_layertool_cmd_default_dl_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without DL_DIR set, ``-d`` falls back to ``/tmp/yocto-downloads``."""
    monkeypatch.delenv("DL_DIR", raising=False)
    cfg = _ti_cfg(tmp_path)

    cmd = ti_layertool._build_layertool_cmd(cfg)

    assert cmd[7] == "/tmp/yocto-downloads"


def test_build_layertool_cmd_varies_with_manifest(tmp_path: Path) -> None:
    """``-f`` reflects ``cfg.manifest`` directly under configs/variscite/."""
    cfg = _ti_cfg(
        tmp_path,
        manifest="processor-sdk-scarthgap-non-chromium-11.00.09.04-config_var02.txt",
    )

    cmd = ti_layertool._build_layertool_cmd(cfg)

    assert cmd[3] == "configs/variscite/processor-sdk-scarthgap-non-chromium-11.00.09.04-config_var02.txt"


def test_build_layertool_cmd_ignores_machine_distro(tmp_path: Path) -> None:
    """The layertool argv does not encode ``cfg.machine`` or ``cfg.distro``.

    Those land in ``ti_setup_env``'s ``local.conf`` rewrite, not here.
    Asserting absence prevents an accidental coupling.
    """
    cfg_a = _ti_cfg(tmp_path, machine="am62x-var-som", distro="arago")
    cfg_b = _ti_cfg(tmp_path, machine="am335x-evm", distro="poky")

    cmd_a = ti_layertool._build_layertool_cmd(cfg_a)
    cmd_b = ti_layertool._build_layertool_cmd(cfg_b)

    assert cmd_a == cmd_b
    joined = " ".join(cmd_a)
    assert "am62x-var-som" not in joined
    assert "arago" not in joined


# ---------------------------------------------------------------------------
# _strip_dl_dir
# ---------------------------------------------------------------------------


def test_strip_dl_dir_removes_line_keeps_others(tmp_path: Path) -> None:
    """Removes a ``DL_DIR`` assignment line; keeps every other line verbatim."""
    local_conf = tmp_path / "local.conf"
    local_conf.write_text('MACHINE = "am62x-var-som"\nDL_DIR = "/tmp/yocto-downloads"\nDISTRO = "arago"\n')

    ti_layertool._strip_dl_dir(local_conf)

    text = local_conf.read_text()
    assert "DL_DIR" not in text
    assert 'MACHINE = "am62x-var-som"' in text
    assert 'DISTRO = "arago"' in text
    # Trailing newline preserved
    assert text.endswith("\n")


def test_strip_dl_dir_handles_indented_line(tmp_path: Path) -> None:
    """An indented ``DL_DIR`` line is matched and removed (lstrip semantics)."""
    local_conf = tmp_path / "local.conf"
    local_conf.write_text('MACHINE = "am62x-var-som"\n   DL_DIR = "/tmp/yocto-downloads"\n')

    ti_layertool._strip_dl_dir(local_conf)

    assert "DL_DIR" not in local_conf.read_text()


def test_strip_dl_dir_no_op_when_no_match(tmp_path: Path) -> None:
    """Without a ``DL_DIR`` line, file content is unchanged byte for byte."""
    local_conf = tmp_path / "local.conf"
    original = 'MACHINE = "am62x-var-som"\nDISTRO = "arago"\n'
    local_conf.write_text(original)

    ti_layertool._strip_dl_dir(local_conf)

    assert local_conf.read_text() == original


def test_strip_dl_dir_missing_file_is_noop(tmp_path: Path) -> None:
    """An absent ``local.conf`` is a no-op (no exception, no file created)."""
    missing = tmp_path / "absent.conf"

    ti_layertool._strip_dl_dir(missing)

    assert not missing.exists()


# ---------------------------------------------------------------------------
# _record_active_config and reset_sources
# ---------------------------------------------------------------------------


def test_record_active_config_writes_atomically(tmp_path: Path) -> None:
    """Writes ``ti/conf/active-config.txt`` with the manifest plus newline."""
    cfg = _ti_cfg(tmp_path)

    ti_layertool._record_active_config(cfg)

    tracked = tmp_path / "ti" / "conf" / "active-config.txt"
    assert tracked.is_file()
    assert tracked.read_text() == cfg.manifest + "\n"
    # The .tmp sibling must not survive the atomic replace.
    assert not (tracked.parent / "active-config.txt.tmp").exists()


def test_record_active_config_overwrites_previous(tmp_path: Path) -> None:
    """Re-recording with a new manifest replaces the previous content."""
    cfg_old = _ti_cfg(tmp_path, manifest="processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt")
    cfg_new = _ti_cfg(tmp_path, manifest="processor-sdk-scarthgap-non-chromium-11.00.09.04-config_var02.txt")

    ti_layertool._record_active_config(cfg_old)
    ti_layertool._record_active_config(cfg_new)

    tracked = tmp_path / "ti" / "conf" / "active-config.txt"
    assert tracked.read_text() == cfg_new.manifest + "\n"


def test_reset_sources_removes_sources_and_marker(tmp_path: Path) -> None:
    """Wipes ``ti/sources/`` and ``ti/conf/active-config.txt``; idempotent."""
    cfg = _ti_cfg(tmp_path)
    ti = tmp_path / "ti"
    sources = ti / "sources" / "oe-core"
    sources.mkdir(parents=True)
    (sources / "oe-init-build-env").write_text("# stub\n")
    conf_dir = ti / "conf"
    conf_dir.mkdir(parents=True)
    tracked = conf_dir / "active-config.txt"
    tracked.write_text(cfg.manifest + "\n")

    ti_layertool.reset_sources(cfg)

    assert not (ti / "sources").exists()
    assert not tracked.exists()


def test_reset_sources_noop_on_clean_workspace(tmp_path: Path) -> None:
    """Running on a workspace with neither sources nor tracked file is a no-op."""
    cfg = _ti_cfg(tmp_path)

    # Must not raise even though nothing exists.
    ti_layertool.reset_sources(cfg)

    assert not (tmp_path / "ti" / "sources").exists()


# ---------------------------------------------------------------------------
# ti_setup_env._set_or_replace
# ---------------------------------------------------------------------------


def test_set_or_replace_replaces_existing(tmp_path: Path) -> None:
    """A matching line is replaced in place; trailing newline is preserved."""
    text = 'DISTRO = "arago"\nMACHINE ?= "am335x-evm"\nIMAGE_FSTYPES = "tar.gz"\n'
    key_re = re.compile(r"^\s*MACHINE\s*\??=", re.MULTILINE)

    out = ti_setup_env._set_or_replace(text, "MACHINE", "am62x-var-som", key_re)

    assert 'MACHINE = "am62x-var-som"' in out
    assert 'MACHINE ?= "am335x-evm"' not in out
    # The non-target lines are kept exactly.
    assert 'DISTRO = "arago"' in out
    assert 'IMAGE_FSTYPES = "tar.gz"' in out
    assert out.endswith("\n")


def test_set_or_replace_appends_when_missing(tmp_path: Path) -> None:
    """Without an existing key, the new assignment is appended to the file."""
    text = 'DISTRO = "arago"\n'
    key_re = re.compile(r"^\s*MACHINE\s*\??=", re.MULTILINE)

    out = ti_setup_env._set_or_replace(text, "MACHINE", "am62x-var-som", key_re)

    lines = out.splitlines()
    assert lines[-1] == 'MACHINE = "am62x-var-som"'
    assert lines[0] == 'DISTRO = "arago"'


def test_set_or_replace_drops_duplicate_keys(tmp_path: Path) -> None:
    """If the same key appears twice, only the first is replaced; duplicates dropped."""
    text = 'MACHINE = "old1"\nDISTRO = "arago"\nMACHINE ?= "old2"\n'
    key_re = re.compile(r"^\s*MACHINE\s*\??=", re.MULTILINE)

    out = ti_setup_env._set_or_replace(text, "MACHINE", "am62x-var-som", key_re)

    # One MACHINE assignment remains, with the new value.
    machine_lines = [line for line in out.splitlines() if "MACHINE" in line]
    assert machine_lines == ['MACHINE = "am62x-var-som"']


def test_set_or_replace_preserves_no_trailing_newline(tmp_path: Path) -> None:
    """If input lacks a trailing newline, the output also lacks one."""
    text = 'DISTRO = "arago"'  # no trailing newline
    key_re = re.compile(r"^\s*MACHINE\s*\??=", re.MULTILINE)

    out = ti_setup_env._set_or_replace(text, "MACHINE", "am62x-var-som", key_re)

    assert not out.endswith("\n")


# ---------------------------------------------------------------------------
# ti_setup_env.run (no subprocess - pure filesystem)
# ---------------------------------------------------------------------------


def _seed_ti_workspace(
    tmp_path: Path,
    *,
    local_conf_body: str | None = None,
    write_bblayers: bool = True,
) -> BuildConfig:
    """Materialize the ti/build/conf/ tree expected by ``ti_setup_env.run``."""
    cfg = _ti_cfg(tmp_path)
    conf = tmp_path / "ti" / "build" / "conf"
    conf.mkdir(parents=True)
    if local_conf_body is None:
        local_conf_body = 'MACHINE ?= "am335x-evm"\nDISTRO ?= "poky"\nDL_DIR = "/tmp/old-downloads"\n'
    (conf / "local.conf").write_text(local_conf_body)
    if write_bblayers:
        (conf / "bblayers.conf").write_text("# stub\n")
    return cfg


def test_run_rewrites_local_conf(tmp_path: Path) -> None:
    """``run`` overrides MACHINE/DISTRO and strips DL_DIR in local.conf."""
    cfg = _seed_ti_workspace(tmp_path)

    ti_setup_env.run(cfg, MagicMock())

    text = (cfg.bsp_root / "build" / "conf" / "local.conf").read_text()
    assert 'MACHINE = "am62x-var-som"' in text
    assert 'DISTRO = "arago"' in text
    assert "DL_DIR" not in text


def test_run_appends_missing_machine_distro(tmp_path: Path) -> None:
    """When local.conf has no MACHINE/DISTRO lines, they are appended."""
    cfg = _seed_ti_workspace(tmp_path, local_conf_body="# empty\n")

    ti_setup_env.run(cfg, MagicMock())

    text = (cfg.bsp_root / "build" / "conf" / "local.conf").read_text()
    assert 'MACHINE = "am62x-var-som"' in text
    assert 'DISTRO = "arago"' in text


def test_run_raises_when_local_conf_missing(tmp_path: Path) -> None:
    """Absent ``local.conf`` raises ``FileNotFoundError`` before any rewrite."""
    cfg = _ti_cfg(tmp_path)
    # Do NOT seed the workspace.

    with pytest.raises(FileNotFoundError, match=r"local\.conf"):
        ti_setup_env.run(cfg, MagicMock())


def test_run_raises_when_bblayers_missing(tmp_path: Path) -> None:
    """A successful local.conf rewrite still raises if bblayers.conf is absent."""
    cfg = _seed_ti_workspace(tmp_path, write_bblayers=False)

    with pytest.raises(RuntimeError, match=r"bblayers\.conf"):
        ti_setup_env.run(cfg, MagicMock())

    # local.conf rewrite still happened before the bblayers check raised.
    text = (cfg.bsp_root / "build" / "conf" / "local.conf").read_text()
    assert 'MACHINE = "am62x-var-som"' in text


def test_run_logs_step_start_and_ok(tmp_path: Path) -> None:
    """Happy path emits step_start then step_ok on the RunLogger."""
    cfg = _seed_ti_workspace(tmp_path)
    log = MagicMock()

    ti_setup_env.run(cfg, log)

    log.step_start.assert_called_once()
    log.step_ok.assert_called_once()
