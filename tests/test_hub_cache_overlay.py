"""Hub download-mirror overlay: content, ordering, packaging and gated selection."""

from __future__ import annotations

import importlib.resources
import types
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
import yaml

from bakar.cli import app
from bakar.commands import _overlays
from bakar.commands._overlays import _tuning_extra_overlays
from bakar.config import BuildConfig
from bakar.user_config import UserConfig

if TYPE_CHECKING:
    from typer.testing import CliRunner

pytestmark = pytest.mark.unit

_OVERLAY_DIR = Path(str(importlib.resources.files("bakar") / "overlays"))
_HUB = _OVERLAY_DIR / "bakar-tuning-hub-cache.yml"
_HUB_URL = "http://hub.test:8080"
_SCHEMES = ("https?", "ftp", "crate", "gomod")
_EXCLUDED_SCHEMES = ("git", "gitsm")


def _load(name: str) -> dict:
    return yaml.safe_load((_OVERLAY_DIR / name).read_text())


def _cfg(url: str | None) -> MagicMock:
    cfg = MagicMock(spec=BuildConfig)
    cfg.dl_mirror_url = url
    cfg.use_hub_mirror = bool(url)
    return cfg


@pytest.fixture(autouse=True)
def _clear_memo() -> None:
    _overlays._HUB_PROBE_MEMO.clear()


def test_overlay_holds_one_premirrors_prepend() -> None:
    data = _load("bakar-tuning-hub-cache.yml")
    assert list(data["env"]) == ["BAKAR_DL_MIRROR_URL"]
    blocks = data["local_conf_header"]
    assert list(blocks) == ["zz-bakar-45-hub-cache"]
    body = blocks["zz-bakar-45-hub-cache"]
    lines = [line for line in body.splitlines() if line.startswith("PREMIRRORS:prepend")]
    assert len(lines) == 1
    assert "BAKAR_DL_MIRROR_URL" in lines[0]
    for scheme in _SCHEMES:
        assert scheme in lines[0]
    for scheme in _EXCLUDED_SCHEMES:
        assert scheme not in lines[0]
    assert "SSTATE_MIRRORS" not in body
    assert "BB_HASHSERVE_UPSTREAM" not in body


def test_block_key_sorts_after_generic_and_shared_cache() -> None:
    key = "zz-bakar-45-hub-cache"
    generic = _load("bakar-tuning-generic.yml")["local_conf_header"]
    assert all(key > other for other in generic)
    assert key > "zz-bakar-40-shared-cache"


def test_overlay_ships_as_package_data() -> None:
    assert importlib.resources.files("bakar").joinpath("overlays/bakar-tuning-hub-cache.yml").is_file()


def test_reachable_hub_includes_overlay() -> None:
    with patch("bakar.probes.http_reachable", return_value=True) as probe:
        assert _HUB in _tuning_extra_overlays(_cfg(_HUB_URL))
    probe.assert_called_once_with(_HUB_URL, timeout=3.0)


def test_unreachable_hub_excludes_overlay_and_warns_once(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("bakar.probes.http_reachable", return_value=False):
        assert _HUB not in _tuning_extra_overlays(_cfg(_HUB_URL))
    err = capsys.readouterr().err
    warnings = [line for line in err.splitlines() if _HUB_URL in line]
    assert len(warnings) == 1
    assert "without the hub mirror" in warnings[0]


def test_unset_url_excludes_overlay_without_probing() -> None:
    with patch("bakar.probes.http_reachable") as probe:
        assert _HUB not in _tuning_extra_overlays(_cfg(None))
    probe.assert_not_called()


def test_repeated_resolution_probes_once() -> None:
    with patch("bakar.probes.http_reachable", return_value=False) as probe:
        _tuning_extra_overlays(_cfg(_HUB_URL))
        _tuning_extra_overlays(_cfg(_HUB_URL))
    assert probe.call_count == 1


def test_dry_run_prints_the_decision_and_exits_zero(runner: CliRunner, tmp_path: Path) -> None:
    setup_dir = tmp_path / "ws"
    setup_dir.mkdir()
    cfg = MagicMock()
    cfg.kas_yaml = setup_dir / "kas.yml"
    cfg.bsp_root = setup_dir
    cfg.runs_dir = setup_dir / "build" / "runs"
    cfg.resolved_tmpdir = setup_dir / "build" / "tmp"
    cfg.sstate_mirror_url = None
    cfg.dl_mirror_url = _HUB_URL
    cfg.use_hub_mirror = True
    run_log = MagicMock()
    run_log.return_value.__enter__.return_value.run_id = "20260603-000000"
    run_log.return_value.__exit__.return_value = False
    with (
        patch("bakar.probes.http_reachable", return_value=False),
        patch("bakar.commands._app._load_user_config_safe", return_value=UserConfig()),
        patch("bakar.commands._app._get_vendors", return_value=[]),
        patch("bakar.commands.build._bbsetup_workspace", return_value=setup_dir),
        patch("bakar.config.resolve", return_value=cfg),
        patch("bakar.commands._build_flavors.translate_bbsetup_config", return_value={"machine": "qemux86-64"}),
        patch("bakar.commands._build_flavors.write_bbsetup_yaml"),
        patch("bakar.commands._build_flavors._run_doctor_gate"),
        patch("bakar.commands._build_common.RunLogger", run_log),
        patch("bakar.commands.build.step_kas.dry_run_preview_lines", return_value=[]),
    ):
        result = runner.invoke(app, ["build", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert sum(_HUB_URL in line for line in result.output.splitlines()) == 1


def test_env_url_wins_over_config_for_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_DL_MIRROR_URL", "http://env.test:9")
    with patch("bakar.probes.http_reachable", return_value=True) as probe:
        assert _HUB in _tuning_extra_overlays(_cfg(_HUB_URL))
    probe.assert_called_once_with("http://env.test:9", timeout=3.0)


def test_env_url_does_not_probe_when_config_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_DL_MIRROR_URL", "http://env.test:9")
    with patch("bakar.probes.http_reachable") as probe:
        assert _HUB not in _tuning_extra_overlays(_cfg(None))
    probe.assert_not_called()


def _premirrors_expr() -> str:
    body = _load("bakar-tuning-hub-cache.yml")["local_conf_header"]["zz-bakar-45-hub-cache"]
    line = next(ln for ln in body.splitlines() if ln.startswith("PREMIRRORS:prepend"))
    value = line.split("=", 1)[1].strip()
    assert value.startswith('"${@')
    assert value.endswith('}"')
    return value[len('"${@') : -len('}"')]


def _eval_premirrors(environ: dict[str, str]) -> str:
    stub_os = types.SimpleNamespace(environ=environ)
    expr = _premirrors_expr()  # the shipped overlay's own inline-python text, run against a stub os
    # nosemgrep: python.lang.security.audit.eval-detected.eval-detected
    return eval(expr, {"os": stub_os})


@pytest.mark.parametrize("environ", [{}, {"BAKAR_DL_MIRROR_URL": ""}])
def test_empty_or_absent_url_yields_no_premirror(environ: dict[str, str]) -> None:
    assert _eval_premirrors(environ) == ""


def test_set_url_yields_checksummed_schemes_and_the_url() -> None:
    value = _eval_premirrors({"BAKAR_DL_MIRROR_URL": _HUB_URL})
    assert value.startswith("(https?|ftp|crate|gomod)://.*/.* " + _HUB_URL + "/ ")
    assert value.endswith("\\n ")


def test_unreachable_warning_hides_userinfo_and_keeps_ipv6_literal(capsys: pytest.CaptureFixture[str]) -> None:
    url = "http://user:s3cret@[fd00::1]:8080/dl"
    with patch("bakar.probes.http_reachable", return_value=False):
        _tuning_extra_overlays(_cfg(url))
    err = capsys.readouterr().err
    warnings = [line for line in err.splitlines() if "hub mirror" in line]
    assert len(warnings) == 1
    assert "s3cret" not in err
    assert "user" not in err
    assert "http://[fd00::1]:8080/dl" in warnings[0]
