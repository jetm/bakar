"""Invariants of a layer dispatch that must hold for any name, path or option.

Spec: layer-repo-delivery, "Never delete anything on the host for a layer
dispatch". The example tests live in ``test_avocado_dispatch``; these drive
``dispatch_layer_build`` with generated names and paths and check the four
things that must never vary: an empty deletion list, no delete/force/prune/
remove option in anything rendered, the dispatching host's workspace never
reaching the remote command, and an older-host refusal that ``--yes`` cannot
override.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bakar import bsp_detect
from bakar.steps import avocado_dispatch as ad
from bakar.steps import remote_dispatch
from tests.test_avocado_dispatch import (
    HOST,
    LAYER_ARGV,
    LAYER_NAME,
    LAYER_WRITES,
    WORKSPACE,
    LayerRec,
    Recorder,
    _remote_argv_line,
)
from tests.test_layer_delivery_remote import _FORBIDDEN

pytestmark = pytest.mark.unit

# Reused from the delivery-script tests, plus the words the spec names outright.
_FORBIDDEN_FRAGMENTS = (*_FORBIDDEN, "force", "prune", "remove", "delete")
# The one place a delivery script says "delete": the message of its refusal to nest a
# worktree inside one an older bakar left behind (an echo, never a command).
_OLDER_LAYOUT_REFUSAL_TAIL = '"deletes one, so retire it yourself, then dispatch again"'
_FORBIDDEN_ARGV = {"--force", "-f", "--delete", "--prune", "--mirror", "--force-with-lease", "--remove-source-files"}

# Awkward characters, with no letter that could spell a forbidden word (r, m, f,
# d, c, l, e, g and p are left out) so a hit in a script is a real option and
# not a generated path that happens to contain "force".
_AWKWARD = "abxyz0 -_.'\"\\;$()&|`<>*?\t\u00e9\u65e5\u672c"
_awkward_text = st.text(alphabet=_AWKWARD, min_size=0, max_size=30)
_long_text = st.text(alphabet="abxyz-", min_size=200, max_size=300)
_name_part = st.one_of(_awkward_text, _long_text)
_dir_part = st.one_of(_awkward_text, _long_text).map(lambda s: "/" + "d" + s)
_branch = st.tuples(st.sampled_from(["", "-", "--", "a", "..", ".x"]), _name_part).map(lambda t: "a" + t[0] + t[1])


def _write_files(co: Path) -> None:
    (co / "kas" / "machine").mkdir(parents=True, exist_ok=True)
    (co / "kas" / "machine" / "m.yml").write_text("header: {version: 14}\n")


def _layer(co: Path, *, branch: str, linked: bool, dirname: str) -> bsp_detect.LayerRepo:
    return bsp_detect.LayerRepo(
        checkout=co,
        name=LAYER_NAME,
        dependency={},
        is_linked_worktree=linked,
        checkout_name=dirname,
        branch=branch,
        entry=co / "kas" / "machine" / "m.yml",
    )


def _dispatch(  # noqa: PLR0913 - one keyword per request knob under test
    co: Path,
    layer: bsp_detect.LayerRepo,
    local_args: list[str],
    *,
    layer_repo_dir: Path | None = None,
    worktree_root: Path | None = None,
    assume_yes: bool = False,
    build_dir_template: str | None = None,
    dry_run: bool = False,
) -> int:
    request = ad.AvocadoDispatchRequest(
        workspace=WORKSPACE,
        checkout=co,
        worktree=None,
        kas_yaml=co / "kas" / "machine" / "m.yml",
        kas_arg="kas/machine/m.yml",
        local_args=local_args,
        release="scarthgap",
        worktree_root=worktree_root,
        build_dir_template=build_dir_template,
        cap_mb=64,
        invoking_cwd=co,
        workspace_source="[workspaces] template",
    )
    flags = remote_dispatch.DispatchFlags(sccache_dist=False, assume_yes=assume_yes, dry_run=dry_run)
    return ad.dispatch_layer_build(HOST, request, layer, layer_repo_dir, flags)


def _push_argv(rec: Recorder) -> list[str]:
    return next(a for n, a in rec.calls if n == "push")


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    branch=_branch,
    dirname=_branch,
    linked=st.booleans(),
    layer_repo_dir=st.one_of(st.none(), _dir_part.map(Path)),
    worktree_root=st.one_of(st.none(), _dir_part.map(Path)),
    assume_yes=st.booleans(),
    snapshot=st.booleans(),
    template=st.sampled_from([None, "b-{worktree}-{stem}"]),
)
def test_no_layer_dispatch_plans_a_deletion_or_renders_a_destructive_option(  # noqa: PLR0913 - one argument per strategy
    tmp_path_factory: pytest.TempPathFactory,
    branch: str,
    dirname: str,
    linked: bool,
    layer_repo_dir: Path | None,
    worktree_root: Path | None,
    assume_yes: bool,
    snapshot: bool,
    template: str | None,
) -> None:
    built: list[remote_dispatch.DispatchPlan] = []

    class _Recording(remote_dispatch.DispatchPlan):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            built.append(self)

    co = tmp_path_factory.mktemp("inv") / "wt"
    _write_files(co)
    with pytest.MonkeyPatch.context() as mp:
        rec = Recorder(mp)
        lrec = LayerRec(rec, mp)
        rec.snapshot = snapshot
        rec.untracked = ["notes.txt"]
        mp.setattr(remote_dispatch, "DispatchPlan", _Recording)
        rc = _dispatch(
            co,
            _layer(co, branch=branch, linked=linked, dirname=dirname),
            list(LAYER_ARGV),
            layer_repo_dir=layer_repo_dir,
            worktree_root=worktree_root,
            assume_yes=assume_yes,
            build_dir_template=template,
        )
    # A name that sanitises to nothing is refused before the host is contacted.
    if rc != 0:
        assert not LAYER_WRITES & set(rec.names())
        return
    assert len(built) == 1
    assert built[0].deletes == []
    push = _push_argv(rec)
    assert not _FORBIDDEN_ARGV & set(push)
    assert not any(arg.startswith(("--force", "--delete", "--prune", "--mirror")) for arg in push[3:6])
    for script in (lrec.repo_script, rec.ensure_script, rec.remote_script):
        assert script
        # The older-layout refusal only echoes that bakar never deletes; it runs nothing.
        scanned = script.replace(_OLDER_LAYOUT_REFUSAL_TAIL, "")
        for fragment in _FORBIDDEN_FRAGMENTS:
            assert fragment not in scanned, fragment


@pytest.mark.parametrize("dirname", ["--force", "-f", "--delete", "--prune", ";rm -rf x", "$(rm x)", "'; rm x; '"])
def test_hostile_checkout_names_never_reach_a_destructive_option(
    tmp_path: Path, dirname: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    co = tmp_path / "wt"
    _write_files(co)
    rec = Recorder(monkeypatch)
    lrec = LayerRec(rec, monkeypatch)
    rc = _dispatch(co, _layer(co, branch="main", linked=True, dirname=dirname), list(LAYER_ARGV))
    if rc != 0:
        assert not LAYER_WRITES & set(rec.names())
        return
    push = _push_argv(rec)
    assert not _FORBIDDEN_ARGV & set(push)
    # The hostile name is sanitised into one safe ref component, so no push
    # argument can start with a dash after the subcommand.
    assert all(not arg.startswith("-") for arg in push[push.index("push") + 2 :])
    # The sanitised name is data, so a bare word like "force" may appear as a
    # name; only option-shaped fragments count here.
    for script in (lrec.repo_script, rec.ensure_script, rec.remote_script):
        for fragment in _FORBIDDEN:
            assert fragment not in script, fragment


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    local_ws=_name_part.map(lambda s: "/LOCALWS" + s),
    form=st.sampled_from(["-w", "--workspace", "--workspace=", "-w="]),
    before_terminator=st.booleans(),
)
def test_dispatching_hosts_workspace_never_reaches_the_remote_command(
    tmp_path_factory: pytest.TempPathFactory, local_ws: str, form: str, before_terminator: bool
) -> None:
    co = tmp_path_factory.mktemp("ws") / "wt"
    _write_files(co)
    if form == "--workspace=":
        opt = [f"--workspace={local_ws}"]
    elif form == "-w=":
        opt = [f"-w{local_ws}"]
    else:
        opt = [form, local_ws]
    tail = ["--", "bitbake", "x"] if before_terminator else []
    local = ["build", "kas/machine/m.yml", *opt, "--on", HOST, "-y", *tail]
    with pytest.MonkeyPatch.context() as mp:
        rec = Recorder(mp)
        LayerRec(rec, mp)
        mp.setenv("KAS_WORK_DIR", local_ws)
        rc = _dispatch(co, _layer(co, branch="main", linked=False, dirname="wt"), local)
    assert rc == 0
    script = rec.remote_script
    assert "LOCALWS" not in script
    tokens = shlex.split(_remote_argv_line(script))
    bakar = tokens.index("bakar")
    remote_args = tokens[bakar + 1 :]
    stop = remote_args.index("--") if "--" in remote_args else len(remote_args)
    head = remote_args[:stop]
    assert head.count("--workspace") == 1
    assert head[head.index("--workspace") + 1] == str(WORKSPACE)
    assert "-w" not in head
    assert "--on" not in remote_args


_PROBE_FAILURES = [
    (2, "Unknown key: dispatch.layer_repo_dir\n"),
    (2, ""),
    (1, "error: no such setting\n"),
    (127, "bash: bakar: command not found\n"),
    (255, "Connection refused\n"),
    (255, ""),
    (3, "Unknown key: dispatch.layer_repo_dir\n"),
]


@pytest.mark.parametrize(("rc", "stderr"), _PROBE_FAILURES)
@pytest.mark.parametrize("dry_run", [False, True])
def test_yes_never_lets_a_failed_capability_probe_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int, stderr: str, dry_run: bool
) -> None:
    co = tmp_path / "wt"
    _write_files(co)
    rec = Recorder(monkeypatch)
    lrec = LayerRec(rec, monkeypatch)
    rec.capability_rc = rc
    rec.capability_stderr = stderr
    result = _dispatch(
        co,
        _layer(co, branch="main", linked=False, dirname="wt"),
        list(LAYER_ARGV),
        assume_yes=True,
        dry_run=dry_run,
    )
    assert result != 0
    assert "capability" in rec.names()
    assert not ({"check_workspace", "pin_probe"} | LAYER_WRITES) & set(rec.names())
    assert lrec.repo_script == ""
    assert rec.ensure_script == "" and rec.remote_script == ""
