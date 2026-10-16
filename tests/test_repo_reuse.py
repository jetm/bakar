"""Tests for bakar.steps.repo_reuse against real temporary git repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from bakar.steps.repo_reuse import RepoReuseError, RepoState, classify, reuse_overlay_text

PIN = "0123456789abcdef0123456789abcdef01234567"


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _make_repo(path: Path) -> tuple[str, str]:
    """Create a repo with two commits; return (first, second) SHAs, HEAD at second."""
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "f.txt").write_text("one\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "one")
    first = _git(path, "rev-parse", "HEAD")
    (path / "f.txt").write_text("two\n")
    _git(path, "commit", "-q", "-am", "two")
    second = _git(path, "rev-parse", "HEAD")
    return first, second


def _entry(commit: str | None, **extra: str) -> dict:
    entry: dict = {"url": "https://example.com/r.git", **extra}
    if commit is not None:
        entry["commit"] = commit
    return entry


def test_clean_at_pin_is_frozen(tmp_path: Path) -> None:
    _, second = _make_repo(tmp_path / "meta-x")
    (state,) = classify({"meta-x": _entry(second)}, tmp_path)
    assert state.state == "frozen"
    assert state.path == tmp_path / "meta-x"
    assert state.pin == second


def test_off_pin_is_side_and_head_unchanged(tmp_path: Path) -> None:
    repo = tmp_path / "meta-x"
    first, second = _make_repo(repo)
    (state,) = classify({"meta-x": _entry(first)}, tmp_path)
    assert state.state == "side"
    assert state.side_path == tmp_path / ".bakar" / "pins" / "meta-x" / first[:12]
    assert state.side_exists is False
    assert _git(repo, "rev-parse", "HEAD") == second
    assert not state.side_path.exists()


def test_dirty_at_pin_is_side(tmp_path: Path) -> None:
    repo = tmp_path / "meta-x"
    _, second = _make_repo(repo)
    (repo / "f.txt").write_text("modified\n")
    (state,) = classify({"meta-x": _entry(second)}, tmp_path)
    assert state.state == "side"
    assert (repo / "f.txt").read_text() == "modified\n"


def test_slow_git_read_does_not_misclassify_clean_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, second = _make_repo(tmp_path / "meta-x")
    real_run = subprocess.run

    def slow_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        timeout = kwargs.get("timeout")
        if isinstance(timeout, int | float) and timeout <= 5:
            raise subprocess.TimeoutExpired(cmd="git", timeout=timeout)
        return real_run(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr("bakar.gitutil.subprocess.run", slow_run)
    (state,) = classify({"meta-x": _entry(second)}, tmp_path)
    assert state.state == "frozen"


def test_absent_is_missing(tmp_path: Path) -> None:
    (state,) = classify({"meta-x": _entry("a" * 40)}, tmp_path)
    assert state.state == "missing"


def test_url_without_commit_raises(tmp_path: Path) -> None:
    with pytest.raises(RepoReuseError, match="meta-x"):
        classify({"meta-x": _entry(None)}, tmp_path)


def test_url_less_entries_skipped(tmp_path: Path) -> None:
    assert classify({"meta-avocado": {"path": "meta-avocado"}, "x": None}, tmp_path) == []


def test_path_resolution_relative_and_absolute(tmp_path: Path) -> None:
    _, rel_pin = _make_repo(tmp_path / "layers" / "rel")
    abs_repo = tmp_path / "elsewhere" / "abs"
    _, abs_pin = _make_repo(abs_repo)
    states = classify(
        {
            "r": _entry(rel_pin, path="layers/rel"),
            "a": _entry(abs_pin, path=str(abs_repo)),
        },
        tmp_path / "ws",
    )
    assert [s.path for s in states] == [tmp_path / "ws" / "layers" / "rel", abs_repo]
    assert [s.state for s in states] == ["missing", "frozen"]


def _side_checkout(tmp_path: Path, name: str, pin: str) -> Path:
    side = tmp_path / ".bakar" / "pins" / name / pin[:12]
    side.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", str(tmp_path / name), str(side)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(side), "-c", "advice.detachedHead=false", "switch", "-q", "--detach", pin],
        check=True,
        capture_output=True,
    )
    return side


def test_existing_clean_side_checkout_emits_path_and_null_url(tmp_path: Path) -> None:
    first, _ = _make_repo(tmp_path / "meta-x")
    side = _side_checkout(tmp_path, "meta-x", first)
    (state,) = classify({"meta-x": _entry(first)}, tmp_path)
    assert state.state == "side"
    assert state.side_exists is True
    doc = yaml.safe_load(reuse_overlay_text([state], tmp_path))
    assert doc["repos"]["meta-x"] == {"path": side.relative_to(tmp_path).as_posix(), "url": None}


def test_existing_dirty_side_checkout_raises(tmp_path: Path) -> None:
    first, _ = _make_repo(tmp_path / "meta-x")
    side = _side_checkout(tmp_path, "meta-x", first)
    (side / "f.txt").write_text("dirty\n")
    with pytest.raises(RepoReuseError, match=str(side)):
        classify({"meta-x": _entry(first)}, tmp_path)


def test_existing_side_checkout_at_other_head_raises(tmp_path: Path) -> None:
    first, second = _make_repo(tmp_path / "meta-x")
    side = _side_checkout(tmp_path, "meta-x", second)
    renamed = side.parent / first[:12]
    side.rename(renamed)
    with pytest.raises(RepoReuseError, match=str(renamed)):
        classify({"meta-x": _entry(first)}, tmp_path)


def test_overlay_parses_and_omits_missing(tmp_path: Path) -> None:
    _, second = _make_repo(tmp_path / "frozen")
    first, _ = _make_repo(tmp_path / "off")
    states = classify(
        {
            "frozen": _entry(second),
            "off": _entry(first),
            "gone": _entry("b" * 40),
        },
        tmp_path,
    )
    doc = yaml.safe_load(reuse_overlay_text(states, tmp_path))
    assert doc["header"] == {"version": 16}
    assert doc["repos"]["frozen"] == {"path": "frozen", "url": None}
    assert doc["repos"]["off"] == {"path": f".bakar/pins/off/{first[:12]}"}
    assert "gone" not in doc["repos"]


def test_overlay_path_under_the_workspace_is_relative(tmp_path: Path) -> None:
    """kas resolves a relative path against KAS_WORK_DIR, which is ``/work`` in the container."""
    ws = tmp_path / "ws"
    states = [
        RepoState("meta-a", ws / "layers" / "meta-a", PIN, "frozen"),
        RepoState("meta-b", ws / "meta-b", PIN, "side", ws / ".bakar" / "pins" / "meta-b" / PIN[:12], True),
    ]
    doc = yaml.safe_load(reuse_overlay_text(states, ws))
    assert doc["repos"]["meta-a"]["path"] == "layers/meta-a"
    assert doc["repos"]["meta-b"]["path"] == f".bakar/pins/meta-b/{PIN[:12]}"


def test_overlay_path_outside_the_workspace_stays_absolute(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    outside = tmp_path / "elsewhere" / "meta-a"
    # A ``..`` that leaves the workspace must not pass as workspace-relative.
    dotted = ws / ".." / "other" / "meta-c"
    states = [
        RepoState("meta-a", outside, PIN, "frozen"),
        RepoState("meta-c", dotted, PIN, "frozen"),
    ]
    doc = yaml.safe_load(reuse_overlay_text(states, ws))
    assert doc["repos"]["meta-a"]["path"] == str(outside)
    assert Path(doc["repos"]["meta-c"]["path"]).is_absolute()


def test_overlay_path_through_a_symlinked_workspace_is_relative(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "meta-a").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    state = RepoState("meta-a", real / "meta-a", PIN, "frozen")
    doc = yaml.safe_load(reuse_overlay_text([state], link))
    assert doc["repos"]["meta-a"]["path"] == "meta-a"
