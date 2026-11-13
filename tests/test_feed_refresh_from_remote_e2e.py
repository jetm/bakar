"""``bakar feed refresh --from HOST --run ID`` end to end through the CLI.

The host is faked at the two seams a real one crosses. The rsync pull is real:
a PATH-stub ``ssh`` drops the ssh options and the host argument and runs the
remote command locally, so the copy, the staged-tree checks and the second
describe all run for real. ``bakar feed describe`` on the host is answered by
the real ``describe_build`` against a fake build tree, behind the patched
``_ssh_bash_capture``. Local kas pins, the meta-avocado checkout and the
publishing tail's external steps (``feed.sync``, ``createrepo``, layer hashes)
are stubbed where ``run_refresh_remote`` and the pipeline look them up.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import os
import shlex
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from bakar import feed, feed_delta, feed_describe, layers
from bakar.commands import _feed_describe as describe
from bakar.commands import _feed_refresh as refresh
from bakar.commands import _feed_refresh_remote as remote_mod
from bakar.commands import app
from bakar.feed_describe import DescribedFile
from bakar.steps import remote_dispatch, remote_pull
from tests.test_feed_describe import RUN_ID, Host, git_repo

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

pytestmark = pytest.mark.unit

HOST = "pc2"
RELEASE = "2026"
CHANNEL = "edge"
RPM = "rpm/core2_64/htop-1.0-r0.1.core2_64.rpm"
OTHER_COMMIT = "d" * 40
TAIL = "the previous delta is untouched"

type Mutate = Callable[[feed_describe.BuildDescription], feed_describe.BuildDescription]


class FakeHost:
    """The fake build host plus everything the CLI run is observed through."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp
        self.build = Host(tmp)
        # A second recipe the run built but nobody asked for.
        self.build.world.recipe("nano")
        ws = self.build.world.workspace
        self.pin = git_repo(ws / "meta-x")
        self.build.layer_conf(ws / "meta-x")
        self.declared: dict[str, object] = {"meta-x": {"commit": self.pin}}
        self.ssh_log = tmp / "ssh-calls.log"
        self.describe_calls: list[list[str]] = []
        self.mutate: Mutate | None = None
        self.kas = tmp / "machine.yml"
        self.kas.write_text("header: {}\n")
        self.project = tmp / "proj"
        self.project.mkdir()
        (self.project / "avocado.yaml").write_text("project: demo\n")
        # The CLI derives the delta and its staging area as siblings of the feed root.
        self.feed_root = tmp / "feed"
        self.delta_root = tmp / "feed-delta"
        self.build.world.cfg.effective_feed_dir = self.feed_root  # type: ignore[attr-defined]
        self._stub_local_side(monkeypatch)
        self._stub_ssh(monkeypatch)
        self._stub_describe(monkeypatch)

    def _stub_local_side(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tmp = self.tmp
        monkeypatch.setattr(refresh.feed_ops, "scripts_dir", lambda _y: tmp / "scripts")
        monkeypatch.setattr("bakar.commands.feed._resolve_refresh_cfg", lambda _w, _k: self.build.world.cfg)
        monkeypatch.setattr("bakar.commands.feed._feed_refresh_output.preflight_blocks", lambda **_k: False)
        monkeypatch.setattr("bakar.commands.feed._feed_refresh_output.check_port", lambda _o: None)
        monkeypatch.setattr(remote_mod, "preflight_or_report", lambda _host, *, assume_yes: True)
        monkeypatch.setattr(remote_mod, "meta_avocado_checkout", lambda _y: tmp / "checkout")
        monkeypatch.setattr(remote_mod.kas_pins, "probe_pins", lambda _y, _c: (RELEASE, self.declared))
        monkeypatch.setattr(remote_mod, "run_git", lambda argv, **_k: None)
        monkeypatch.setattr(
            layers, "collect_layer_hashes", lambda _cfg: [layers.LayerHash(repo="m", short_hash="abc", branch="")]
        )

        def fake_sync(
            _cfg: object, *, publish: feed.FeedPublish, feed_root: Path, stage_root: Path, **_kw: object
        ) -> dict:
            chan = feed.channel_root(feed_root, release=publish.release, channel=publish.channel)
            staged = feed.channel_root(stage_root, release=publish.release, channel=publish.channel)
            for rpm in publish.deploy_dir.rglob("*.rpm"):
                dest = staged / "target" / "qemux86-64" / rpm.parent.name / rpm.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(rpm, dest)
            (chan / "target" / "qemux86-64" / "repodata").mkdir(parents=True, exist_ok=True)
            count = len(list(publish.deploy_dir.rglob("*.rpm")))
            (chan / "target" / "qemux86-64" / "repodata" / "pooled-primary.xml.gz").write_bytes(
                gzip.compress(f'<metadata packages="{count}"></metadata>'.encode())
            )
            return {"snapshot": None, "channel_root": chan, "repos": ["target/qemux86-64"]}

        def fake_createrepo(repo: Path) -> None:
            rpms = sorted(repo.rglob("*.rpm"))
            pkgs = "".join(f'<package type="rpm"><location href="{p.relative_to(repo)}"/></package>' for p in rpms)
            (repo / "repodata").mkdir(exist_ok=True)
            (repo / "repodata" / "primary.xml").write_text(f'<metadata packages="{len(rpms)}">{pkgs}</metadata>')

        monkeypatch.setattr(refresh.feed, "sync", fake_sync)
        monkeypatch.setattr(refresh, "_createrepo", fake_createrepo)

    def _stub_ssh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An ``ssh`` on PATH that logs the call, drops its options and host, and runs the rest locally."""
        if shutil.which("rsync") is None:
            pytest.skip("rsync not installed")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        ssh = bin_dir / "ssh"
        ssh.write_text(
            f'#!/bin/sh\nwhile [ "$1" = "-o" ]; do shift 2; done\nshift\necho ran >> {shlex.quote(str(self.ssh_log))}\n'
            'exec "$@"\n'
        )
        ssh.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def _stub_describe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_capture(
            host: str, script: str, *, batch_mode: bool = True, timeout: float = 0.0
        ) -> subprocess.CompletedProcess[str]:
            argv = shlex.split(script)
            assert argv[:3] == ["bakar", "feed", "describe"], argv
            assert host == HOST
            self.describe_calls.append(argv)
            recipes = tuple(argv[i + 1] for i, a in enumerate(argv) if a == "--recipe")
            deps = dataclasses.replace(self.build.deps(), lock_probe=self.build.probe)
            desc = describe.describe_build(argv[3], recipes, deps=deps)
            if self.mutate is not None:
                desc = self.mutate(desc)
            return subprocess.CompletedProcess(argv, 0, stdout=feed_describe.encode(desc) + "\n", stderr="")

        monkeypatch.setattr(remote_dispatch, "_ssh_bash_capture", fake_capture)

    @property
    def ssh_runs(self) -> int:
        return len(self.ssh_log.read_text().splitlines()) if self.ssh_log.exists() else 0

    def refresh(self, *extra: str, recipe: str = "htop") -> Any:
        args = [
            *("feed", "refresh", str(self.kas), "--recipe", recipe, "--project", str(self.project)),
            *("--release", RELEASE, "--channel", CHANNEL),
            *("--from", HOST, "--run", RUN_ID, *extra),
        ]
        return CliRunner().invoke(app, args)

    def channel_dir(self) -> Path:
        return feed.channel_root(self.delta_root, release=RELEASE, channel=CHANNEL)

    def delta_state(self) -> dict[str, tuple[bytes, int]]:
        """Every file under the delta with its bytes and mtime: byte-identical means this compares equal."""
        root = self.delta_root
        return {
            p.relative_to(root).as_posix(): (p.read_bytes(), p.lstat().st_mtime_ns)
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }

    def staging_dirs(self) -> list[Path]:
        return sorted(self.tmp.glob("pull-*"))

    def project_state(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in sorted(self.project.rglob("*")) if p.is_file()}


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    return FakeHost(tmp_path, monkeypatch)


def _words(text: str) -> str:
    """Console output with rich's line wrapping undone."""
    return " ".join(text.split())


def _publish_first(fake: FakeHost) -> dict[str, tuple[bytes, int]]:
    """Publish a good delta so a later refused run has something to leave alone."""
    result = fake.refresh()
    assert result.exit_code == 0, result.output
    state = fake.delta_state()
    assert state
    return state


def _assert_refused_cleanly(
    fake: FakeHost, result: Any, previous: dict[str, tuple[bytes, int]], *needles: str, tail: bool = True
) -> None:
    out = _words(result.output)
    assert result.exit_code == 1, result.output
    assert "feed refresh failed:" in out
    for needle in needles:
        assert needle in out, (needle, out)
    if tail:
        assert TAIL in out
    assert fake.delta_state() == previous
    assert fake.staging_dirs() == []


def test_finished_run_publishes_only_the_named_recipes(fake: FakeHost) -> None:
    before_project = fake.project_state()

    result = fake.refresh()

    assert result.exit_code == 0, result.output
    chan = fake.channel_dir()
    # The host also built nano; only htop was asked for, so only htop travels.
    assert sorted(p.name for p in chan.rglob("*.rpm")) == ["htop-1.0-r0.1.core2_64.rpm"]
    assert not list(chan.rglob("nano*"))
    recipe_map = json.loads((chan / feed_delta.RECIPE_MAP_NAME).read_text())
    (entry,) = recipe_map["entries"]
    assert entry["recipe"] == "htop"
    assert entry["packages"] == ["htop"]
    assert entry["layer"] == "meta-oe"
    assert entry["pv"] == "1.0"
    assert entry["machine"] == "qemux86-64"
    assert entry["release"] == RELEASE
    delta = json.loads((chan / feed_delta.DELTA_NAME).read_text())
    assert delta["built_recipes"] == ["htop"]
    assert delta["bakar_run_id"] == RUN_ID
    source = delta["source"]
    assert set(source) == {"host", "run_id", "pulled_at", "observed_layers", "pin_override"}
    assert source["host"] == HOST
    assert source["run_id"] == RUN_ID
    assert source["pin_override"] is False
    assert source["observed_layers"] == {"meta-x": fake.pin, "meta-oe": None}
    # The overlay stanzas and the three avocado commands are on stdout; no project file moved.
    assert result.stdout == (
        "repos:\n"
        "  bakar-delta-target-qemux86-64:\n"
        "    path: ../feed-delta/2026/edge/target/qemux86-64\n"
        "    stages: [rootfs, runtime, ext, initramfs]\n"
        "avocado unlock\n"
        "avocado install\n"
        "avocado build\n"
    )
    assert fake.project_state() == before_project
    assert fake.staging_dirs() == []
    assert fake.ssh_runs == 1


@pytest.mark.parametrize(
    ("lock_state", "terminal", "signal"),
    [
        (("held", None), None, "a build holds"),
        (("free", None), {}, "is in state running"),
    ],
    ids=["lock-held", "run-still-running"],
)
def test_live_build_is_refused_naming_the_signal_and_copies_nothing(
    fake: FakeHost, lock_state: tuple[str, None], terminal: dict[str, object] | None, signal: str
) -> None:
    previous = _publish_first(fake)
    runs_before = fake.ssh_runs
    fake.build.probe_result = lock_state
    if terminal is not None:
        fake.build.write_run(terminal=terminal)

    result = fake.refresh()

    _assert_refused_cleanly(fake, result, previous, signal, RUN_ID)
    if lock_state[0] == "held":
        assert "still live" in _words(result.output)
        assert fake.build.lock_file.name in _words(result.output)
    assert fake.ssh_runs == runs_before


def test_planted_symlink_in_place_of_a_described_rpm_is_refused(fake: FakeHost, tmp_path: Path) -> None:
    previous = _publish_first(fake)
    host_rpm = fake.build.deploy_root / RPM
    outside = tmp_path / "elsewhere.rpm"
    shutil.copy2(host_rpm, outside)
    host_rpm.unlink()
    host_rpm.symlink_to(outside)

    result = fake.refresh()

    _assert_refused_cleanly(fake, result, previous, "htop-1.0-r0.1.core2_64.rpm")
    assert outside.read_bytes() == b"rpm"


def test_description_entry_escaping_the_deploy_root_is_refused_before_any_copy(fake: FakeHost) -> None:
    previous = _publish_first(fake)
    runs_before = fake.ssh_runs
    hostile = "rpm/../../etc/passwd.rpm"

    def inject(desc: feed_describe.BuildDescription) -> feed_describe.BuildDescription:
        return dataclasses.replace(desc, files=(*desc.files, DescribedFile(hostile, 3, "0" * 64)))

    fake.mutate = inject

    result = fake.refresh()

    _assert_refused_cleanly(fake, result, previous, hostile)
    assert fake.ssh_runs == runs_before


def test_oversized_described_file_is_refused_before_the_copy(fake: FakeHost) -> None:
    previous = _publish_first(fake)
    runs_before = fake.ssh_runs
    huge = remote_pull.MAX_FILE_BYTES + 1

    def inflate(desc: feed_describe.BuildDescription) -> feed_describe.BuildDescription:
        files = tuple(dataclasses.replace(f, size=huge) if f.path == RPM else f for f in desc.files)
        return dataclasses.replace(desc, files=files)

    fake.mutate = inflate

    result = fake.refresh()

    _assert_refused_cleanly(fake, result, previous, RPM, f"{huge} bytes", "per-file cap")
    assert fake.ssh_runs == runs_before


def test_rsync_size_cap_backstops_a_description_that_understates_a_file(
    fake: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    # validate_description binds its default cap at import, so patching the module
    # constant lowers only rsync's --max-size: the copy skips the file and the staged
    # tree check refuses the gap.
    previous = _publish_first(fake)
    monkeypatch.setattr(remote_pull, "MAX_FILE_BYTES", 1)

    result = fake.refresh()

    _assert_refused_cleanly(fake, result, previous, "described file", "is missing from staging")


def test_pin_mismatch_is_refused_then_published_with_the_override(fake: FakeHost) -> None:
    fake.declared = {"meta-x": {"commit": OTHER_COMMIT}}

    refused = fake.refresh()

    out = _words(refused.output)
    assert refused.exit_code == 1, refused.output
    assert f"meta-x declared {OTHER_COMMIT} but the host built {fake.pin}" in out
    assert "--allow-pin-mismatch" in out
    assert not fake.delta_root.exists() or not list(fake.delta_root.rglob("*.rpm"))
    assert fake.staging_dirs() == []
    assert fake.ssh_runs == 0

    allowed = fake.refresh("--allow-pin-mismatch")

    assert allowed.exit_code == 0, allowed.output
    delta = json.loads((fake.channel_dir() / feed_delta.DELTA_NAME).read_text())
    assert delta["source"]["pin_override"] is True
    assert delta["source"]["observed_layers"] == {"meta-x": fake.pin, "meta-oe": None}
    assert [p.name for p in fake.channel_dir().rglob("*.rpm")] == ["htop-1.0-r0.1.core2_64.rpm"]


def test_pin_for_a_repository_the_host_did_not_report_says_there_was_no_evidence(fake: FakeHost) -> None:
    previous = _publish_first(fake)
    runs_before = fake.ssh_runs
    fake.declared = {**fake.declared, "meta-y": {"commit": OTHER_COMMIT}}

    result = fake.refresh()

    _assert_refused_cleanly(fake, result, previous, f"meta-y declared {OTHER_COMMIT} but the host reported no evidence")
    assert fake.ssh_runs == runs_before


def test_second_refresh_is_refused_on_the_lock_before_the_host_is_contacted(fake: FakeHost) -> None:
    with refresh.refresh_lock(fake.feed_root):
        result = fake.refresh()

    out = _words(result.output)
    assert result.exit_code == 1, result.output
    assert "feed refresh failed: another refresh holds" in out
    # rich may wrap the long path mid-word, so compare with all whitespace removed.
    assert str(refresh.lock_path(fake.feed_root)) in "".join(out.split())
    assert fake.ssh_runs == 0
    assert fake.describe_calls == []
    assert not fake.delta_root.exists()
    assert fake.staging_dirs() == []
