"""Tests for build-time dependency-graph capture.

The hazard this capture carries is asymmetric and worth stating: a stranded
bitbake cooker does not fail the build that stranded it. It fails the NEXT
build, on a different machine, with an error naming neither the graph pass nor
the run that caused it. So the payload's sequencing is the thing under test, and
its FAILURE path matters more than its success path - six real captures on the
fleet exercised the success path and none ever exercised the other.

The headline falsifier: a payload joined with ``&&`` instead of ``;`` passes
every success-path test here and strands the lock in exactly the case the unlock
exists for.
"""

from __future__ import annotations

from pathlib import Path

from bakar.steps.kas_build import GRAPH_ARTIFACTS, GRAPH_MARKER_NAME, graph_capture_command


def test_payload_releases_the_lock_after_the_graph_pass() -> None:
    cmd = graph_capture_command("core-image-minimal")

    assert "bitbake -g core-image-minimal" in cmd
    assert "bitbake -m" in cmd


def test_payload_sequencing_unlocks_after_a_failed_graph_pass() -> None:
    """The headline falsifier, and the reason this is `;` rather than `&&`.

    A `bitbake -g` that starts its cooker and then exits non-zero - a parse
    error, ENOSPC, a recipe broken by whatever is being built - short-circuits
    an `&&` and leaves that server holding the lock.
    """
    cmd = graph_capture_command("core-image-minimal")

    assert "&&" not in cmd
    graph_pass, _, rest = cmd.partition(";")
    assert "bitbake -g" in graph_pass
    assert "bitbake -m" in rest


def test_payload_preserves_the_graph_pass_exit_status() -> None:
    """The unlock must not swallow the failure it is sequenced past."""
    cmd = graph_capture_command("core-image-minimal")

    assert "rc=$?" in cmd
    assert cmd.rstrip().endswith("exit $rc")
    # `rc` is captured before the unlock runs, or it would hold bitbake -m's status.
    assert cmd.index("rc=$?") < cmd.index("bitbake -m")


def test_the_unlock_runs_under_bash_syntax_not_fish() -> None:
    """`rc=$?` is bash-only and kas hands the payload to $SHELL.

    The login shell on this fleet is fish, which rejects it with
    "Unsupported use of '='" at exit 127 - before bitbake starts, so the
    traceback names bitbake rather than the shell. The capture pins SHELL for
    that reason; this pins the assumption that the payload NEEDS it.
    """
    cmd = graph_capture_command("core-image-minimal")

    assert "rc=$?" in cmd, "a payload with no bash-only syntax would not need the SHELL pin"


def test_target_is_quoted_so_it_cannot_inject_shell() -> None:
    """The target reaches a shell payload, so it is quoted rather than trusted.

    A target is normally an image name from config, but it flows into a string
    handed to `sh -c` - and the payload's own structure is `;`-separated, so an
    unquoted target carrying a `;` would append commands rather than name a
    recipe.
    """
    cmd = graph_capture_command("weird name; rm -rf /tmp/x")

    # The whole target sits inside one quoted word, so its `;` is data.
    assert "'weird name; rm -rf /tmp/x'" in cmd
    # And the payload's own tail is unchanged - the injection did not displace
    # the unlock or the exit-status propagation.
    assert cmd.endswith("; rc=$?; bitbake -m; exit $rc")


def test_both_artifacts_are_named() -> None:
    assert set(GRAPH_ARTIFACTS) == {"task-depends.dot", "pn-buildlist"}


def test_marker_name_is_distinct_from_the_artifacts() -> None:
    """The sidecar carries provenance; co-location alone is not evidence."""
    assert GRAPH_MARKER_NAME not in GRAPH_ARTIFACTS
    assert GRAPH_MARKER_NAME.endswith(".json")


def test_idle_wait_returns_immediately_when_the_cooker_is_already_quiet(monkeypatch, tmp_path) -> None:
    from bakar.steps import kas_build

    monkeypatch.setattr(kas_build, "_lock_holder_has_activity", lambda _d: False)
    slept: list[float] = []
    monkeypatch.setattr(kas_build.time, "sleep", slept.append)

    assert kas_build._wait_for_cooker_idle(tmp_path, _FakeLog()) is True
    assert slept == [], "a quiet cooker must not cost a single poll interval"


def test_idle_wait_returns_true_once_activity_stops(monkeypatch, tmp_path) -> None:
    """The real case: active at first, idle a few seconds later.

    Measured on a real build - refused at 17:07:07, the identical command
    succeeded at 17:07:17.
    """
    from bakar.steps import kas_build

    calls = iter([True, True, False])
    monkeypatch.setattr(kas_build, "_lock_holder_has_activity", lambda _d: next(calls))
    monkeypatch.setattr(kas_build.time, "sleep", lambda _s: None)

    assert kas_build._wait_for_cooker_idle(tmp_path, _FakeLog()) is True


def test_idle_wait_times_out_rather_than_blocking_teardown(monkeypatch, tmp_path) -> None:
    """A capture that cannot get a quiet cooker is an absent optional artifact.

    Blocking a completed build's teardown on one would be the worse trade, so
    this must return False rather than wait indefinitely.
    """
    from bakar.steps import kas_build

    monkeypatch.setattr(kas_build, "_lock_holder_has_activity", lambda _d: True)
    monkeypatch.setattr(kas_build.time, "sleep", lambda _s: None)
    log = _FakeLog()

    assert kas_build._wait_for_cooker_idle(tmp_path, log, timeout=0.0) is False
    assert any("still active" in w for w in log.warnings)


class _FakeLog:
    """Minimal RunLogger stand-in: the helpers under test only warn and info."""

    def __init__(self, run_dir: Path | None = None) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []
        # Only the capture path reads this; the idle/target helpers never do.
        self.run_dir = run_dir if run_dir is not None else Path()

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def info(self, msg: str) -> None:
        self.infos.append(msg)


def test_explicit_ctx_target_wins_without_shelling_out(monkeypatch) -> None:
    """A command-line target needs no kas dump."""
    from bakar.steps import kas_build

    def _fail(*_a, **_k):
        raise AssertionError("kas dump must not run when ctx.target is set")

    monkeypatch.setattr(kas_build, "run_kas_subcommand", _fail)
    ctx = type("Ctx", (), {"target": "my-image"})()

    assert kas_build._resolve_capture_target(ctx, _FakeLog()) == "my-image"


def _capture_ctx(tmp_path):
    """Minimal ctx/log pair for ``_capture_dependency_graph``.

    Only the attributes the capture actually reads are supplied; the kas launch
    itself is stubbed out by each test.
    """
    cfg = type(
        "Cfg",
        (),
        {
            "bsp_root": tmp_path,
            "build_dir_name": "build",
            "resolved_tmpdir": tmp_path / "build" / "tmp",
            "capture_graph": True,
        },
    )()
    run_dir = tmp_path / "runs" / "20260101-000000"
    run_dir.mkdir(parents=True)
    log = _FakeLog(run_dir)
    ctx = type("Ctx", (), {"target": "core-image-minimal", "cfg": cfg})()
    return ctx, log


def test_capture_opts_into_a_bound_and_its_own_process_group(monkeypatch, tmp_path) -> None:
    """The capture is the only caller that opts in, and it must opt into BOTH.

    A deadline without ``isolate_process_group=True`` is worse than no deadline:
    the child would share bakar's process group, so the escalation the timeout
    triggers has nothing safe to signal and abandons the cooker instead.
    """
    from bakar.steps import kas_build

    ctx, log = _capture_ctx(tmp_path)
    monkeypatch.setattr(kas_build, "_wait_for_cooker_idle", lambda *_a, **_k: True)
    seen: dict[str, object] = {}

    def _fake_capture(_ctx, _cmd, _out, **kwargs):
        seen.update(kwargs)
        return 1  # stop before the artifact copy; the kwargs are what is under test

    monkeypatch.setattr(kas_build, "run_shell_capture", _fake_capture)

    assert kas_build._capture_dependency_graph(ctx, log) is None

    assert seen["timeout"] == kas_build.GRAPH_CAPTURE_TIMEOUT_S
    assert seen["isolate_process_group"] is True


def test_capture_timeout_is_an_order_of_magnitude_above_the_idle_wait() -> None:
    """The two bounds answer different questions and must not be confused.

    The idle wait bounds how long to wait FOR a quiet cooker; this bounds the
    graph pass itself, which legitimately runs for minutes on a large tree. A
    capture deadline anywhere near the idle wait would abort healthy work.
    """
    from bakar.steps import kas_build

    assert kas_build.GRAPH_CAPTURE_TIMEOUT_S >= 10 * kas_build.GRAPH_CAPTURE_IDLE_TIMEOUT_S


def test_capture_reports_a_hang_distinctly_from_a_failure(monkeypatch, tmp_path) -> None:
    """A timeout must not crash the completed build, and must read as a hang.

    The build already produced its image; the graph is an optional artifact. But
    "capture failed" and "capture hung and was killed" send a later triage to
    different places, so the message keeps them apart.
    """
    import subprocess

    from bakar.steps import kas_build

    ctx, log = _capture_ctx(tmp_path)
    monkeypatch.setattr(kas_build, "_wait_for_cooker_idle", lambda *_a, **_k: True)

    def _hang(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="kas", timeout=900.0)

    monkeypatch.setattr(kas_build, "run_shell_capture", _hang)

    assert kas_build._capture_dependency_graph(ctx, log) is None
    assert any("exceeded" in w and "killed" in w for w in log.warnings), log.warnings


def test_declining_the_capture_skips_the_cooker_idle_wait(monkeypatch, tmp_path) -> None:
    """``capture_graph = False`` returns before anything that costs time.

    The idle wait is up to 60s of pure waiting and runs before the capture, so a
    guard placed after it would make declining cost almost as much as accepting.
    Both the wait and the target resolution are booby-trapped here: reaching
    either one fails the test rather than merely slowing it.
    """
    from bakar.steps import kas_build

    ctx, log = _capture_ctx(tmp_path)
    ctx.cfg.capture_graph = False

    def _must_not_run(*_a, **_k):
        raise AssertionError("declining the capture must not wait, resolve, or launch")

    monkeypatch.setattr(kas_build, "_wait_for_cooker_idle", _must_not_run)
    monkeypatch.setattr(kas_build, "_resolve_capture_target", _must_not_run)
    monkeypatch.setattr(kas_build, "run_shell_capture", _must_not_run)

    assert kas_build._capture_dependency_graph(ctx, log) is None
    assert log.warnings == [], "declining is a choice, not a problem to warn about"
    assert any("declined" in m for m in log.infos), log.infos
