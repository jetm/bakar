"""Tests for ``bakar.steps.avocado_dispatch`` with every ssh, git and subprocess call mocked."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bakar.release_workspace import sanitize_delivered_name
from bakar.steps import avocado_dispatch as ad
from bakar.steps import layer_delivery, remote_dispatch

pytestmark = pytest.mark.unit

HOST = "pc2"
COMMIT = "a" * 40
WORKSPACE = Path("/srv/ws/scarthgap")


class Recorder:
    """Install mocks for every external call and record them in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.preflight: tuple[bool, str | None] = (True, "bakar 1.0")
        self.local_version: str | None = "bakar 1.0"
        self.workspace_error: str | None = None
        self.snapshot = False
        self.push_rc = 0
        self.ensure_rc = 0
        self.capability_rc = 0
        self.capability_stderr = ""
        self.untracked: list[str] = []
        self.stream: tuple[int, list[str], bool] | type[BaseException] = (0, ["ok\n"], True)
        self.ensure_script = ""
        self.capability_script = ""
        self.remote_script = ""

        def preflight(host: str) -> tuple[bool, str | None]:
            self.calls.append(("preflight", host))
            return self.preflight

        def local_version() -> str | None:
            self.calls.append(("local_version", None))
            return self.local_version

        def check_ws(host: str, workspace: str, release: str) -> None:
            self.calls.append(("check_workspace", (host, workspace, release)))
            if self.workspace_error:
                raise remote_dispatch.DispatchError(self.workspace_error)

        def name(checkout: Path, worktree: Any) -> str:
            self.calls.append(("delivered_name", (checkout, worktree)))
            return "feature-x"

        def snapshot(checkout: Path, *, cap_mb: int) -> tuple[str, bool]:
            self.calls.append(("snapshot", (checkout, cap_mb)))
            return COMMIT, self.snapshot

        def run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append(("push", argv))
            return subprocess.CompletedProcess(argv, self.push_rc)

        def untracked(checkout: Path) -> list[str]:
            self.calls.append(("untracked", checkout))
            return self.untracked

        def capture(host: str, script: str, **_kw: Any) -> subprocess.CompletedProcess[str]:
            if script.startswith("bakar settings get "):
                self.calls.append(("capability", host))
                self.capability_script = script
                return subprocess.CompletedProcess(["ssh"], self.capability_rc, "", self.capability_stderr)
            self.calls.append(("ensure", host))
            self.ensure_script = script
            return subprocess.CompletedProcess(["ssh"], self.ensure_rc, "", "boom")

        def stream(host: str, script: str) -> tuple[int, list[str], bool]:
            self.calls.append(("stream", host))
            self.remote_script = script
            if isinstance(self.stream, type):
                raise self.stream()
            return self.stream

        def surface(host: str, ws: Path, captured: list[str], rc: int) -> None:
            self.calls.append(("surface", (host, ws, rc)))

        def no_prompt(*_a: Any, **_kw: Any) -> bool:
            raise AssertionError("dispatch must not prompt")

        monkeypatch.setattr(remote_dispatch, "preflight_remote", preflight)
        monkeypatch.setattr(remote_dispatch, "_local_bakar_version", local_version)
        monkeypatch.setattr(remote_dispatch, "check_remote_workspace", check_ws)
        monkeypatch.setattr(layer_delivery, "delivered_name", name)
        monkeypatch.setattr(layer_delivery, "snapshot_commit", snapshot)
        monkeypatch.setattr(layer_delivery, "snapshot_untracked_paths", untracked, raising=False)
        monkeypatch.delenv("BAKAR_REPO_POLICY", raising=False)
        monkeypatch.setattr(ad.subprocess, "run", run)
        monkeypatch.setattr(remote_dispatch, "_ssh_bash_capture", capture)
        monkeypatch.setattr(remote_dispatch, "_stream_remote_build", stream)
        monkeypatch.setattr(remote_dispatch, "_surface_run_id", surface)
        monkeypatch.setattr(ad.socket, "gethostname", lambda: "pc1")
        monkeypatch.setattr("typer.confirm", no_prompt)
        monkeypatch.setattr("builtins.input", no_prompt)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    return Recorder(monkeypatch)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    co = tmp_path / "wt" / "feature-x"
    (co / "kas" / "machine").mkdir(parents=True)
    (co / "kas" / "machine" / "m.yml").write_text("header: {version: 14}\n")
    (co / "kas" / "extra.yml").write_text("header: {version: 14}\n")
    return co


def _dispatch(checkout: Path, kas_arg: str, local_args: list[str], **overrides: Any) -> int:
    kwargs: dict[str, Any] = {
        "workspace": WORKSPACE,
        "checkout": checkout,
        "worktree": None,
        "kas_yaml": checkout / "kas" / "machine" / "m.yml",
        "kas_arg": kas_arg,
        "local_args": local_args,
        "release": "scarthgap",
        "worktree_root": None,
        "build_dir_template": None,
        "cap_mb": 64,
        "invoking_cwd": checkout,
    }
    flags = {"sccache_dist": overrides.pop("sccache_dist", False), "assume_yes": overrides.pop("assume_yes", False)}
    kwargs.update(overrides)
    return ad.dispatch_avocado_build(HOST, ad.AvocadoDispatchRequest(**kwargs), remote_dispatch.DispatchFlags(**flags))


def _remote_argv_line(script: str) -> str:
    # The build guard wraps the build either as `flock -s <lock> bash -c ...`
    # or as `bash -c '<script>' <lock> ...`; the build line starts with either.
    return next(
        line
        for line in script.splitlines()
        if " bakar " in line and line.lstrip().startswith(("exec flock", "exec bash -c"))
    )


def test_call_order_and_success(rec: Recorder, checkout: Path) -> None:
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml", "--on", HOST])
    assert rc == 0
    assert rec.names() == [
        "delivered_name",
        "snapshot",
        "preflight",
        "local_version",
        "capability",
        "check_workspace",
        "push",
        "ensure",
        "stream",
        "surface",
    ]
    assert ("check_workspace", (HOST, str(WORKSPACE), "scarthgap")) in rec.calls
    assert ("surface", (HOST, WORKSPACE / "build-m-feature-x", 0)) in rec.calls


@pytest.mark.parametrize(
    "failure",
    ["preflight", "version", "capability", "workspace"],
)
def test_no_remote_write_when_a_check_fails(rec: Recorder, checkout: Path, failure: str) -> None:
    if failure == "preflight":
        rec.preflight = (False, "unreachable")
    elif failure == "version":
        rec.preflight = (True, "bakar 2.0")
    elif failure == "capability":
        rec.capability_rc = 2
    else:
        rec.workspace_error = "release mismatch"
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml", "--on", HOST])
    assert rc == 1
    assert "push" not in rec.names()
    assert "ensure" not in rec.names()
    assert "stream" not in rec.names()


def test_version_mismatch_proceeds_with_yes(rec: Recorder, checkout: Path) -> None:
    rec.preflight = (True, "bakar 2.0")
    rc = _dispatch(
        checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml", "--on", HOST, "--yes"], assume_yes=True
    )
    assert rc == 0
    assert "push" in rec.names()


@pytest.mark.parametrize(("remote_version", "assume_yes"), [("bakar 1.0", False), ("bakar 2.0", True)])
def test_old_remote_bakar_is_refused_even_with_yes(
    rec: Recorder, checkout: Path, capsys: pytest.CaptureFixture[str], remote_version: str, assume_yes: bool
) -> None:
    # A remote bakar from before release-workspace dispatch would run plain kas
    # and move the shared clones; --yes overrides only the version check.
    rec.preflight = (True, remote_version)
    rec.capability_rc = 2
    rec.capability_stderr = "Unknown key: dispatch.snapshot_untracked_cap_mb\n"
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"], assume_yes=assume_yes)
    err = " ".join(capsys.readouterr().err.split())
    assert rc == 1
    assert f"could not confirm that the bakar on {HOST} supports release-workspace dispatch" in err
    assert "(exit 2): Unknown key: dispatch.snapshot_untracked_cap_mb;" in err
    assert "if that bakar predates this feature, update it" in err
    assert "--yes does not override this" in err
    assert rec.capability_script.strip() == "bakar settings get dispatch.snapshot_untracked_cap_mb"
    assert not {"check_workspace", "push", "ensure", "stream"} & set(rec.names())


@pytest.mark.parametrize("assume_yes", [False, True])
def test_remote_config_error_is_shown_not_called_old(
    rec: Recorder, checkout: Path, capsys: pytest.CaptureFixture[str], assume_yes: bool
) -> None:
    # A current remote bakar with a broken config fails the probe too; its own
    # error is shown so the advice does not claim the bakar is outdated.
    rec.capability_rc = 1
    rec.capability_stderr = "".join(f"Invalid bakar config: line {n}\n" for n in range(1, 8))
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"], assume_yes=assume_yes)
    err = " ".join(capsys.readouterr().err.split())
    assert rc == 1
    assert f"could not confirm that the bakar on {HOST} supports release-workspace dispatch (exit 1)" in err
    assert "Invalid bakar config: line 1" in err
    assert "Invalid bakar config: line 5" in err
    assert "line 6" not in err
    assert "predates release-workspace dispatch" not in err
    assert "--yes does not override this" in err
    assert not {"check_workspace", "push", "ensure", "stream"} & set(rec.names())


def test_capability_probe_ssh_failure_is_reported_as_such(
    rec: Recorder, checkout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec.capability_rc = 255
    rec.capability_stderr = "Connection refused\n"
    assert _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"]) == 1
    err = capsys.readouterr().err
    assert f"could not probe the bakar on {HOST}" in err
    assert "Connection refused" in err
    assert "could not confirm" not in err
    assert "push" not in rec.names()


def test_repo_policy_typo_exits_2_before_any_ssh(
    rec: Recorder, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuze")
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    assert rc == 2
    assert "'reuze' is not a repository policy" in capsys.readouterr().err
    assert rec.calls == []


@pytest.mark.parametrize("policy", ["kas", "reuse", ""])
def test_valid_local_repo_policy_dispatches(
    rec: Recorder, checkout: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    monkeypatch.setenv("BAKAR_REPO_POLICY", policy)
    assert _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"]) == 0
    # The remote is always told to reuse, whatever the local policy is.
    assert "BAKAR_REPO_POLICY=reuse" in rec.remote_script


def test_relative_worktree_root_exits_2_before_any_ssh(
    rec: Recorder, checkout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"], worktree_root=Path("wt"))
    assert rc == 2
    assert "[dispatch] worktree_root" in capsys.readouterr().err
    assert rec.calls == []


def test_push_targets_remote_clone_and_ref(rec: Recorder, checkout: Path) -> None:
    _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    push = next(argv for name, argv in rec.calls if name == "push")
    assert push[:3] == ["git", "-C", str(checkout)]
    assert f"ssh://{HOST}{WORKSPACE}/meta-avocado" in push
    assert push[-1] == f"{COMMIT}:refs/bakar/feature-x/{COMMIT[:12]}"


def test_push_failure_stops_before_ensure(rec: Recorder, checkout: Path) -> None:
    rec.push_rc = 128
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    assert rc == 128
    assert "ensure" not in rec.names()
    assert "stream" not in rec.names()


def test_push_failure_from_shallow_checkout_names_the_host_fetch(
    rec: Recorder, checkout: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    rec.push_rc = 1
    monkeypatch.setattr("bakar.steps.layer_delivery.is_shallow", lambda _checkout: True)
    _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    out = capsys.readouterr().err
    assert "shallow" in out
    assert "git -C" in out
    assert "fetch" in out


@pytest.mark.parametrize("snapshot", [False, True])
def test_marker_inputs(rec: Recorder, checkout: Path, snapshot: bool) -> None:
    rec.snapshot = snapshot
    _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    script = rec.ensure_script
    assert "pc1" in script
    assert str(checkout) in script
    assert f'"snapshot": {"true" if snapshot else "false"}' in script
    assert f"{WORKSPACE}/.bakar/worktrees" in script


def test_ensure_failure_reports_reason(rec: Recorder, checkout: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rec.ensure_rc = 5
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    assert rc == 5
    assert "stream" not in rec.names()
    err = capsys.readouterr().err
    assert "another" in err.lower() or "marker" in err.lower()


def test_remote_argv_cwd_and_env(rec: Recorder, checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAS_WORK_DIR", "/local/ws")
    monkeypatch.setenv("BAKAR_FOO", "bar")
    local = [
        "build",
        str(checkout / "kas" / "machine" / "m.yml"),
        "-w",
        "/local/ws",
        "--workspace=/x",
        "--on",
        HOST,
        "-y",
    ]
    rc = _dispatch(
        checkout,
        str(checkout / "kas" / "machine" / "m.yml"),
        local,
        worktree_root=Path("/srv/wt"),
        build_dir_template="b-{worktree}-{stem}",
    )
    assert rc == 0
    script = rec.remote_script
    assert "cd /srv/wt/feature-x" in script
    assert "BAKAR_REPO_POLICY=reuse" in script
    assert "BAKAR_WORKTREE_BUILD_DIR=b-{worktree}-{stem}" in script
    assert "BAKAR_FOO=bar" in script
    assert "KAS_WORK_DIR" not in script
    assert "BAKAR_SCCACHE_DIST=0" in script
    line = _remote_argv_line(script)
    assert line.endswith("bakar build kas/machine/m.yml")
    assert str(checkout) not in line
    assert "-w" not in line.split()
    assert "--on" not in line


def test_colon_joined_relative_kas_arg(rec: Recorder, checkout: Path) -> None:
    cwd = checkout / "kas"
    kas_arg = "machine/m.yml:extra.yml"
    rc = _dispatch(checkout, kas_arg, ["build", kas_arg], invoking_cwd=cwd)
    assert rc == 0
    assert _remote_argv_line(rec.remote_script).endswith("bakar build kas/machine/m.yml:kas/extra.yml")


def test_kas_segment_outside_checkout_exits_2(rec: Recorder, checkout: Path, tmp_path: Path) -> None:
    outside = tmp_path / "other.yml"
    outside.write_text("header: {version: 14}\n")
    kas_arg = f"kas/machine/m.yml:{outside}"
    rc = _dispatch(checkout, kas_arg, ["build", kas_arg])
    assert rc == 2
    assert rec.calls == []


def test_plan_printed_without_prompt(rec: Recorder, checkout: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rec.snapshot = True
    rec.untracked = ["notes.txt", "kas/new.yml"]
    _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    err = " ".join(capsys.readouterr().err.split())
    root = f"{WORKSPACE}/.bakar/worktrees"
    assert "dispatch plan" in err
    assert f"{root}/feature-x" in err
    assert f"{WORKSPACE}/meta-avocado/.git/worktrees/feature-x" in err
    assert "bakar-delivered.json" in err
    assert f"bookkeeping files in {root} (created if missing)" in err
    assert ".feature-x.lock, .feature-x.pending, .feature-x.build.lock" in err
    assert f"refs/bakar/feature-x/{COMMIT[:12]}" in err
    assert f"{WORKSPACE}/build-m-feature-x" in err
    assert "snapshot of uncommitted changes: yes" in err
    assert "untracked paths the snapshot carries: notes.txt kas/new.yml" in err
    assert "nothing is deleted" in err


def test_plan_lists_at_most_twenty_untracked_paths(
    rec: Recorder, checkout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec.snapshot = True
    rec.untracked = [f"f{i:02d}" for i in range(23)]
    _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    err = capsys.readouterr().err
    assert "f19" in err
    assert "f20" not in err
    assert "and 3 more" in err


def test_plan_names_no_untracked_paths_without_a_snapshot(
    rec: Recorder, checkout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec.untracked = ["never-shown"]
    _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    assert "untracked" not in capsys.readouterr().err
    assert "untracked" not in rec.names()


def test_interrupted_stream_returns_130(rec: Recorder, checkout: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rec.stream = KeyboardInterrupt
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    assert rc == 130
    err = capsys.readouterr().err
    assert f"bakar stop --on {HOST}" in err
    assert "bakar triage" in err
    assert "surface" not in rec.names()


def test_lost_stream_skips_run_id(rec: Recorder, checkout: Path) -> None:
    rec.stream = (255, [], False)
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    assert rc == remote_dispatch._DISPATCH_LOST_EXIT
    assert "surface" not in rec.names()


def test_remote_failure_rc_propagates(rec: Recorder, checkout: Path) -> None:
    rec.stream = (1, ["Run `bakar triage 20260101-000000` for details.\n"], True)
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    assert rc == 1
    assert ("surface", (HOST, WORKSPACE / "build-m-feature-x", 1)) in rec.calls


def test_unusable_name_exits_2_before_any_remote_write(
    rec: Recorder, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unusable(_checkout: Path, _worktree: Any) -> str:
        return sanitize_delivered_name("...")

    monkeypatch.setattr(layer_delivery, "delivered_name", unusable)
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml", "--on", HOST])
    assert rc == 2
    assert "no usable characters" in capsys.readouterr().err
    # Refused before the host is contacted at all, not only before a write.
    assert not {"snapshot", "preflight", "capability", "check_workspace", "push", "ensure", "stream"} & set(rec.names())


def test_snapshot_failure_exits_1_before_any_ssh(
    rec: Recorder, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def too_big(_checkout: Path, *, cap_mb: int) -> tuple[str, bool]:
        raise layer_delivery.DeliveryError("untracked files exceed the cap")

    monkeypatch.setattr(layer_delivery, "snapshot_commit", too_big)
    rc = _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml"])
    assert rc == 1
    assert "untracked files exceed the cap" in capsys.readouterr().err
    assert rec.names() == ["delivered_name"]


def test_hyphen_host_rejected(rec: Recorder, checkout: Path) -> None:
    rc = ad.dispatch_avocado_build(
        "-oProxyCommand=x",
        ad.AvocadoDispatchRequest(
            workspace=WORKSPACE,
            checkout=checkout,
            worktree=None,
            kas_yaml=checkout / "kas" / "machine" / "m.yml",
            kas_arg="kas/machine/m.yml",
            local_args=["build", "kas/machine/m.yml"],
            release="scarthgap",
            worktree_root=None,
            build_dir_template=None,
            cap_mb=64,
            invoking_cwd=checkout,
        ),
        remote_dispatch.DispatchFlags(sccache_dist=False, assume_yes=False),
    )
    assert rc == 1
    # Only the local name and snapshot ran; nothing reached ssh or git push.
    assert not {"preflight", "capability", "check_workspace", "push", "ensure", "stream"} & set(rec.names())


def test_strip_workspace_option_forms() -> None:
    args = ["build", "-w", "/a", "--workspace", "/b", "--workspace=/c", "-w/d", "k.yml"]
    assert ad.strip_workspace_option(args) == ["build", "k.yml"]


def test_strip_workspace_option_in_short_clusters() -> None:
    assert ad.strip_workspace_option(["build", "-kw", "/a", "k.yml"]) == ["build", "-k", "k.yml"]
    assert ad.strip_workspace_option(["build", "-kw/a", "k.yml"]) == ["build", "-k", "k.yml"]
    assert ad.strip_workspace_option(["build", "-nk", "k.yml"]) == ["build", "-nk", "k.yml"]


def test_remote_env_always_names_the_build_dir_template() -> None:
    assert ad.remote_env(None)["BAKAR_WORKTREE_BUILD_DIR"] == "build-{stem}-{worktree}"
    assert ad.remote_env("b-{stem}")["BAKAR_WORKTREE_BUILD_DIR"] == "b-{stem}"


@pytest.mark.parametrize(
    "forbidden", ["rm ", "worktree remove", "worktree prune", "reset --hard", "clean -f", "--force"]
)
def test_dispatched_scripts_carry_no_destructive_command(rec: Recorder, checkout: Path, forbidden: str) -> None:
    assert _dispatch(checkout, "kas/machine/m.yml", ["build", "kas/machine/m.yml", "--on", HOST]) == 0
    assert forbidden not in rec.ensure_script
    assert forbidden not in rec.remote_script


# Fragments of a command that deletes, moves or prunes on the host. A delivered
# script carrying any of them could cost the shared workspace data.
_DESTRUCTIVE = (
    "rm ",
    " gc",
    "rmdir",
    " mv ",
    "unlink",
    "--prune",
    "--del",
    "reset --hard",
    "worktree remove",
    "worktree prune",
    "--force",
)


@settings(max_examples=25, deadline=None)
@given(
    snapshot=st.booleans(),
    assume_yes=st.booleans(),
    worktree_root=st.sampled_from([None, Path("/srv/wt"), Path("/home/u/.bakar wt")]),
    build_dir_template=st.sampled_from([None, "b-{worktree}-{stem}"]),
    sccache_dist=st.booleans(),
)
def test_every_avocado_dispatch_plan_and_script_is_non_destructive(  # noqa: PLR0913 - one argument per hypothesis strategy
    tmp_path_factory: pytest.TempPathFactory,
    snapshot: bool,
    assume_yes: bool,
    worktree_root: Path | None,
    build_dir_template: str | None,
    sccache_dist: bool,
) -> None:
    built: list[remote_dispatch.DispatchPlan] = []

    # __init__ rather than __post_init__: the base dataclass's generated __init__
    # only calls a __post_init__ that existed when it was decorated.
    class _Recording(remote_dispatch.DispatchPlan):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            built.append(self)

    co = tmp_path_factory.mktemp("prop") / "feature-x"
    with pytest.MonkeyPatch.context() as mp:
        rec = Recorder(mp)
        rec.snapshot = snapshot
        rec.untracked = ["notes.txt"]
        mp.setattr(remote_dispatch, "DispatchPlan", _Recording)
        rc = _dispatch(
            co,
            "kas/machine/m.yml",
            ["build", "kas/machine/m.yml", "--on", HOST],
            assume_yes=assume_yes,
            worktree_root=worktree_root,
            build_dir_template=build_dir_template,
            sccache_dist=sccache_dist,
        )
    assert rc == 0
    assert len(built) == 1
    assert all(plan.deletes == [] for plan in built)
    assert rec.ensure_script and rec.remote_script
    for fragment in _DESTRUCTIVE:
        assert fragment not in rec.ensure_script, fragment
        assert fragment not in rec.remote_script, fragment
