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
