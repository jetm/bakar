"""check_kas_yaml_syntax for kas files outside a routed workspace's own meta-avocado.

A kas file whose meta-avocado checkout is not ``cfg.workspace/meta-avocado`` is
validated through a wrapper pinning meta-avocado to that checkout. Under
``BAKAR_REPO_POLICY=reuse`` the workspace is shared, and a plain dump against
it would move every clean repository to its pin, so validation goes through
``kas_pins.read_merged_config`` in a throwaway work directory instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from bakar.config import BuildConfig
from bakar.diagnostics import Severity, Status, check_kas_yaml_syntax

_MACHINE = "header:\n  version: 16\nmachine: qemux86-64\n"


def _cfg(workspace: Path, kas_yaml: Path) -> BuildConfig:
    return BuildConfig(
        workspace=workspace,
        host_mode=False,
        bsp_family="generic",
        machine="qemux86-64",
        distro="poky",
        image="core-image-minimal",
        manifest="kas.yml",
        repo_url="https://example.invalid/none.git",
        repo_branch="scarthgap",
        kas_container_image="jetm/kas-build-env:latest",
        kas_yaml_override=kas_yaml,
    )


def _kas_file(checkout: Path) -> Path:
    path = checkout / "kas" / "machine" / "m.yml"
    path.parent.mkdir(parents=True)
    path.write_text(_MACHINE)
    return path


@pytest.fixture
def outside(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A routed workspace plus a meta-avocado main checkout that lives elsewhere."""
    ws = tmp_path / "ws"
    ws.mkdir()
    checkout = tmp_path / "src" / "meta-avocado"
    return ws, checkout, _kas_file(checkout)


@pytest.fixture
def inside(tmp_path: Path) -> tuple[Path, Path]:
    """A workspace whose own meta-avocado holds the kas file."""
    ws = tmp_path / "ws"
    return ws, _kas_file(ws / "meta-avocado")


@pytest.fixture(autouse=True)
def _kas_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BAKAR_REPO_POLICY", raising=False)
    monkeypatch.setattr("bakar.diagnostics.shutil.which", lambda _name: "/usr/bin/kas")


class _Recorder:
    """Stand-in for subprocess.run that records every call and its wrapper content."""

    def __init__(self, *, returncode: int = 0, stderr: str = "", stdout: str = "header:\n  version: 16\n") -> None:
        self.calls: list[dict[str, Any]] = []
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout

    def __call__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        target = Path(argv[-1])
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": kwargs.get("cwd"),
                "env": kwargs.get("env") or {},
                "content": target.read_text() if target.exists() else None,
            }
        )
        stderr = self.stderr.replace("{target}", str(target))
        return subprocess.CompletedProcess(args=argv, returncode=self.returncode, stdout=self.stdout, stderr=stderr)


def _install(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    # diagnostics and kas_pins share the subprocess module, so this sees both.
    monkeypatch.setattr(subprocess, "run", recorder)


def test_main_checkout_outside_workspace_dumps_a_wrapper(
    monkeypatch: pytest.MonkeyPatch, outside: tuple[Path, Path, Path]
) -> None:
    ws, checkout, kas_yaml = outside
    rec = _Recorder()
    _install(monkeypatch, rec)

    result = check_kas_yaml_syntax(_cfg(ws, kas_yaml))

    assert result.status is Status.PASS
    assert len(rec.calls) == 1
    call = rec.calls[0]
    dumped = Path(call["argv"][-1])  # type: ignore[index]
    assert dumped != kas_yaml
    assert dumped.name == "avocado-wrapper.yml"
    wrapper = yaml.safe_load(call["content"])  # type: ignore[arg-type]
    assert wrapper["repos"]["meta-avocado"]["path"] == str(checkout.resolve())
    assert wrapper["header"]["includes"][0] == {"repo": "meta-avocado", "file": "kas/machine/m.yml"}
    assert call["cwd"] == ws
    assert call["env"]["KAS_WORK_DIR"] == str(ws)  # type: ignore[index]
    assert str(kas_yaml) in result.message
    assert "avocado-wrapper" not in result.message


def test_kas_file_inside_workspace_meta_avocado_is_dumped_directly(
    monkeypatch: pytest.MonkeyPatch, inside: tuple[Path, Path]
) -> None:
    ws, kas_yaml = inside
    rec = _Recorder()
    _install(monkeypatch, rec)

    assert check_kas_yaml_syntax(_cfg(ws, kas_yaml)).status is Status.PASS
    assert [c["argv"] for c in rec.calls] == [["kas", "dump", str(kas_yaml)]]
    assert rec.calls[0]["env"]["KAS_WORK_DIR"] == str(ws)  # type: ignore[index]


def test_workspace_given_through_a_symlink_still_dumps_directly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, inside: tuple[Path, Path]
) -> None:
    ws, kas_yaml = inside
    link = tmp_path / "ws-link"
    link.symlink_to(ws)
    rec = _Recorder()
    _install(monkeypatch, rec)

    assert check_kas_yaml_syntax(_cfg(link, kas_yaml)).status is Status.PASS
    assert [c["argv"] for c in rec.calls] == [["kas", "dump", str(kas_yaml)]]


def test_failure_message_never_names_the_temporary_wrapper(
    monkeypatch: pytest.MonkeyPatch, outside: tuple[Path, Path, Path]
) -> None:
    ws, _checkout, kas_yaml = outside
    rec = _Recorder(returncode=1, stderr="2026-01-01 00:00:00 - ERROR    - Include file missing from {target}\n")
    _install(monkeypatch, rec)

    result = check_kas_yaml_syntax(_cfg(ws, kas_yaml))

    tmp_dir = str(Path(rec.calls[0]["argv"][-1]).parent)  # type: ignore[index]
    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert str(kas_yaml) in result.message
    assert tmp_dir not in result.message
    assert "avocado-wrapper" not in result.message
    assert result.fix_hint is not None
    assert tmp_dir not in result.fix_hint
    assert f"kas dump {kas_yaml}" not in result.fix_hint


def _dumps_against(rec: _Recorder, workspace: Path) -> list[list[str]]:
    return [
        c["argv"]
        for c in rec.calls
        if c["argv"][:2] == ["kas", "dump"]
        and c["env"].get("KAS_WORK_DIR") in (str(workspace), str(workspace.resolve()))
    ]


@pytest.mark.parametrize("layout", ["outside", "inside"])
def test_reuse_policy_never_dumps_against_the_workspace(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, layout: str
) -> None:
    ws, kas_yaml = request.getfixturevalue(layout)[0], request.getfixturevalue(layout)[-1]
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    rec = _Recorder()
    _install(monkeypatch, rec)

    result = check_kas_yaml_syntax(_cfg(ws, kas_yaml))

    assert result.status is Status.PASS
    assert rec.calls, "the kas file must still be validated"
    assert _dumps_against(rec, ws) == []
    for call in rec.calls:
        work_dir = Path(call["env"]["KAS_WORK_DIR"])  # type: ignore[index]
        assert not work_dir.resolve().is_relative_to(ws.resolve())
    assert str(kas_yaml) in result.message


def test_reuse_policy_maps_a_routing_error_to_block(
    monkeypatch: pytest.MonkeyPatch, outside: tuple[Path, Path, Path]
) -> None:
    ws, _checkout, kas_yaml = outside
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    rec = _Recorder(returncode=1, stderr="2026-01-01 00:00:00 - ERROR    - bad include in {target}\n")
    _install(monkeypatch, rec)

    result = check_kas_yaml_syntax(_cfg(ws, kas_yaml))

    tmp_dir = str(Path(rec.calls[0]["argv"][-1]).parent)  # type: ignore[index]
    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert str(kas_yaml) in result.message
    assert "bad include" in result.message
    assert tmp_dir not in result.message
    assert _dumps_against(rec, ws) == []


def test_reuse_policy_outside_any_meta_avocado_validates_like_the_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The build falls back to the kas policy for this file, so the check does too.
    ws = tmp_path / "ws"
    kas_yaml = ws / "kas.yml"
    kas_yaml.parent.mkdir()
    kas_yaml.write_text(_MACHINE)
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    rec = _Recorder()
    _install(monkeypatch, rec)

    result = check_kas_yaml_syntax(_cfg(ws, kas_yaml))

    assert result.status is not Status.SKIP
    assert rec.calls, "the kas file must be validated"


def test_unknown_repo_policy_blocks(monkeypatch: pytest.MonkeyPatch, outside: tuple[Path, Path, Path]) -> None:
    ws, _checkout, kas_yaml = outside
    monkeypatch.setenv("BAKAR_REPO_POLICY", "resue")
    rec = _Recorder()
    _install(monkeypatch, rec)

    result = check_kas_yaml_syntax(_cfg(ws, kas_yaml))

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "'resue'" in result.message
    assert rec.calls == []


@pytest.mark.parametrize("missing", ["kas-binary", "kas-yaml"])
def test_unknown_repo_policy_blocks_before_either_skip(
    monkeypatch: pytest.MonkeyPatch, outside: tuple[Path, Path, Path], missing: str
) -> None:
    # The fixture's kas file sits in a meta-avocado checkout, the only kind of
    # build that reads the policy, so a typo must block even when the file is
    # not generated yet.
    ws, _checkout, kas_yaml = outside
    monkeypatch.setenv("BAKAR_REPO_POLICY", "resue")
    if missing == "kas-binary":
        monkeypatch.setattr("bakar.diagnostics.shutil.which", lambda _name: None)
    else:
        kas_yaml.unlink()
    rec = _Recorder()
    _install(monkeypatch, rec)

    result = check_kas_yaml_syntax(_cfg(ws, kas_yaml))

    assert result.status is Status.FAIL
    assert result.severity is Severity.BLOCK
    assert "'resue'" in result.message
    assert rec.calls == []


@pytest.mark.parametrize("generated", [False, True])
def test_unknown_repo_policy_is_ignored_for_a_plain_kas_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, generated: bool
) -> None:
    # A kas file outside any meta-avocado checkout never reads the policy at
    # build time, so a bad value must not block the doctor for it.
    ws = tmp_path / "plain"
    ws.mkdir()
    kas_yaml = ws / "kas.yml"
    if generated:
        kas_yaml.write_text(_MACHINE)
    monkeypatch.setenv("BAKAR_REPO_POLICY", "resue")
    rec = _Recorder()
    _install(monkeypatch, rec)

    result = check_kas_yaml_syntax(_cfg(ws, kas_yaml))

    if generated:
        assert result.status is Status.PASS
        assert [call["argv"] for call in rec.calls] == [["kas", "dump", str(kas_yaml)]]
    else:
        assert result.status is Status.SKIP
        assert rec.calls == []
