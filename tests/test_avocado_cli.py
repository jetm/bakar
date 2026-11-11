"""The avocado wrapper: the only module that starts avocado or writes stanza text."""

from __future__ import annotations

import ast
import hashlib
import stat
from pathlib import Path

import pytest
import yaml

from bakar.steps.avocado_cli import (
    AVOCADO_COMMANDS,
    OverlayRepo,
    UrlRepo,
    render_overlay_stanza,
    render_url_stanza,
    run_avocado_commands,
)

pytestmark = pytest.mark.unit

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "bakar"
WRAPPER = SRC_ROOT / "steps" / "avocado_cli.py"


def _stub(bin_dir: Path, log: Path, *, fail_on: str | None = None) -> None:
    """Install a stub ``avocado`` that records ``<subcommand> <cwd>`` per call."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / "avocado"
    exe.write_text(
        f'#!/bin/sh\necho "$1 $(pwd)" >> "{log}"\nif [ "$1" = "{fail_on or ""}" ]; then exit 3; fi\nexit 0\n',
        encoding="utf-8",
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)


def _calls(log: Path) -> list[tuple[str, str]]:
    if not log.exists():
        return []
    return [tuple(line.split(" ", 1)) for line in log.read_text().splitlines()]  # type: ignore[misc]


def _holds_avocado_literal(node: ast.AST) -> bool:
    if not isinstance(node, ast.List | ast.Tuple):
        return False
    return any(isinstance(e, ast.Constant) and e.value == "avocado" for e in node.elts)


def test_only_wrapper_spawns_avocado() -> None:
    offenders = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if path == WRAPPER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [f"{path.relative_to(SRC_ROOT)}:{n.lineno}" for n in ast.walk(tree) if _holds_avocado_literal(n)]
    assert offenders == []
    wrapper_tree = ast.parse(WRAPPER.read_text(encoding="utf-8"))
    assert any(_holds_avocado_literal(n) for n in ast.walk(wrapper_tree))


def test_stanza_has_one_entry_per_non_empty_repo_with_relative_paths(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    delta = tmp_path / "delta"
    project.mkdir()
    repos = [
        OverlayRepo(name="delta-target", path=delta / "target", stages=("rootfs",), package_count=2),
        OverlayRepo(name="delta-sdk", path=delta / "sdk", stages=("sdk",), package_count=0),
        OverlayRepo(name="delta-ext", path=delta / "ext", stages=("ext", "runtime"), package_count=1),
    ]
    text = render_overlay_stanza(project, repos)
    assert text == (
        "repos:\n"
        "  delta-target:\n"
        "    path: ../delta/target\n"
        "    stages: [rootfs]\n"
        "  delta-ext:\n"
        "    path: ../delta/ext\n"
        "    stages: [ext, runtime]\n"
    )


def test_stanza_parses_as_a_map_keyed_by_feed_name(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    repos = [
        OverlayRepo(name="delta-target", path=tmp_path / "d" / "t", stages=("rootfs", "runtime"), package_count=1),
        OverlayRepo(name="delta-sdk", path=tmp_path / "d" / "s", stages=("sdk",), package_count=3),
    ]
    assert yaml.safe_load(render_overlay_stanza(project, repos)) == {
        "repos": {
            "delta-target": {"path": "../d/t", "stages": ["rootfs", "runtime"]},
            "delta-sdk": {"path": "../d/s", "stages": ["sdk"]},
        }
    }


def _url_repos() -> list[UrlRepo]:
    return [
        UrlRepo(
            name="delta-target", url="http://127.0.0.1:8080/2026/edge/target/m", stages=("rootfs",), package_count=2
        ),
        UrlRepo(name="delta-sdk", url="http://127.0.0.1:8080/2026/edge/sdk/m", stages=("sdk",), package_count=0),
        UrlRepo(name="delta-ext", url="http://h:9/x", stages=("ext", "runtime"), package_count=1),
    ]


def test_url_stanza_exact_output_skips_empty_repos() -> None:
    assert render_url_stanza(_url_repos()) == (
        "repos:\n"
        "  delta-target:\n"
        "    url: http://127.0.0.1:8080/2026/edge/target/m\n"
        "    stages: [rootfs]\n"
        "  delta-ext:\n"
        "    url: http://h:9/x\n"
        "    stages: [ext, runtime]\n"
    )


def test_url_stanza_parses_as_a_map_keyed_by_feed_name() -> None:
    assert yaml.safe_load(render_url_stanza(_url_repos())) == {
        "repos": {
            "delta-target": {"url": "http://127.0.0.1:8080/2026/edge/target/m", "stages": ["rootfs"]},
            "delta-ext": {"url": "http://h:9/x", "stages": ["ext", "runtime"]},
        }
    }


def test_url_stanza_is_empty_when_every_repo_is_empty() -> None:
    assert render_url_stanza([UrlRepo(name="a", url="http://h", stages=("t",), package_count=0)]) == ""


def test_stanza_is_empty_when_every_repo_is_empty(tmp_path: Path) -> None:
    repos = [OverlayRepo(name="a", path=tmp_path / "a", stages=("target",), package_count=0)]
    assert render_overlay_stanza(tmp_path, repos) == ""


def test_commands_run_in_order_in_project_dir_and_leave_config_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    config = project / "avocado.yaml"
    config.write_text("distro:\n  release: 2024\n", encoding="utf-8")
    before = hashlib.sha256(config.read_bytes()).hexdigest()
    log = tmp_path / "log"
    _stub(tmp_path / "bin", log)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")

    result = run_avocado_commands(project, timeout=30)

    assert result.ok is True
    assert result.failed_command is None
    assert _calls(log) == [(c, str(project)) for c in ("unlock", "install", "build")]
    assert AVOCADO_COMMANDS == ("unlock", "install", "build")
    assert hashlib.sha256(config.read_bytes()).hexdigest() == before


def test_first_failure_stops_the_sequence_and_names_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    log = tmp_path / "log"
    _stub(tmp_path / "bin", log, fail_on="unlock")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")

    result = run_avocado_commands(project, timeout=30)

    assert result.ok is False
    assert result.failed_command == "unlock"
    assert result.returncode == 3
    assert [c for c, _ in _calls(log)] == ["unlock"]


def test_missing_executable_is_a_failure_not_an_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    result = run_avocado_commands(tmp_path, timeout=30)
    assert result.ok is False
    assert result.failed_command == "unlock"
    assert result.returncode != 0


def test_timeout_kills_the_command_and_reports_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "avocado"
    exe.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    result = run_avocado_commands(tmp_path, timeout=0.5, grace=0.5)

    assert result.ok is False
    assert result.failed_command == "unlock"
    assert result.timed_out is True
