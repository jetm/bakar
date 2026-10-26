"""Tests for bakar.steps.repo_reuse against real temporary git repositories."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from bakar.steps.repo_reuse import (
    RepoFacts,
    RepoReuseError,
    RepoState,
    blocking,
    classify,
    classify_facts,
    ensure_core_bitbake_link,
    gather_local_facts,
    pinned_entries,
    render_blockers,
    render_pin_table,
    render_refusal,
    reuse_overlay_text,
)

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
        return real_run(*args, **kwargs)  # ty: ignore[no-matching-overload]

    monkeypatch.setattr("bakar.gitutil.subprocess.run", slow_run)
    (state,) = classify({"meta-x": _entry(second)}, tmp_path)
    assert state.state == "frozen"


def test_absent_is_missing(tmp_path: Path) -> None:
    (state,) = classify({"meta-x": _entry("a" * 40)}, tmp_path)
    assert state.state == "missing"


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


def test_existing_dirty_side_checkout_is_blocking(tmp_path: Path) -> None:
    first, _ = _make_repo(tmp_path / "meta-x")
    side = _side_checkout(tmp_path, "meta-x", first)
    (side / "f.txt").write_text("dirty\n")
    (state,) = classify({"meta-x": _entry(first)}, tmp_path)
    assert state.state == "side"
    assert state.blocking is not None
    assert str(side) in state.blocking
    # The blocking state still names the side checkout it refuses, and that it exists.
    assert state.side_path == side
    assert state.side_exists is True


def test_existing_side_checkout_at_other_head_is_blocking(tmp_path: Path) -> None:
    first, second = _make_repo(tmp_path / "meta-x")
    side = _side_checkout(tmp_path, "meta-x", second)
    renamed = side.parent / first[:12]
    side.rename(renamed)
    (state,) = classify({"meta-x": _entry(first)}, tmp_path)
    assert state.blocking is not None
    assert str(renamed) in state.blocking


def test_single_refusal_names_every_blocker(tmp_path: Path) -> None:
    bb_first, _ = _make_repo(tmp_path / "bitbake")
    oe_first, _ = _make_repo(tmp_path / "openembedded-core")
    first, _ = _make_repo(tmp_path / "meta-x")
    _side_checkout(tmp_path, "meta-x", first)
    (tmp_path / ".bakar" / "pins" / "meta-x" / first[:12] / "f.txt").write_text("dirty\n")
    states = classify(
        {
            "bitbake": _entry(bb_first),
            "openembedded-core": _entry(oe_first),
            "meta-x": _entry(first),
        },
        tmp_path,
    )
    assert [s.name for s in blocking(states)] == ["bitbake", "openembedded-core", "meta-x"]
    text = render_refusal(states, repair="bakar sync -f m.yml")
    assert text.splitlines()[0].startswith("3 repositories block")
    for name in ("bitbake", "openembedded-core", "meta-x"):
        assert f"  {name} ({tmp_path / name}): " in text
    assert "off its pin or has local changes" in text
    assert "has local changes or is not at its pin" in text
    assert f"pin {bb_first[:12]}" in text
    assert "clean" in text
    assert "pin commit present" in text
    assert "Move the clean clones to their pins with: bakar sync -f m.yml" in text


def test_commit_less_url_is_blocking_not_raised(tmp_path: Path) -> None:
    (state,) = classify({"meta-x": _entry(None)}, tmp_path)
    assert state.state == "invalid"
    assert state.pin is None
    assert state.blocking == "no commit pin"
    assert "pin none" in render_refusal([state], repair="r")


def test_non_hex_pin_is_invalid(tmp_path: Path) -> None:
    (state,) = classify({"meta-x": _entry("main")}, tmp_path)
    assert state.state == "invalid"
    assert state.blocking == "commit pin is not a hexadecimal commit id"


def test_only_side_repos_do_not_block(tmp_path: Path) -> None:
    first, _ = _make_repo(tmp_path / "meta-x")
    states = classify({"meta-x": _entry(first)}, tmp_path)
    assert states[0].state == "side"
    assert blocking(states) == []
    sides = [s for s in states if s.state == "side"]
    assert [s.name for s in sides] == ["meta-x"]


def test_refusal_lists_non_blocking_side_repos(tmp_path: Path) -> None:
    bb_first, _ = _make_repo(tmp_path / "bitbake")
    first, _ = _make_repo(tmp_path / "meta-x")
    states = classify({"bitbake": _entry(bb_first), "meta-x": _entry(first)}, tmp_path)
    text = render_refusal(states, repair="r")
    assert text.splitlines()[0].startswith("1 repository blocks")
    assert "Using side checkouts: meta-x" in text


def test_render_pin_table_lists_every_repo(tmp_path: Path) -> None:
    _, second = _make_repo(tmp_path / "frozen")
    first, _ = _make_repo(tmp_path / "off")
    states = classify(
        {"frozen": _entry(second), "off": _entry(first), "gone": _entry("a" * 40), "bad": _entry(None)},
        tmp_path,
    )
    lines = render_pin_table(states)
    assert len(lines) == 4
    assert lines[0].startswith("frozen: frozen, HEAD ")
    assert lines[1].startswith("off: side, ")
    assert lines[2].startswith("gone: missing, HEAD none, pin aaaaaaaaaaaa, unknown, pin commit unknown")
    assert lines[3].startswith("bad: invalid, HEAD none, pin none")


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


def test_pinned_entries_resolves_paths_and_side_paths(tmp_path: Path) -> None:
    repos = {
        "local": {"path": "x"},
        "meta-a": _entry(PIN, path="layers/meta-a"),
        "meta-b": _entry(None),
        "meta-c": _entry(PIN, path=str(tmp_path / "abs")),
    }
    a, b, c = pinned_entries(repos, tmp_path)
    assert (a.name, a.pin, a.path) == ("meta-a", PIN, tmp_path / "layers" / "meta-a")
    assert a.side_path == tmp_path / ".bakar" / "pins" / "meta-a" / PIN[:12]
    assert (b.pin, b.path, b.side_path) == (None, tmp_path / "meta-b", None)
    assert c.path == tmp_path / "abs"


def test_gather_local_facts_reports_head_clean_and_pin_presence(tmp_path: Path) -> None:
    first, second = _make_repo(tmp_path / "meta-x")
    (tmp_path / "meta-x" / "f.txt").write_text("dirty\n")
    entries = pinned_entries({"meta-x": _entry(first), "meta-y": _entry(PIN)}, tmp_path)
    fx, fy = gather_local_facts(entries)
    assert fx.exists
    assert fx.head == second
    assert fx.clean is False
    assert fx.pin_present is True
    assert fx.side_exists is False
    assert fy == RepoFacts.unknown()


def test_classify_facts_matches_classify(tmp_path: Path) -> None:
    first, second = _make_repo(tmp_path / "meta-x")
    repos = {"meta-x": _entry(second), "meta-y": _entry(first, path="meta-x"), "meta-z": _entry(PIN)}
    entries = pinned_entries(repos, tmp_path)
    states = classify_facts(entries, gather_local_facts(entries))
    assert [(s.name, s.state) for s in states] == [("meta-x", "frozen"), ("meta-y", "side"), ("meta-z", "missing")]
    assert states == classify(repos, tmp_path)
    assert states[0].head == second
    assert states[0].clean is True
    assert states[0].pin_present is True


def _core_and_bitbake(tmp_path: Path, *, bitbake_frozen: bool) -> dict:
    """A diverged openembedded-core beside a bitbake that is or is not at its pin."""
    core_first, _ = _make_repo(tmp_path / "openembedded-core")
    bb_first, bb_second = _make_repo(tmp_path / "bitbake")
    return {
        "bitbake": _entry(bb_second if bitbake_frozen else bb_first),
        "openembedded-core": _entry(core_first),
    }


def _paired_core(tmp_path: Path) -> RepoState:
    repos = _core_and_bitbake(tmp_path, bitbake_frozen=True)
    states = {s.name: s for s in classify(repos, tmp_path, pair_core_with_bitbake=True)}
    return states["openembedded-core"]


def test_core_pairs_with_frozen_bitbake_when_enabled(tmp_path: Path) -> None:
    repos = _core_and_bitbake(tmp_path, bitbake_frozen=True)
    states = {s.name: s for s in classify(repos, tmp_path, pair_core_with_bitbake=True)}
    core = states["openembedded-core"]
    assert core.state == "side"
    assert core.blocking is None
    assert core.side_path is not None
    assert core.paired_bitbake == tmp_path / "bitbake"
    assert states["bitbake"].state == "frozen"
    assert states["bitbake"].paired_bitbake is None


def test_core_blocks_when_pairing_disabled(tmp_path: Path) -> None:
    repos = _core_and_bitbake(tmp_path, bitbake_frozen=True)
    states = {s.name: s for s in classify(repos, tmp_path)}
    core = states["openembedded-core"]
    assert core.blocking == "off its pin or has local changes"
    assert core.paired_bitbake is None


def test_bitbake_diverged_always_blocks(tmp_path: Path) -> None:
    repos = _core_and_bitbake(tmp_path, bitbake_frozen=False)
    for pair in (False, True):
        states = {s.name: s for s in classify(repos, tmp_path, pair_core_with_bitbake=pair)}
        assert states["bitbake"].blocking == "off its pin or has local changes"
        assert states["openembedded-core"].blocking is not None
        assert states["openembedded-core"].paired_bitbake is None


def test_ensure_core_bitbake_link_creates_relative_link(tmp_path: Path) -> None:
    core = _paired_core(tmp_path)
    assert core.side_path is not None
    ensure_core_bitbake_link(core)
    link = core.side_path.parent / "bitbake"
    assert link.is_symlink()
    assert not Path(os.readlink(link)).is_absolute()
    assert link.resolve() == (tmp_path / "bitbake").resolve()


def test_ensure_core_bitbake_link_reuses_matching_link(tmp_path: Path) -> None:
    core = _paired_core(tmp_path)
    assert core.side_path is not None
    ensure_core_bitbake_link(core)
    link = core.side_path.parent / "bitbake"
    target = os.readlink(link)
    ensure_core_bitbake_link(core)
    assert os.readlink(link) == target


def test_ensure_core_bitbake_link_refuses_foreign_entry(tmp_path: Path) -> None:
    core = _paired_core(tmp_path)
    assert core.side_path is not None
    link_dir = core.side_path.parent
    link_dir.mkdir(parents=True)
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    (link_dir / "bitbake").symlink_to(foreign)
    with pytest.raises(RepoReuseError, match="never replaces"):
        ensure_core_bitbake_link(core)
    assert os.readlink(link_dir / "bitbake") == str(foreign)

    plain_dir = tmp_path / "plain" / "pins"
    (plain_dir / "bitbake").mkdir(parents=True)
    other = RepoState(core.name, core.path, core.pin, "side", plain_dir / "sha", paired_bitbake=tmp_path / "bitbake")
    with pytest.raises(RepoReuseError, match="never replaces"):
        ensure_core_bitbake_link(other)
    assert (plain_dir / "bitbake").is_dir()


def test_ensure_core_bitbake_link_through_a_symlinked_pins_dir(tmp_path: Path) -> None:
    # <ws>/.bakar/pins lives on another disk, at a different depth than the workspace.
    elsewhere = tmp_path / "otherdisk" / "deep" / "pins"
    elsewhere.mkdir(parents=True)
    (tmp_path / ".bakar").mkdir()
    (tmp_path / ".bakar" / "pins").symlink_to(elsewhere)
    core = _paired_core(tmp_path)
    assert core.side_path is not None
    ensure_core_bitbake_link(core)
    link = core.side_path.parent / "bitbake"
    assert os.path.realpath(link) == os.path.realpath(tmp_path / "bitbake")
    # A later build accepts the link it finds instead of refusing forever.
    ensure_core_bitbake_link(core)


def test_ensure_core_bitbake_link_refuses_a_link_that_resolves_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = _paired_core(tmp_path)
    assert core.side_path is not None
    link = core.side_path.parent / "bitbake"
    real_symlink = os.symlink
    # Something renames a directory between computing the target and creating the link.
    monkeypatch.setattr(
        "bakar.steps.repo_reuse.os.symlink", lambda _target, path: real_symlink(str(tmp_path / "nowhere"), path)
    )
    with pytest.raises(RepoReuseError, match=r"nowhere, not .*bakar removed it"):
        ensure_core_bitbake_link(core)
    assert not os.path.lexists(link)


def test_refusal_with_extra_overlays_does_not_offer_sync() -> None:
    syncable = _blocker("meta-a", head="f" * 40, clean=True)
    text = render_refusal([syncable], repair="bakar sync m.yml -w /ws", extra_overlays=["extra.yml"])
    assert "bakar sync m.yml" not in text
    assert "extra.yml" in text
    assert "by hand" in text


def test_refusal_offers_sync_when_extra_overlays_keep_the_main_pins() -> None:
    syncable = _blocker("meta-a", head="f" * 40, clean=True)
    text = render_refusal(
        [syncable], repair="bakar sync m.yml -w /ws", extra_overlays=["extra.yml"], main_pins={"meta-a": PIN}
    )
    assert "Move the clean clones to their pins with: bakar sync m.yml -w /ws" in text
    assert "extra.yml" not in text


def test_refusal_names_the_pin_an_extra_overlay_sets() -> None:
    syncable = _blocker("meta-a", head="f" * 40, clean=True)
    text = render_refusal(
        [syncable], repair="bakar sync m.yml -w /ws", extra_overlays=["extra.yml"], main_pins={"meta-a": "e" * 40}
    )
    assert "bakar sync m.yml" not in text
    assert f"extra.yml, which bakar sync does not read, and they pin meta-a at {PIN[:12]}" in text


def test_paired_link_resolves_like_oe_buildenv(tmp_path: Path) -> None:
    core = _paired_core(tmp_path)
    assert core.side_path is not None
    ensure_core_bitbake_link(core)
    assert os.path.realpath(core.side_path / ".." / "bitbake") == os.path.realpath(tmp_path / "bitbake")


def _blocker(name: str, *, head: str | None, clean: bool | None, kind: str = "side") -> RepoState:
    return RepoState(name, Path("/ws") / name, PIN, kind, head=head, clean=clean, blocking="because")  # type: ignore[arg-type]


def test_refusal_offers_sync_only_for_blockers_it_can_move() -> None:
    off_pin_clean = _blocker("bitbake", head="a" * 40, clean=True)
    dirty = _blocker("openembedded-core", head="a" * 40, clean=False)
    no_pin = _blocker("meta-x", head=None, clean=None, kind="invalid")
    at_pin_dirty = _blocker("meta-y", head=PIN, clean=False)

    mixed = render_refusal([off_pin_clean, dirty, no_pin, at_pin_dirty], repair="bakar sync m.yml")
    only_by_hand = render_refusal([dirty, no_pin, at_pin_dirty], repair="bakar sync m.yml")

    assert "Move the clean clones to their pins with: bakar sync m.yml" in mixed
    assert "fix them by hand: openembedded-core, meta-x, meta-y" in mixed
    assert "bakar sync m.yml" not in only_by_hand
    assert "fix them by hand: openembedded-core, meta-x, meta-y" in only_by_hand


def _blocker_pinned(name: str, pin: str) -> RepoState:
    return RepoState(name, Path("/ws") / name, pin, "side", head="b" * 40, clean=True, blocking="because")


def test_short_pin_gets_by_hand_line_not_sync() -> None:
    text = render_refusal([_blocker_pinned("meta-x", "abcdef123456")], repair="bakar sync m.yml")
    assert "bakar sync m.yml" not in text
    assert "fix them by hand: meta-x" in text


def test_sha256_pin_is_not_invalid(tmp_path: Path) -> None:
    pin = "c" * 64
    entries = pinned_entries({"meta-x": _entry(pin)}, tmp_path)
    at_pin = RepoFacts(
        exists=True, head=pin, clean=True, pin_present=True, side_exists=False, side_head=None, side_clean=None
    )
    off_pin = RepoFacts(
        exists=True, head="d" * 64, clean=True, pin_present=True, side_exists=False, side_head=None, side_clean=None
    )
    assert classify_facts(entries, [at_pin])[0].state == "frozen"
    (side,) = classify_facts(entries, [off_pin])
    assert side.state == "side"
    assert side.blocking is None


def test_blocker_line_names_the_clone_path() -> None:
    (line,) = render_blockers([_blocker("meta-x", head="a" * 40, clean=True)])
    assert line.startswith("  meta-x (/ws/meta-x): because; HEAD aaaaaaaaaaaa")


def test_control_characters_in_names_are_replaced() -> None:
    evil = "meta-\x1b[2Kx"
    blocked = RepoState(evil, Path("/ws") / evil, PIN, "side", head="a" * 40, clean=True, blocking="because")
    side = RepoState("ok\x7f", Path("/ws/ok"), PIN, "side", Path("/ws/side\x1bx"), head="a" * 40, clean=False)
    text = render_refusal([blocked, side], repair="bakar sync m\x1b.yml")
    table = "\n".join(render_pin_table([blocked, side]))
    for out in (text, table):
        assert "\x1b" not in out
        assert "\x7f" not in out
    assert "meta-?[2Kx (/ws/meta-?[2Kx)" in text
    assert "Using side checkouts: ok?" in text
    assert "with: bakar sync m?.yml" in text
    assert table.startswith("meta-?[2Kx: side")
