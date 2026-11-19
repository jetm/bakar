"""Doctor row dl-mirror-links: restores mirror links through apply_links."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bakar import diagnostics
from bakar.diagnostics import Severity, Status, check_dl_mirror_links
from bakar.steps import dl_link, hub_mount
from tests.conftest import make_build_config

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DL_DIR", raising=False)
    monkeypatch.setattr(hub_mount, "_PROBE_MEMO", {})


def _cfg(dl_dir: Path | None, mirrors: list[Path] | None, **kw: object):
    paths = " ".join(str(m) for m in mirrors) if mirrors else None
    kw.setdefault("host_mode", True)
    return make_build_config(
        workspace=Path("/nonexistent-ws"),
        dl_dir=str(dl_dir) if dl_dir else None,
        dl_mirror_paths=paths,
        **kw,
    )


def _mirror(root: Path, name: str, files: list[str], *, stamped: bool = True) -> Path:
    d = root / name
    d.mkdir()
    for f in files:
        (d / f).write_bytes(b"x")
        if stamped:
            (d / (f + ".done")).write_bytes(b"")
    return d


def _dl(root: Path) -> Path:
    d = root / "dl"
    d.mkdir()
    return d


def _entries(d: Path) -> set[str]:
    return {e.name for e in d.iterdir()}


def _ready(monkeypatch: pytest.MonkeyPatch, states: dict[str, str] | None = None) -> list[list[Path]]:
    calls: list[list[Path]] = []
    state_by_dir: dict[str, str] = states or {}

    def fake(dirs):
        calls.append(list(dirs))
        return [hub_mount.DirStatus(path=d, state=state_by_dir.get(str(d), "ready"), detail="") for d in dirs]

    monkeypatch.setattr(hub_mount, "probe_dirs", fake)
    return calls


def test_missing_files_are_linked_and_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(monkeypatch)
    names = [f"f{i:02d}.tar.gz" for i in range(25)]
    m = _mirror(tmp_path, "m", names)
    dl = _dl(tmp_path)
    r = check_dl_mirror_links(_cfg(dl, [m]))
    assert r.status == Status.PASS
    assert r.severity == Severity.INFO
    assert "25" in r.message
    for n in names[:10]:
        assert n in r.message
    assert names[10] not in r.message
    assert _entries(dl) == set(names)
    assert all((dl / n).is_symlink() and os.readlink(dl / n) == str(m / n) for n in names)


def test_rerun_passes_and_creates_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(monkeypatch)
    m = _mirror(tmp_path, "m", ["a.tar.gz", "b.tar.gz"])
    dl = _dl(tmp_path)
    check_dl_mirror_links(_cfg(dl, [m]))
    before = _entries(dl)
    r = check_dl_mirror_links(_cfg(dl, [m]))
    assert r.status == Status.PASS
    assert _entries(dl) == before


def test_git_tarballs_and_unstamped_create_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(monkeypatch)
    m = _mirror(tmp_path, "m", ["git2_x.tar.gz", "gitshallow_y.tar.gz"])
    (m / "nostamp.tar.gz").write_bytes(b"x")
    dl = _dl(tmp_path)
    r = check_dl_mirror_links(_cfg(dl, [m]))
    assert r.status == Status.PASS
    assert _entries(dl) == set()


def test_same_set_as_verb_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(monkeypatch)
    names = ["a.tar.gz", "b.zip", "git2_c.tar.gz"]
    m = _mirror(tmp_path, "m", names)
    (m / "nostamp.tar.gz").write_bytes(b"x")
    dl_doctor = _dl(tmp_path)
    dl_verb = tmp_path / "dl2"
    dl_verb.mkdir()
    check_dl_mirror_links(_cfg(dl_doctor, [m]))
    dl_link.apply_links(dl_link.plan_links(dl_verb, [m]), dl_verb)
    assert _entries(dl_doctor) == _entries(dl_verb) == {"a.tar.gz", "b.zip"}


def test_existing_entries_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(monkeypatch)
    m = _mirror(tmp_path, "m", ["reg", "dir", "dang", "new"])
    dl = _dl(tmp_path)
    (dl / "reg").write_bytes(b"mine")
    (dl / "dir").mkdir()
    (dl / "dang").symlink_to(tmp_path / "gone")
    r = check_dl_mirror_links(_cfg(dl, [m]))
    assert r.status == Status.PASS
    assert (dl / "reg").read_bytes() == b"mine"
    assert (dl / "dir").is_dir()
    assert os.readlink(dl / "dang") == str(tmp_path / "gone")
    assert (dl / "new").is_symlink()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_non_writable_dl_dir_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(monkeypatch)
    m = _mirror(tmp_path, "m", ["a", "b", "c"])
    dl = _dl(tmp_path)
    dl.chmod(0o555)
    try:
        r = check_dl_mirror_links(_cfg(dl, [m]))
    finally:
        dl.chmod(0o755)
    assert r.status == Status.FAIL
    assert r.severity == Severity.WARN
    assert "3 link(s)" in r.message
    assert "Permission denied" in r.message
    assert "bakar cache link-mirror" in r.message
    assert _entries(dl) == set()


def test_key_unset_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _ready(monkeypatch)
    dl = _dl(tmp_path)
    r = check_dl_mirror_links(_cfg(dl, None))
    assert r.status == Status.SKIP
    assert r.severity == Severity.INFO
    assert calls == []
    assert _entries(dl) == set()


def test_dl_dir_unset_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _ready(monkeypatch)
    m = _mirror(tmp_path, "m", ["a"])
    r = check_dl_mirror_links(_cfg(None, [m]))
    assert r.status == Status.SKIP
    assert r.severity == Severity.INFO
    assert calls == []


def test_env_dl_dir_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(monkeypatch)
    m = _mirror(tmp_path, "m", ["a"])
    cfg_dl = _dl(tmp_path)
    env_dl = tmp_path / "envdl"
    env_dl.mkdir()
    monkeypatch.setenv("DL_DIR", str(env_dl))
    check_dl_mirror_links(_cfg(cfg_dl, [m]))
    assert _entries(env_dl) == {"a"}
    assert _entries(cfg_dl) == set()


def test_container_build_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _ready(monkeypatch)
    m = _mirror(tmp_path, "m", ["a"])
    dl = _dl(tmp_path)
    r = check_dl_mirror_links(_cfg(dl, [m], host_mode=False))
    assert r.status == Status.SKIP
    assert r.severity == Severity.INFO
    assert "not used" in r.message
    assert calls == []
    assert _entries(dl) == set()


def test_no_ready_directory_skips_naming_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = _mirror(tmp_path, "m", ["a"])
    _ready(monkeypatch, {str(m): "missing"})
    dl = _dl(tmp_path)
    r = check_dl_mirror_links(_cfg(dl, [m]))
    assert r.status == Status.SKIP
    assert r.severity == Severity.INFO
    assert "dl-mirror-paths-ready" in r.message
    assert _entries(dl) == set()


def test_unlisted_directory_noted_and_listed_one_linked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(monkeypatch)
    good = _mirror(tmp_path, "good", ["a"])
    bad = _mirror(tmp_path, "bad", ["z"])
    real = dl_link.plan_links

    def lister(dirs, deadline_s):
        return {d: (["a", "a.done"] if d == good else None) for d in dirs}

    def wrapped(dl_dir, mirror_dirs, **kw):
        return real(dl_dir, mirror_dirs, lister=lister, deadline_s=kw["deadline_s"])

    monkeypatch.setattr(dl_link, "plan_links", wrapped)
    dl = _dl(tmp_path)
    r = check_dl_mirror_links(_cfg(dl, [good, bad]))
    assert r.severity == Severity.INFO
    assert r.status == Status.PASS
    assert "not listed" in r.message
    assert str(bad) in r.message
    assert _entries(dl) == {"a"}


def test_timeout_listing_never_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(monkeypatch)
    m = _mirror(tmp_path, "m", ["a"])
    real = dl_link.plan_links
    monkeypatch.setattr(
        dl_link,
        "plan_links",
        lambda d, ms, **kw: real(d, ms, lister=lambda dirs, deadline_s: dict.fromkeys(dirs), **kw),
    )
    dl = _dl(tmp_path)
    r = check_dl_mirror_links(_cfg(dl, [m]))
    assert r.status != Status.FAIL
    assert r.severity == Severity.INFO
    assert _entries(dl) == set()


def test_unreadable_dl_dir_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(monkeypatch)
    m = _mirror(tmp_path, "m", ["a"])
    r = check_dl_mirror_links(_cfg(tmp_path / "absent", [m]))
    assert r.status == Status.SKIP
    assert r.severity == Severity.INFO


def test_row_registered_at_warn() -> None:
    name = "dl-mirror-links"
    assert name in {n for _g, ns in diagnostics.CHECK_GROUPS for n in ns}
    assert (check_dl_mirror_links, name, Severity.WARN) in diagnostics._CHECK_METADATA
    assert check_dl_mirror_links in diagnostics.SHARED_CHECKS
    assert check_dl_mirror_links in diagnostics._CACHE_TOUCHING_CHECKS


def test_never_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = _mirror(tmp_path, "m", ["a"])
    dl = _dl(tmp_path)
    cases: list[Callable[[], object]] = [
        lambda: _cfg(dl, None),
        lambda: _cfg(None, [m]),
        lambda: _cfg(dl, [m], host_mode=False),
        lambda: _cfg(tmp_path / "absent", [m]),
        lambda: _cfg(dl, [m]),
    ]
    for states in ({}, {str(m): "missing"}, {str(m): "unresponsive"}):
        _ready(monkeypatch, states)
        for make in cases:
            assert check_dl_mirror_links(make()).severity != Severity.BLOCK  # type: ignore[arg-type]


def test_message_names_only_the_files_actually_linked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(monkeypatch)
    m = _mirror(tmp_path, "m", ["a.tgz", "b.tgz"])
    dl = _dl(tmp_path)
    real_apply = dl_link.apply_links

    def racing_apply(plan: dl_link.LinkPlan, dl_dir: Path) -> dl_link.ApplyResult:
        (dl_dir / "a.tgz").write_bytes(b"bitbake")
        return real_apply(plan, dl_dir)

    monkeypatch.setattr(dl_link, "apply_links", racing_apply)
    r = check_dl_mirror_links(_cfg(dl, [m]))
    assert r.status == Status.PASS
    assert "linked 1 file(s)" in r.message
    assert "b.tgz" in r.message
    assert "a.tgz" not in r.message


def test_nothing_listable_skips_instead_of_passing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(monkeypatch)
    m = _mirror(tmp_path, "m", ["a"])
    real = dl_link.plan_links
    monkeypatch.setattr(
        dl_link,
        "plan_links",
        lambda d, ms, **kw: real(d, ms, lister=lambda dirs, deadline_s: dict.fromkeys(dirs), **kw),
    )
    r = check_dl_mirror_links(_cfg(_dl(tmp_path), [m]))
    assert r.status == Status.SKIP
    assert "could be listed" in r.message


def test_linked_names_are_neutralized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(monkeypatch)
    bidi, csi = chr(0x202E), chr(0x9B)
    m = _mirror(tmp_path, "m", [f"a{bidi}b{csi}c"])
    r = check_dl_mirror_links(_cfg(_dl(tmp_path), [m]))
    assert r.status == Status.PASS
    assert bidi not in r.message
    assert csi not in r.message


def test_dry_run_gate_leaves_the_link_check_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    from bakar.commands import _helpers

    seen: list[object] = []

    def fake_run_all(cfg, bsp, **kw):
        seen.append(kw.get("exclude"))
        return []

    monkeypatch.setattr(_helpers, "run_all", fake_run_all)
    monkeypatch.setattr(_helpers, "_print_diagnosis", lambda *_a, **_k: None)
    log = types.SimpleNamespace(
        run_dir=tmp_path,
        step_start=lambda *_a, **_k: None,
        step_ok=lambda *_a, **_k: None,
        check_start=lambda *_a, **_k: None,
        check_end=lambda *_a, **_k: None,
    )
    _helpers._run_doctor_gate(_cfg(None, None), log, None, dry_run=True)
    _helpers._run_doctor_gate(_cfg(None, None), log, None)
    assert seen == [{"dl-mirror-links"}, None]


def test_only_empty_directory_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(monkeypatch)
    m = _mirror(tmp_path, "m", [])
    dl = _dl(tmp_path)
    r = check_dl_mirror_links(_cfg(dl, [m]))
    assert r.status == Status.FAIL
    assert r.severity == Severity.WARN
    assert r.severity != Severity.BLOCK
    assert str(m) in r.message
    assert "not mounted" in r.message
    assert _entries(dl) == set()


def test_empty_directory_named_when_another_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(monkeypatch)
    empty = _mirror(tmp_path, "empty", [])
    full = _mirror(tmp_path, "full", ["a", "b", "c"])
    dl = _dl(tmp_path)
    r = check_dl_mirror_links(_cfg(dl, [empty, full]))
    assert r.status == Status.PASS
    assert r.severity == Severity.INFO
    assert "linked 3 file(s)" in r.message
    assert str(empty) in r.message
    assert _entries(dl) == {"a", "b", "c"}


def test_empty_plus_unlisted_warns_naming_each_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(monkeypatch)
    empty = _mirror(tmp_path, "empty", [])
    bad = _mirror(tmp_path, "bad", ["z"])
    real = dl_link.plan_links

    def lister(dirs, deadline_s):
        return {d: ([] if d == empty else None) for d in dirs}

    def wrapped(dl_dir, mirror_dirs, **kw):
        return real(dl_dir, mirror_dirs, lister=lister, deadline_s=kw["deadline_s"])

    monkeypatch.setattr(dl_link, "plan_links", wrapped)
    dl = _dl(tmp_path)
    r = check_dl_mirror_links(_cfg(dl, [empty, bad]))
    assert r.status == Status.FAIL
    assert r.severity == Severity.WARN
    assert r.message.count(str(empty)) == 1
    assert f"mirror not listed: {bad}" in r.message
    assert _entries(dl) == set()


def test_unstamped_only_mirror_names_no_empty_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(monkeypatch)
    m = _mirror(tmp_path, "m", [])
    (m / "nostamp.tar.gz").write_bytes(b"x")
    r = check_dl_mirror_links(_cfg(_dl(tmp_path), [m]))
    assert r.status == Status.PASS
    assert "nothing to link" in r.message
    assert "lists no files" not in r.message


def test_partly_empty_mirror_with_nothing_to_link_passes_and_names_the_empty_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(monkeypatch)
    empty = _mirror(tmp_path, "empty", [])
    full = _mirror(tmp_path, "full", ["a"])
    dl = _dl(tmp_path)
    (dl / "a").write_bytes(b"already here")
    r = check_dl_mirror_links(_cfg(dl, [empty, full]))
    assert r.status == Status.PASS
    assert r.severity == Severity.INFO
    assert "nothing to link" in r.message
    assert f"mirror lists no files: {empty}" in r.message
    assert _entries(dl) == {"a"}


def test_every_listed_directory_empty_warns_naming_each_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(monkeypatch)
    first = _mirror(tmp_path, "first", [])
    second = _mirror(tmp_path, "second", [])
    dl = _dl(tmp_path)
    r = check_dl_mirror_links(_cfg(dl, [first, second]))
    assert r.status == Status.FAIL
    assert r.severity == Severity.WARN
    assert r.message.count(str(first)) == 1
    assert r.message.count(str(second)) == 1
    assert _entries(dl) == set()
