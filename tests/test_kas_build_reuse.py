"""Tests for the meta-avocado wrapper placement and the repository reuse policy.

Covers ``_write_meta_avocado_wrapper`` choosing an absolute ``path:`` for a
checkout outside ``cfg.workspace/meta-avocado``, and ``_build_kas_arg`` /
``_run_kas_dump`` under ``BAKAR_REPO_POLICY``: the reuse overlay is merged last
and the ``--skip repos_checkout`` retry is reserved for the ``kas`` policy.
Every kas and git call is mocked.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from bakar.bsp_detect import LayerRepo
from bakar.config import BuildConfig, _overlay_dir
from bakar.release_workspace import ReleaseRoutingError
from bakar.steps import kas_build, kas_graph_capture
from bakar.steps.kas_build import _build_kas_arg, _run_kas_dump, _write_meta_avocado_wrapper
from bakar.steps.repo_reuse import RepoFacts, RepoState

DUMP_YAML = "header:\n  version: 16\nrepos:\n  meta-foo:\n    url: https://example.invalid/foo.git\n    commit: abc\n"
PIN = "0123456789abcdef0123456789abcdef01234567"


def _cfg(workspace: Path, kas_yaml: Path) -> BuildConfig:
    cfg = BuildConfig(
        workspace=workspace,
        bsp_family="generic",  # type: ignore[arg-type]
        machine="generic",
        distro="generic",
        image="generic",
        manifest="",
        repo_url="",
        repo_branch="",
        kas_container_image="jetm/kas-build-env:latest",
        kas_yaml_override=kas_yaml,
    )
    cfg.bsp_root.mkdir(parents=True, exist_ok=True)
    return cfg


def _kas_file(checkout: Path) -> Path:
    kas_yaml = checkout / "kas" / "machine" / "m.yml"
    kas_yaml.parent.mkdir(parents=True, exist_ok=True)
    kas_yaml.write_text("header:\n  version: 16\n", encoding="utf-8")
    return kas_yaml


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["kas", "dump"], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def no_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BAKAR_REPO_POLICY", raising=False)


# ---------------------------------------------------------------------------
# _write_meta_avocado_wrapper
# ---------------------------------------------------------------------------


def test_wrapper_is_absolute_for_checkout_outside_workspace(tmp_path: Path) -> None:
    checkout = tmp_path / "release" / "meta-avocado"
    kas_yaml = _kas_file(checkout)
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = _cfg(ws, kas_yaml)

    wrapper = _write_meta_avocado_wrapper(cfg, kas_yaml)

    data = yaml.safe_load(wrapper.read_text(encoding="utf-8"))
    assert data["repos"] == {"meta-avocado": {"path": str(checkout.resolve())}}
    assert data["header"]["includes"] == [{"repo": "meta-avocado", "file": "kas/machine/m.yml"}]


def test_wrapper_is_relative_byte_for_byte_inside_workspace(tmp_path: Path) -> None:
    kas_yaml = _kas_file(tmp_path / "meta-avocado")
    cfg = _cfg(tmp_path, kas_yaml)

    wrapper = _write_meta_avocado_wrapper(cfg, kas_yaml)

    assert wrapper.read_text(encoding="utf-8") == (
        "header:\n"
        "  version: 16\n"
        "  includes:\n"
        "    - repo: meta-avocado\n"
        "      file: kas/machine/m.yml\n"
        "repos:\n"
        "  meta-avocado:\n"
        "    path: meta-avocado\n"
    )


def test_wrapper_is_relative_when_workspace_is_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    kas_yaml = _kas_file(real / "meta-avocado")
    link = tmp_path / "link"
    link.symlink_to(real)
    cfg = _cfg(link, link / "meta-avocado" / "kas" / "machine" / "m.yml")

    wrapper = _write_meta_avocado_wrapper(cfg, kas_yaml)

    assert yaml.safe_load(wrapper.read_text(encoding="utf-8"))["repos"]["meta-avocado"]["path"] == "meta-avocado"


# ---------------------------------------------------------------------------
# _build_kas_arg under BAKAR_REPO_POLICY
# ---------------------------------------------------------------------------


def _meta_avocado_setup(tmp_path: Path) -> tuple[BuildConfig, Path, Path]:
    kas_yaml = _kas_file(tmp_path / "meta-avocado")
    cfg = _cfg(tmp_path, kas_yaml)
    overlay = tmp_path / "overlay.yml"
    overlay.write_text("header:\n  version: 16\n", encoding="utf-8")
    return cfg, kas_yaml, overlay


def test_reuse_policy_merges_reuse_overlay_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, kas_yaml, overlay = _meta_avocado_setup(tmp_path)
    side = tmp_path / ".bakar" / "pins" / "meta-foo" / PIN[:12]
    states = [
        RepoState("poky", tmp_path / "poky", PIN, "frozen"),
        RepoState("meta-foo", tmp_path / "meta-foo", PIN, "side", side, False),
        RepoState("meta-bar", tmp_path / "meta-bar", PIN, "side", tmp_path / "bar-side", True),
    ]
    merged = {"repos": {"poky": {"url": "u", "commit": PIN}}}

    with (
        patch.object(kas_build.kas_pins, "read_merged_config", return_value=merged) as read,
        patch.object(kas_build.repo_reuse, "classify", return_value=states) as classify,
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)) as run,
    ):
        _build_kas_arg(cfg, kas_yaml, overlay)

    assert read.call_args.kwargs["checkout"].resolve() == (tmp_path / "meta-avocado").resolve()
    # The pins come from the same stack the dump merges, minus the reuse overlay.
    assert read.call_args.kwargs["extra_files"] == [cfg.bsp_root / ".bakar" / "overlays" / overlay.name]
    # Beside the build's own wrapper, so the overlays' includes resolve alike.
    assert read.call_args.kwargs["wrap_parent"] == cfg.bsp_root
    classify.assert_called_once_with(merged["repos"], cfg.workspace, pair_core_with_bitbake=cfg.host_mode)
    argv = run.call_args.args[0]
    assert argv[:2] == ["kas", "dump"]
    assert "--skip" not in argv
    assert argv[-1].split(":")[-1] == "avocado-reuse.yml"
    overlay_data = yaml.safe_load((cfg.bsp_root / "avocado-reuse.yml").read_text(encoding="utf-8"))
    # Workspace-relative, so kas-container finds them under /work as well.
    assert overlay_data["repos"]["poky"] == {"path": "poky", "url": None}
    assert overlay_data["repos"]["meta-foo"] == {"path": f".bakar/pins/meta-foo/{PIN[:12]}"}
    assert overlay_data["repos"]["meta-bar"] == {"path": "bar-side", "url": None}
    err = capsys.readouterr().err
    assert f"side checkout meta-foo at {PIN[:12]}: {side}" in err
    assert f"side checkout meta-bar at {PIN[:12]}: {tmp_path / 'bar-side'} (reused)" in err


def test_reuse_errors_fail_the_build_with_their_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, kas_yaml, overlay = _meta_avocado_setup(tmp_path)
    exc = ReleaseRoutingError("cannot read pins")

    with (
        patch.object(kas_build.kas_pins, "read_merged_config", side_effect=exc),
        patch("bakar.steps.kas_build.subprocess.run") as run,
        pytest.raises(RuntimeError, match=str(exc)),
    ):
        _build_kas_arg(cfg, kas_yaml, overlay)

    run.assert_not_called()


REASON = "off its pin or has local changes"


@pytest.mark.parametrize("name", ["bitbake", "openembedded-core"])
def test_side_checkout_of_bitbake_or_oe_core_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, kas_yaml, overlay = _meta_avocado_setup(tmp_path)
    merged = {
        "repos": {
            "meta-foo": {"url": "https://example.invalid/foo.git", "commit": PIN},
            name: {"url": f"https://example.invalid/{name}.git", "commit": PIN},
        }
    }

    def facts_for(entries: list) -> list[RepoFacts]:
        # Both clones sit on another commit; meta-foo is dirty so it takes a side checkout.
        return [RepoFacts(True, OTHER_HEAD, e.name != "meta-foo", True, False, None, None) for e in entries]

    with (
        patch.object(kas_build.kas_pins, "read_merged_config", return_value=merged),
        patch.object(kas_build.repo_reuse, "gather_local_facts", side_effect=facts_for),
        patch("bakar.steps.kas_build.subprocess.run") as run,
        pytest.raises(RuntimeError) as exc,
    ):
        _build_kas_arg(cfg, kas_yaml, overlay)

    msg = str(exc.value)
    assert name in msg
    assert PIN[:12] in msg
    assert REASON in msg
    assert f"bakar sync {kas_yaml}" in msg
    run.assert_not_called()
    assert not (cfg.bsp_root / "avocado-reuse.yml").exists()


def test_reuse_refusal_names_all_blockers_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, kas_yaml, overlay = _meta_avocado_setup(tmp_path)
    names = ("bitbake", "openembedded-core", "meta-dirty", "meta-ok")
    merged = {"repos": {n: {"url": f"https://example.invalid/{n}.git", "commit": PIN} for n in names}}

    def facts_for(entries: list) -> list[RepoFacts]:
        by_name = {
            # clean but off its pin: bakar sync can move it
            "bitbake": RepoFacts(True, OTHER_HEAD, True, True, False, None, None),
            # dirty, and it may not take a side checkout: only a hand edit helps
            "openembedded-core": RepoFacts(True, OTHER_HEAD, False, True, False, None, None),
            # clean shared clone but a dirty side checkout: moving the clone clears it
            "meta-dirty": RepoFacts(True, OTHER_HEAD, True, True, True, OTHER_HEAD, False),
            # dirty shared clone: takes a side checkout, blocks nothing
            "meta-ok": RepoFacts(True, OTHER_HEAD, False, True, False, None, None),
        }
        return [by_name[e.name] for e in entries]

    with (
        patch.object(kas_build.kas_pins, "read_merged_config", return_value=merged),
        patch.object(kas_build.repo_reuse, "gather_local_facts", side_effect=facts_for),
        patch("bakar.steps.kas_build.subprocess.run") as run,
        pytest.raises(RuntimeError) as exc,
    ):
        _build_kas_arg(cfg, kas_yaml, overlay)

    msg = str(exc.value)
    assert msg.startswith("3 repositories block this build:")
    for name in ("bitbake", "openembedded-core", "meta-dirty"):
        assert f"  {name} ({tmp_path / name}): " in msg
    assert "  meta-ok " not in msg
    assert msg.count(f"bakar sync {kas_yaml}") == 1
    assert "fix them by hand: openembedded-core" in msg
    assert "Using side checkouts: meta-ok" in msg
    run.assert_not_called()


def test_repair_line_is_quoted_and_names_the_build_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    # The kas file sits in a release checkout routed elsewhere; -w names another workspace.
    kas_yaml = _kas_file(tmp_path / "release; x" / "meta-avocado")
    ws = tmp_path / "other ws"
    ws.mkdir()
    cfg = _cfg(ws, kas_yaml)
    overlay = tmp_path / "overlay.yml"
    overlay.write_text("header:\n  version: 16\n", encoding="utf-8")
    merged = {"repos": {"bitbake": {"url": "https://example.invalid/bitbake.git", "commit": PIN}}}

    def facts_for(entries: list) -> list[RepoFacts]:
        return [RepoFacts(True, OTHER_HEAD, True, True, False, None, None) for _ in entries]

    with (
        patch.object(kas_build.kas_pins, "read_merged_config", return_value=merged),
        patch.object(kas_build.repo_reuse, "gather_local_facts", side_effect=facts_for),
        patch("bakar.steps.kas_build.subprocess.run") as run,
        pytest.raises(RuntimeError) as exc,
    ):
        _build_kas_arg(cfg, kas_yaml, overlay)

    msg = str(exc.value)
    assert f"with: bakar sync '{kas_yaml}' -w '{ws}'" in msg
    run.assert_not_called()


def test_reuse_only_side_checkouts_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, kas_yaml, overlay = _meta_avocado_setup(tmp_path)
    side = tmp_path / ".bakar" / "pins" / "meta-foo" / PIN[:12]
    states = [RepoState("meta-foo", tmp_path / "meta-foo", PIN, "side", side, False)]

    with (
        patch.object(kas_build.kas_pins, "read_merged_config", return_value={"repos": {}}),
        patch.object(kas_build.repo_reuse, "classify", return_value=states),
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)) as run,
    ):
        _build_kas_arg(cfg, kas_yaml, overlay)

    run.assert_called_once()
    assert f"side checkout meta-foo at {PIN[:12]}: {side}\n" in capsys.readouterr().err


def test_side_checkout_line_replaces_control_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, kas_yaml, overlay = _meta_avocado_setup(tmp_path)
    evil = "meta-\x1b[2Kfoo"
    states = [RepoState(evil, tmp_path / "meta-foo", PIN, "side", tmp_path / "side\x1bx", False)]

    with (
        patch.object(kas_build.kas_pins, "read_merged_config", return_value={"repos": {}}),
        patch.object(kas_build.repo_reuse, "classify", return_value=states),
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)),
    ):
        _build_kas_arg(cfg, kas_yaml, overlay)

    err = capsys.readouterr().err
    assert "\x1b" not in err
    assert f"side checkout meta-?[2Kfoo at {PIN[:12]}: {tmp_path}/side?x\n" in err


def test_frozen_bitbake_is_not_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, kas_yaml, overlay = _meta_avocado_setup(tmp_path)
    states = [RepoState("bitbake", tmp_path / "bitbake", PIN, "frozen")]

    with (
        patch.object(kas_build.kas_pins, "read_merged_config", return_value={"repos": {}}),
        patch.object(kas_build.repo_reuse, "classify", return_value=states),
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)) as run,
    ):
        _build_kas_arg(cfg, kas_yaml, overlay)

    run.assert_called_once()


def test_reuse_without_meta_avocado_checkout_falls_back_to_kas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sibling-repo entry has no meta-avocado checkout: one skip line, then the kas policy."""
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    (tmp_path / "meta-avocado").mkdir()
    entry = tmp_path / "meta-avocado-cve" / "cve.yml"
    entry.parent.mkdir()
    entry.write_text("header:\n  version: 16\n", encoding="utf-8")
    cfg = _cfg(tmp_path, entry)
    overlay = tmp_path / "overlay.yml"
    overlay.write_text("header:\n  version: 16\n", encoding="utf-8")

    with (
        patch.object(kas_build.kas_pins, "read_merged_config") as read,
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)) as run,
    ):
        _build_kas_arg(cfg, entry, overlay)

    read.assert_not_called()
    assert "avocado-reuse.yml" not in run.call_args.args[0][-1]
    assert not (cfg.bsp_root / "avocado-reuse.yml").exists()
    assert capsys.readouterr().err.count("reuse skipped") == 1


def test_default_policy_adds_no_reuse_overlay(tmp_path: Path, no_policy: None) -> None:
    cfg, kas_yaml, overlay = _meta_avocado_setup(tmp_path)

    with (
        patch.object(kas_build.kas_pins, "read_merged_config") as read,
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)) as run,
    ):
        _build_kas_arg(cfg, kas_yaml, overlay)

    read.assert_not_called()
    assert "avocado-reuse.yml" not in run.call_args.args[0][-1]
    assert not (cfg.bsp_root / "avocado-reuse.yml").exists()


# ---------------------------------------------------------------------------
# _run_kas_dump retry
# ---------------------------------------------------------------------------

_REBASED = "error: origin/scarthgap does not contain commit deadbeef"


def _dump_inputs(tmp_path: Path) -> tuple[BuildConfig, Path]:
    cfg, _kas_yaml, _overlay = _meta_avocado_setup(tmp_path)
    wrapper = cfg.bsp_root / "avocado-wrapper.yml"
    wrapper.write_text("header:\n  version: 16\n", encoding="utf-8")
    return cfg, wrapper


def test_reuse_policy_does_not_retry_with_skip(tmp_path: Path) -> None:
    cfg, wrapper = _dump_inputs(tmp_path)

    with (
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(1, stderr=_REBASED)) as run,
        pytest.raises(RuntimeError, match="does not contain commit"),
    ):
        _run_kas_dump(cfg, wrapper, Path("o.yml"), [Path("avocado-reuse.yml")])

    assert run.call_count == 1


def test_kas_policy_still_retries_with_skip(tmp_path: Path) -> None:
    cfg, wrapper = _dump_inputs(tmp_path)
    responses = [_completed(1, stderr=_REBASED), _completed(0, stdout=DUMP_YAML)]

    with patch("bakar.steps.kas_build.subprocess.run", side_effect=responses) as run:
        _run_kas_dump(cfg, wrapper, Path("o.yml"))

    assert run.call_count == 2
    assert run.call_args_list[1].args[0][:4] == ["kas", "dump", "--skip", "repos_checkout"]


# ---------------------------------------------------------------------------
# meta-avocado pin merged after every overlay
# ---------------------------------------------------------------------------


def _outside_setup(tmp_path: Path) -> tuple[BuildConfig, Path, Path, Path]:
    checkout = tmp_path / "release" / "meta-avocado"
    kas_yaml = _kas_file(checkout)
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = _cfg(ws, kas_yaml)
    overlay = tmp_path / "overlay.yml"
    overlay.write_text("header:\n  version: 16\n", encoding="utf-8")
    return cfg, kas_yaml, overlay, checkout


def test_avocado_pin_checkout_outside_workspace_is_pinned(tmp_path: Path) -> None:
    cfg, kas_yaml, _overlay, checkout = _outside_setup(tmp_path)

    pinned = kas_build._pinned_checkout(cfg, kas_yaml)

    assert pinned is not None
    assert pinned.resolve() == checkout.resolve()


def test_avocado_pin_not_needed_for_workspace_clone(tmp_path: Path) -> None:
    kas_yaml = _kas_file(tmp_path / "meta-avocado")
    cfg = _cfg(tmp_path, kas_yaml)

    assert kas_build._pinned_checkout(cfg, kas_yaml) is None


def test_avocado_pin_not_needed_when_workspace_is_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    kas_yaml = _kas_file(real / "meta-avocado")
    link = tmp_path / "link"
    link.symlink_to(real)
    cfg = _cfg(link, link / "meta-avocado" / "kas" / "machine" / "m.yml")

    assert kas_build._pinned_checkout(cfg, kas_yaml) is None


def test_avocado_pin_written_for_checkout_outside_workspace(tmp_path: Path) -> None:
    cfg, kas_yaml, _overlay, checkout = _outside_setup(tmp_path)

    rel = kas_build._write_meta_avocado_pin(cfg, kas_yaml)

    assert rel == Path("avocado-pin.yml")
    data = yaml.safe_load((cfg.bsp_root / "avocado-pin.yml").read_text(encoding="utf-8"))
    assert data == {"header": {"version": 16}, "repos": {"meta-avocado": {"path": str(checkout.resolve())}}}


def test_avocado_pin_not_written_for_workspace_clone(tmp_path: Path) -> None:
    kas_yaml = _kas_file(tmp_path / "meta-avocado")
    cfg = _cfg(tmp_path, kas_yaml)

    assert kas_build._write_meta_avocado_pin(cfg, kas_yaml) is None
    assert not (cfg.bsp_root / "avocado-pin.yml").exists()


def test_avocado_pin_merged_last_under_kas_policy(tmp_path: Path, no_policy: None) -> None:
    cfg, kas_yaml, overlay, _checkout = _outside_setup(tmp_path)

    with patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)) as run:
        _build_kas_arg(cfg, kas_yaml, overlay)

    argv = run.call_args.args[0]
    assert argv[:2] == ["kas", "dump"]
    assert argv[-1].split(":")[-1] == "avocado-pin.yml"
    assert run.call_args.kwargs["cwd"] == str(cfg.bsp_root)
    assert (cfg.bsp_root / "avocado-wrapper.yml").is_file()
    assert (cfg.bsp_root / "avocado-pin.yml").is_file()


def test_avocado_pin_follows_reuse_overlay_under_reuse_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, kas_yaml, overlay, _checkout = _outside_setup(tmp_path)
    states = [RepoState("poky", cfg.workspace / "poky", PIN, "frozen")]

    with (
        patch.object(kas_build.kas_pins, "read_merged_config", return_value={"repos": {}}) as read,
        patch.object(kas_build.repo_reuse, "classify", return_value=states),
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)) as run,
    ):
        _build_kas_arg(cfg, kas_yaml, overlay)

    files = run.call_args.args[0][-1].split(":")
    assert files[-2:] == ["avocado-reuse.yml", "avocado-pin.yml"]
    assert all(p.name != "avocado-pin.yml" for p in read.call_args.kwargs["extra_files"])


def test_avocado_pin_absent_for_workspace_clone_under_either_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, kas_yaml, overlay = _meta_avocado_setup(tmp_path)
    states = [RepoState("poky", tmp_path / "poky", PIN, "frozen")]

    monkeypatch.delenv("BAKAR_REPO_POLICY", raising=False)
    with patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)) as run:
        _build_kas_arg(cfg, kas_yaml, overlay)
    assert run.call_args.args[0][-1] == f"avocado-wrapper.yml:.bakar/overlays/{overlay.name}"

    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    with (
        patch.object(kas_build.kas_pins, "read_merged_config", return_value={"repos": {}}),
        patch.object(kas_build.repo_reuse, "classify", return_value=states),
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)) as run,
    ):
        _build_kas_arg(cfg, kas_yaml, overlay)
    assert run.call_args.args[0][-1].split(":")[-1] == "avocado-reuse.yml"
    assert not (cfg.bsp_root / "avocado-pin.yml").exists()


def test_avocado_pin_does_not_unlock_skip_retry_under_reuse(tmp_path: Path) -> None:
    cfg, wrapper = _dump_inputs(tmp_path)

    with (
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(1, stderr=_REBASED)) as run,
        pytest.raises(RuntimeError, match="does not contain commit"),
    ):
        _run_kas_dump(cfg, wrapper, Path("o.yml"), [Path("avocado-reuse.yml"), Path("avocado-pin.yml")])

    assert run.call_count == 1
    assert "--skip" not in run.call_args.args[0]


# ---------------------------------------------------------------------------
# openembedded-core side checkout paired with the workspace's bitbake
# ---------------------------------------------------------------------------

BB_PIN = "fedcba9876543210fedcba9876543210fedcba98"
OTHER_HEAD = "1111111111111111111111111111111111111111"
PAIR_MERGED = {
    "repos": {
        "bitbake": {"url": "https://example.invalid/bb.git", "commit": BB_PIN},
        "openembedded-core": {"url": "https://example.invalid/oe.git", "commit": PIN},
    }
}


def _pair_facts(entries: list) -> list[RepoFacts]:
    facts = []
    for entry in entries:
        if entry.name == "bitbake":
            facts.append(RepoFacts(True, BB_PIN, True, True, False, None, None))
        else:
            facts.append(RepoFacts(True, OTHER_HEAD, False, True, False, None, None))
    return facts


def _build_pair(cfg: BuildConfig, kas_yaml: Path, overlay: Path) -> None:
    with (
        patch.object(kas_build.kas_pins, "read_merged_config", return_value=PAIR_MERGED),
        patch.object(kas_build.repo_reuse, "gather_local_facts", side_effect=_pair_facts),
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)),
    ):
        _build_kas_arg(cfg, kas_yaml, overlay)


def test_host_mode_core_side_pairs_and_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, kas_yaml, overlay = _meta_avocado_setup(tmp_path)
    cfg = replace(cfg, host_mode=True)
    (tmp_path / "bitbake").mkdir()

    _build_pair(cfg, kas_yaml, overlay)

    side_rel = f".bakar/pins/openembedded-core/{PIN[:12]}"
    overlay_data = yaml.safe_load((cfg.bsp_root / "avocado-reuse.yml").read_text(encoding="utf-8"))
    assert overlay_data["repos"]["openembedded-core"] == {"path": side_rel}
    link = tmp_path / ".bakar" / "pins" / "openembedded-core" / "bitbake"
    assert link.is_symlink()
    assert link.resolve() == (tmp_path / "bitbake").resolve()


def test_container_mode_core_side_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, kas_yaml, overlay = _meta_avocado_setup(tmp_path)
    cfg = replace(cfg, host_mode=False)
    (tmp_path / "bitbake").mkdir()

    with pytest.raises(RuntimeError, match="openembedded-core"):
        _build_pair(cfg, kas_yaml, overlay)

    assert not (cfg.bsp_root / "avocado-reuse.yml").exists()
    assert not (tmp_path / ".bakar" / "pins" / "openembedded-core" / "bitbake").exists()


@pytest.mark.parametrize(
    "error",
    [PermissionError(13, "Permission denied"), FileExistsError(17, "File exists")],
    ids=["eacces", "eexist-race"],
)
def test_link_os_error_is_a_one_line_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, kas_yaml, overlay = _meta_avocado_setup(tmp_path)
    cfg = replace(cfg, host_mode=True)
    (tmp_path / "bitbake").mkdir()
    link = tmp_path / ".bakar" / "pins" / "openembedded-core" / "bitbake"
    error.filename = str(link)

    with (
        patch.object(kas_build.repo_reuse.os, "symlink", side_effect=error),
        pytest.raises(RuntimeError) as exc,
    ):
        _build_pair(cfg, kas_yaml, overlay)

    msg = str(exc.value)
    assert "\n" not in msg
    assert str(link) in msg
    assert str(error.strerror) in msg
    assert not (cfg.bsp_root / "avocado-reuse.yml").exists()


def _syncable_refusal(tmp_path: Path, extra_overlays: list[Path] | None, main_pin: str = PIN) -> str:
    """The refusal for a clean bitbake clone off the merged pin ``PIN``; the main file alone pins ``main_pin``."""
    cfg, kas_yaml, overlay = _meta_avocado_setup(tmp_path)

    def read(_kas_yaml: Path, *, checkout: Path, extra_files: list[Path] | None = None, **_kw: object) -> dict:
        # The build merges its overlay stack; bakar sync reads the main file alone.
        pin = PIN if extra_files else main_pin
        return {"repos": {"bitbake": {"url": "https://example.invalid/bitbake.git", "commit": pin}}}

    def facts_for(entries: list) -> list[RepoFacts]:
        # Clean but off its pin, and bitbake takes no side checkout: bakar sync
        # could move it, were the pin the main file's own.
        return [RepoFacts(True, OTHER_HEAD, True, True, False, None, None) for _ in entries]

    with (
        patch.object(kas_build.kas_pins, "read_merged_config", side_effect=read),
        patch.object(kas_build.repo_reuse, "gather_local_facts", side_effect=facts_for),
        patch("bakar.steps.kas_build.subprocess.run") as run,
        pytest.raises(RuntimeError) as exc,
    ):
        _build_kas_arg(cfg, kas_yaml, overlay, extra_overlays)
    run.assert_not_called()
    return str(exc.value)


def test_refusal_with_a_user_overlay_that_repins_a_blocker_does_not_suggest_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    extra = tmp_path / "repin.yml"
    extra.write_text(f"header:\n  version: 16\nrepos:\n  bitbake:\n    commit: {PIN}\n", encoding="utf-8")

    msg = _syncable_refusal(tmp_path, [extra], main_pin="a" * 40)

    assert "with: bakar sync" not in msg
    assert "bakar sync does not read" in msg
    assert "repin.yml" in msg
    assert f"bitbake at {PIN[:12]}" in msg
    assert "by hand" in msg


def test_refusal_with_a_user_overlay_that_sets_no_pin_still_suggests_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    extra = tmp_path / "my-localconf.yml"
    extra.write_text('header:\n  version: 16\nlocal_conf_header:\n  mine: |\n    FOO = "1"\n', encoding="utf-8")

    msg = _syncable_refusal(tmp_path, [extra])

    # The overlay leaves every pin as the main file sets it, so bakar sync moves the clone where the build wants it.
    assert "with: bakar sync " in msg
    assert "does not read" not in msg


def test_refusal_with_only_bundled_tuning_overlays_still_suggests_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    tuning = _overlay_dir() / "bakar-tuning-cache-classify.yml"

    msg = _syncable_refusal(tmp_path, [tuning])

    assert "with: bakar sync " in msg


def test_foreign_link_refuses_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, kas_yaml, overlay = _meta_avocado_setup(tmp_path)
    cfg = replace(cfg, host_mode=True)
    (tmp_path / "bitbake").mkdir()
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    link = tmp_path / ".bakar" / "pins" / "openembedded-core" / "bitbake"
    link.parent.mkdir(parents=True)
    link.symlink_to(foreign)

    with pytest.raises(RuntimeError, match="bakar never replaces it") as exc:
        _build_pair(cfg, kas_yaml, overlay)

    assert str(link) in str(exc.value)
    assert link.resolve() == foreign.resolve()
    assert not (cfg.bsp_root / "avocado-reuse.yml").exists()


# ---------------------------------------------------------------------------
# layer repository wrapper and pin
# ---------------------------------------------------------------------------


def _layer_setup(tmp_path: Path) -> tuple[BuildConfig, LayerRepo, Path]:
    checkout = tmp_path / "layers" / "meta-zedx"
    entry = _kas_file(checkout)
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = _cfg(ws, entry)
    layer = LayerRepo(
        checkout=checkout,
        name="meta-zedx",
        dependency={"url": "https://example.invalid/meta-avocado.git", "branch": "main"},
        is_linked_worktree=False,
        checkout_name="meta-zedx",
        branch="main",
        entry=entry.resolve(),
    )
    return cfg, layer, ws / "meta-avocado"


@pytest.mark.parametrize("dep_name", ["workspace", "side"])
def test_layer_wrapper_declares_both_repos_by_path(tmp_path: Path, dep_name: str) -> None:
    cfg, layer, workspace_clone = _layer_setup(tmp_path)
    dependency = workspace_clone if dep_name == "workspace" else tmp_path / "side" / "meta-avocado"

    wrapper = kas_build._write_layer_wrapper(cfg, layer, dependency)

    assert wrapper == cfg.bsp_root / "avocado-wrapper.yml"
    data = yaml.safe_load(wrapper.read_text(encoding="utf-8"))
    assert data["header"]["includes"] == [{"repo": "meta-zedx", "file": "kas/machine/m.yml"}]
    assert data["repos"]["meta-zedx"] == {"path": str(layer.checkout.resolve())}
    assert "layers" not in data["repos"]["meta-zedx"]
    assert data["repos"]["meta-avocado"]["path"] == str(dependency.resolve())


def test_layer_pin_redeclares_layer_repo_by_absolute_path_only(tmp_path: Path) -> None:
    cfg, layer, _dep = _layer_setup(tmp_path)

    rel = kas_build._write_layer_pin(cfg, layer)

    assert rel == Path("avocado-pin.yml")
    data = yaml.safe_load((cfg.bsp_root / rel).read_text(encoding="utf-8"))
    assert data == {"header": {"version": 16}, "repos": {"meta-zedx": {"path": str(layer.checkout.resolve())}}}


def test_meta_avocado_pin_stays_byte_identical(tmp_path: Path) -> None:
    cfg, kas_yaml, _overlay, checkout = _outside_setup(tmp_path)

    kas_build._write_meta_avocado_pin(cfg, kas_yaml)

    expected = f'header:\n  version: 16\nrepos:\n  meta-avocado:\n    path: "{checkout.resolve()}"\n'
    assert (cfg.bsp_root / "avocado-pin.yml").read_text(encoding="utf-8") == expected


# ---------------------------------------------------------------------------
# layer repository entry under the reuse policy
# ---------------------------------------------------------------------------

LAYER_MERGED = {"repos": {"meta-vendor": {"url": "https://example.invalid/vendor.git", "commit": PIN}}}


def _layer_build_setup(tmp_path: Path) -> tuple[BuildConfig, LayerRepo, Path]:
    cfg, layer, _dep = _layer_setup(tmp_path)
    overlay = tmp_path / "overlay.yml"
    overlay.write_text("header:\n  version: 16\n", encoding="utf-8")
    return cfg, layer, overlay


def _layer_states(ws: Path, *, dependency_side: Path | None = None) -> list[RepoState]:
    dependency = (
        RepoState("meta-avocado", ws / "meta-avocado", PIN, "frozen")
        if dependency_side is None
        else RepoState("meta-avocado", ws / "meta-avocado", PIN, "side", dependency_side, False)
    )
    return [dependency, RepoState("meta-vendor", ws / "meta-vendor", PIN, "frozen")]


def _build_layer(cfg: BuildConfig, layer: LayerRepo, overlay: Path, states: list[RepoState]) -> list[str]:
    """Run ``_build_kas_arg`` for a layer entry with every kas and git call mocked; return the dump argv."""
    with (
        patch.object(kas_build, "layer_repo_of", return_value=layer),
        patch.object(kas_build.kas_pins, "read_merged_config_layer", return_value=LAYER_MERGED),
        patch.object(kas_build.repo_reuse, "classify", return_value=states),
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)) as run,
    ):
        _build_kas_arg(cfg, layer.entry or Path(), overlay)
    return run.call_args.args[0]


def test_layer_entry_dump_carries_reuse_overlay_then_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, layer, overlay = _layer_build_setup(tmp_path)

    argv = _build_layer(cfg, layer, overlay, _layer_states(cfg.workspace))

    assert argv[:2] == ["kas", "dump"]
    # The build's own dump takes no skip flag: the reuse overlay protects it.
    assert "--skip" not in argv
    files = argv[-1].split(":")
    assert files[0] == "avocado-wrapper.yml"
    assert files[-2:] == ["avocado-reuse.yml", "avocado-pin.yml"]
    overlay_data = yaml.safe_load((cfg.bsp_root / "avocado-reuse.yml").read_text(encoding="utf-8"))
    assert overlay_data["repos"]["meta-avocado"] == {"path": "meta-avocado", "url": None}


def test_layer_entry_is_classified_before_the_wrapper_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, layer, overlay = _layer_build_setup(tmp_path)
    wrapper = cfg.bsp_root / "avocado-wrapper.yml"
    seen: list[bool] = []

    def read(*_a: object, **_k: object) -> dict:
        seen.append(wrapper.exists())
        return LAYER_MERGED

    def classify(*_a: object, **_k: object) -> list[RepoState]:
        seen.append(wrapper.exists())
        return _layer_states(cfg.workspace)

    with (
        patch.object(kas_build, "layer_repo_of", return_value=layer),
        patch.object(kas_build.kas_pins, "read_merged_config_layer", side_effect=read),
        patch.object(kas_build.repo_reuse, "classify", side_effect=classify),
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)),
    ):
        _build_kas_arg(cfg, layer.entry or Path(), overlay)

    assert seen == [False, False]
    assert wrapper.exists()


def test_layer_entry_classifies_the_dependency_from_its_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, layer, overlay = _layer_build_setup(tmp_path)
    layer.dependency["commit"] = PIN
    with (
        patch.object(kas_build, "layer_repo_of", return_value=layer),
        patch.object(kas_build.kas_pins, "read_merged_config_layer", return_value=LAYER_MERGED) as read,
        patch.object(kas_build.repo_reuse, "classify", return_value=_layer_states(cfg.workspace)) as classify,
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)),
    ):
        _build_kas_arg(cfg, layer.entry or Path(), overlay)

    assert read.call_args.args[1] == cfg.workspace / "meta-avocado"
    assert read.call_args.kwargs["wrap_parent"] == cfg.bsp_root
    repos = classify.call_args.args[0]
    assert repos["meta-avocado"] == {
        "url": "https://example.invalid/meta-avocado.git",
        "branch": "main",
        "commit": PIN,
        "path": "meta-avocado",
    }
    assert repos["meta-vendor"] == LAYER_MERGED["repos"]["meta-vendor"]


def test_layer_entry_wrapper_names_the_side_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, layer, overlay = _layer_build_setup(tmp_path)
    side = cfg.workspace / ".bakar" / "pins" / "meta-avocado" / PIN[:12]

    _build_layer(cfg, layer, overlay, _layer_states(cfg.workspace, dependency_side=side))

    wrapper = yaml.safe_load((cfg.bsp_root / "avocado-wrapper.yml").read_text(encoding="utf-8"))
    assert wrapper["repos"]["meta-avocado"]["path"] == str(side.resolve())
    # The build wrapper keeps the entry's url, unlike the read-only probe's.
    assert "url" not in wrapper["repos"]["meta-avocado"]


def test_layer_entry_wrapper_names_the_workspace_clone_when_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, layer, overlay = _layer_build_setup(tmp_path)

    _build_layer(cfg, layer, overlay, _layer_states(cfg.workspace))

    wrapper = yaml.safe_load((cfg.bsp_root / "avocado-wrapper.yml").read_text(encoding="utf-8"))
    assert wrapper["repos"]["meta-avocado"]["path"] == str((cfg.workspace / "meta-avocado").resolve())
    pin = yaml.safe_load((cfg.bsp_root / "avocado-pin.yml").read_text(encoding="utf-8"))
    assert pin["repos"] == {"meta-zedx": {"path": str(layer.checkout.resolve())}}


@pytest.mark.parametrize(
    "exc", [ReleaseRoutingError("commit abc of meta-avocado is not in the clone"), ReleaseRoutingError("third repo")]
)
def test_unclassifiable_layer_entry_refuses_before_kas_and_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: ReleaseRoutingError
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, layer, overlay = _layer_build_setup(tmp_path)

    with (
        patch.object(kas_build, "layer_repo_of", return_value=layer),
        patch.object(kas_build.kas_pins, "read_merged_config_layer", side_effect=exc),
        patch("bakar.steps.kas_build.subprocess.run") as run,
        pytest.raises(RuntimeError, match=str(exc)),
    ):
        _build_kas_arg(cfg, layer.entry or Path(), overlay)

    run.assert_not_called()
    assert not (cfg.bsp_root / "avocado-wrapper.yml").exists()
    assert not (cfg.bsp_root / "avocado-reuse.yml").exists()


def test_blocked_layer_entry_refuses_without_writing_or_running_kas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, layer, overlay = _layer_build_setup(tmp_path)
    blocked = [
        RepoState("bitbake", cfg.workspace / "bitbake", PIN, "side", blocking=REASON, head=OTHER_HEAD, clean=True)
    ]

    with (
        patch.object(kas_build, "layer_repo_of", return_value=layer),
        patch.object(kas_build.kas_pins, "read_merged_config_layer", return_value=LAYER_MERGED),
        patch.object(kas_build.repo_reuse, "classify", return_value=blocked),
        patch("bakar.steps.kas_build.subprocess.run") as run,
        pytest.raises(RuntimeError) as err,
    ):
        _build_kas_arg(cfg, layer.entry or Path(), overlay)

    assert "bitbake" in str(err.value)
    # bakar sync reads meta-avocado kas files only, so a layer entry's repair is by hand.
    assert "bakar sync" not in str(err.value)
    assert "git -C <clone> checkout <pin>" in str(err.value)
    assert REASON in str(err.value)
    run.assert_not_called()
    assert not (cfg.bsp_root / "avocado-wrapper.yml").exists()


def test_ambiguous_layer_entry_refuses_under_reuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, layer, overlay = _layer_build_setup(tmp_path)

    with (
        patch.object(kas_build, "layer_repo_of", side_effect=ReleaseRoutingError("declares 2 repositories")),
        patch("bakar.steps.kas_build.subprocess.run") as run,
        pytest.raises(RuntimeError, match="declares 2 repositories"),
    ):
        _build_kas_arg(cfg, layer.entry or Path(), overlay)

    run.assert_not_called()


@pytest.mark.parametrize("policy", ["kas", None])
def test_layer_entry_without_the_reuse_policy_keeps_the_existing_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str | None
) -> None:
    if policy is None:
        monkeypatch.delenv("BAKAR_REPO_POLICY", raising=False)
    else:
        monkeypatch.setenv("BAKAR_REPO_POLICY", policy)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "meta-avocado").mkdir()
    checkout = ws / "meta-zedx"
    entry = _kas_file(checkout)
    cfg = _cfg(ws, entry)
    layer = LayerRepo(checkout, "meta-zedx", {"url": "u"}, False, "meta-zedx", "main", entry.resolve())
    overlay = tmp_path / "overlay.yml"
    overlay.write_text("header:\n  version: 16\n", encoding="utf-8")

    with (
        patch.object(kas_build, "layer_repo_of", return_value=layer) as detect,
        patch.object(kas_build.kas_pins, "read_merged_config_layer") as read,
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)) as run,
    ):
        _build_kas_arg(cfg, entry, overlay)

    # An explicit kas policy is honoured, not refused; the layer reader never runs.
    detect.assert_not_called()
    read.assert_not_called()
    assert "avocado-reuse.yml" not in run.call_args.args[0][-1]


def test_reuse_outside_a_layer_or_meta_avocado_checkout_still_notes_and_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "meta-avocado").mkdir()
    entry = _kas_file(ws / "meta-other")
    cfg = _cfg(ws, entry)
    overlay = tmp_path / "overlay.yml"
    overlay.write_text("header:\n  version: 16\n", encoding="utf-8")

    with (
        patch.object(kas_build, "layer_repo_of", return_value=None),
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)) as run,
    ):
        _build_kas_arg(cfg, entry, overlay)

    assert "not inside a meta-avocado checkout, using the kas policy" in capsys.readouterr().err
    assert "avocado-reuse.yml" not in run.call_args.args[0][-1]


def test_post_build_graph_capture_dump_carries_the_reuse_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capture's target-resolving ``kas dump`` goes through the same protected assembly as the build."""
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    cfg, layer, overlay = _layer_build_setup(tmp_path)
    log = MagicMock()
    ctx = kas_build.KasBuildContext(
        cfg=cfg, log=log, kas_yaml=layer.entry or Path(), overlay_source=overlay, extra_overlays=[]
    )

    with (
        patch.object(kas_build, "layer_repo_of", return_value=layer),
        patch.object(kas_build.kas_pins, "read_merged_config_layer", return_value=LAYER_MERGED),
        patch.object(kas_build.repo_reuse, "classify", return_value=_layer_states(cfg.workspace)),
        patch("bakar.steps.kas_build.subprocess.run", return_value=_completed(0, stdout=DUMP_YAML)) as run,
        patch.object(kas_build, "_run_kas_child", return_value=0),
        patch.object(kas_build, "_build_env", return_value={}),
        patch.object(kas_build, "_container_eventlog_path", return_value=None),
    ):
        kas_graph_capture._resolve_capture_target(ctx, log)

    argv = run.call_args.args[0]
    assert "--skip" not in argv
    assert argv[-1].split(":")[-2:] == ["avocado-reuse.yml", "avocado-pin.yml"]
