"""Tests for ``bakar init --explain``.

Reuses the host-faking ``env`` fixture of the non-interactive feature tests, so
no test starts ssh, systemctl, sudo or psql.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from bakar import diagnostics, user_config
from bakar.commands import init as init_module  # noqa: F401 - registers the command
from bakar.commands._app import app
from bakar.setup.features import FEATURE_IDS
from tests.test_cli_init_features import env  # noqa: F401 - fixture

if TYPE_CHECKING:
    from types import SimpleNamespace

pytestmark = pytest.mark.unit

cli = CliRunner()


def _explain(*args: str):
    return cli.invoke(app, ["init", *args])


def _user_config_path() -> Path:
    return Path.home() / ".config" / "bakar" / "config.toml"


def test_unknown_feature_lists_all_ids(env: SimpleNamespace) -> None:  # noqa: F811
    result = _explain("--explain", "nonsense")
    assert result.exit_code == 1
    assert len(FEATURE_IDS) == 15
    for feature_id in FEATURE_IDS:
        assert feature_id in result.output


def test_uninative_missing_fragment_is_unmet_with_fix(env: SimpleNamespace, monkeypatch) -> None:  # noqa: F811
    fragment = "/usr/share/yocto-uninative/uninative.inc"
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self, **kw: False if str(self) == fragment else real_exists(self, **kw))
    result = _explain("--explain", "uninative", "-w", str(env.ws))
    assert result.exit_code == 0, result.output
    assert f"unmet: {fragment}" in result.output
    assert "fix: install the yocto-uninative-tarball package" in result.output
    assert "uninative" in result.output.lower()


def test_psi_with_support_passing_is_met(env: SimpleNamespace) -> None:  # noqa: F811
    result = _explain("--explain", "psi", "-w", str(env.ws))
    assert result.exit_code == 0, result.output
    assert "met: psi-kernel" in result.output
    assert "unmet" not in result.output


def test_sstate_mirror_without_url_is_not_checked(env: SimpleNamespace) -> None:  # noqa: F811
    result = _explain("--explain", "sstate-mirror", "-w", str(env.ws))
    assert result.exit_code == 0, result.output
    assert "not checked: sstate-mirror-reachable" in result.output
    assert "build.sstate_mirror_url" in result.output
    assert "unmet: sstate-mirror-reachable" not in result.output
    assert "met: sstate-mirror-reachable" not in result.output.replace("not checked", "")


def test_sstate_mirror_unreachable_url_is_unmet(env: SimpleNamespace) -> None:  # noqa: F811
    user_config.set_setting("build.sstate_mirror_url", "http://nobody.invalid/sstate")
    env.overrides["sstate-mirror-reachable"] = diagnostics.CheckResult(
        "sstate-mirror-reachable",
        diagnostics.Severity.WARN,
        diagnostics.Status.FAIL,
        "http://nobody.invalid/sstate did not answer",
        "docs/features/sstate-mirror.md",
    )
    result = _explain("--explain", "sstate-mirror", "-w", str(env.ws))
    assert result.exit_code == 0, result.output
    assert "unmet: sstate-mirror-reachable" in result.output
    assert "http://nobody.invalid/sstate" in result.output


def test_explain_with_other_options_exits_1(env: SimpleNamespace) -> None:  # noqa: F811
    assert _explain("--explain", "ccache", "--feature", "ccache").exit_code == 1
    assert _explain("--explain", "ccache", "--family", "generic").exit_code == 1
    assert _explain("--explain", "ccache", "--disable", "ccache").exit_code == 1


def test_explain_writes_nothing_on_clean_home(env: SimpleNamespace) -> None:  # noqa: F811
    assert not _user_config_path().exists()
    for feature_id in ("ccache", "psi", "sstate-mirror", "nonsense"):
        _explain("--explain", feature_id, "-w", str(env.ws))
    assert not _user_config_path().exists()
    assert not (env.ws / ".bakar.toml").exists()
    assert not env.apply_calls
