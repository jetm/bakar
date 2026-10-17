"""Unit tests for the container primitives in :mod:`bakar.stop_container`.

Covers runtime detection, container-id resolution, the tri-state liveness
query, goneness by listing, running-container discovery with its de-duplication
and warn-wrapper, and the in-container bitbake SIGINT. Every test is hermetic:
no real docker/podman is invoked and the subprocess seam is patched on the
``stop_container`` module.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from bakar import stop_container

pytestmark = pytest.mark.unit


# --- _container_liveness tri-state -----------------------------------------


def test_container_liveness_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """inspect reporting State.Running == true maps to _ALIVE."""
    monkeypatch.setattr(
        stop_container.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="true\n", stderr=""),
    )
    assert stop_container._container_liveness("docker", "cid") == stop_container._ALIVE


def test_container_liveness_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean inspect reporting false is a definitive _DEAD, not an error."""
    monkeypatch.setattr(
        stop_container.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="false\n", stderr=""),
    )
    assert stop_container._container_liveness("docker", "cid") == stop_container._DEAD


def test_container_liveness_nonzero_exit_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero inspect exit is a query error (keep polling), not a drained container."""
    monkeypatch.setattr(
        stop_container.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="daemon unreachable"),
    )
    assert stop_container._container_liveness("docker", "cid") == stop_container._ERROR


def test_container_liveness_oserror_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError (runtime binary absent) is a query error, never _DEAD."""

    def _boom(*_a: object, **_k: object) -> SimpleNamespace:
        raise OSError("no runtime")

    monkeypatch.setattr(stop_container.subprocess, "run", _boom)
    assert stop_container._container_liveness("docker", "cid") == stop_container._ERROR


# --- discover_running_containers (group 10: container-mode discovery) ------


def test_container_discovery_excludes_stopped_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only currently-running containers are queried and reported - no ``-a``."""
    captured_args: list[list[str]] = []

    def _fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        captured_args.append(args)
        return SimpleNamespace(
            returncode=0,
            stdout="cid-live\t20260101-000000\n",
            stderr="",
        )

    monkeypatch.setattr(stop_container.subprocess, "run", _fake_run)

    result = stop_container.discover_running_containers("docker")

    assert result == [stop_container.ContainerCandidate(run_id="20260101-000000", container_id="cid-live")]
    assert len(captured_args) == 1, f"expected exactly one query, got {len(captured_args)}"
    args = captured_args[0]
    assert args[0] == "docker", "must query only the single selected runtime"
    assert "-a" not in args, "a running-only query must never pass -a (all containers)"
    assert "--all" not in args, "a running-only query must never pass --all"
    assert any(a.startswith("label=bakar.run_id") for a in args), (
        "must filter on the bare bakar.run_id label KEY, not one specific run id"
    )


def test_container_discovery_never_queries_a_second_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """A build running under the non-selected runtime is absent - no second query is made."""
    captured_args: list[list[str]] = []

    def _fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        captured_args.append(args)
        # The selected runtime ("docker") has nothing running; a build is only
        # visible under "podman", which discovery must never query to "find" it.
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(stop_container.subprocess, "run", _fake_run)

    result = stop_container.discover_running_containers("docker")

    assert result == []
    assert len(captured_args) == 1, f"expected exactly one query, got {len(captured_args)}"
    runtimes_queried = {args[0] for args in captured_args}
    assert runtimes_queried == {"docker"}, f"queried more than the selected runtime: {runtimes_queried}"


def test_container_discovery_reports_multiple_run_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple distinct running containers each produce their own candidate."""
    monkeypatch.setattr(
        stop_container.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout="cid-a\t20260101-000000\ncid-b\t20260102-000000\n",
            stderr="",
        ),
    )

    result = stop_container.discover_running_containers("podman")

    assert result == [
        stop_container.ContainerCandidate(run_id="20260101-000000", container_id="cid-a"),
        stop_container.ContainerCandidate(run_id="20260102-000000", container_id="cid-b"),
    ]


def test_container_discovery_empty_output_is_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """No running containers carrying the label is a clean empty result, not an error."""
    monkeypatch.setattr(
        stop_container.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert stop_container.discover_running_containers("docker") == []


def test_container_discovery_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero exit from the runtime query raises rather than being swallowed here."""
    monkeypatch.setattr(
        stop_container.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="daemon unreachable"),
    )

    with pytest.raises(RuntimeError, match="daemon unreachable"):
        stop_container.discover_running_containers("docker")


def test_container_discovery_oserror_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing runtime binary (OSError) raises rather than being swallowed here."""

    def _boom(*_a: object, **_k: object) -> SimpleNamespace:
        raise OSError("no such file")

    monkeypatch.setattr(stop_container.subprocess, "run", _boom)

    with pytest.raises(RuntimeError):
        stop_container.discover_running_containers("docker")


def test_container_discovery_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timed-out query raises rather than hanging or being swallowed here."""

    def _timeout(*_a: object, **_k: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd="docker", timeout=stop_container._RUNTIME_QUERY_TIMEOUT_S)

    monkeypatch.setattr(stop_container.subprocess, "run", _timeout)

    with pytest.raises(RuntimeError):
        stop_container.discover_running_containers("docker")


def test_container_discovery_malformed_line_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A line with no tab separator (malformed output) raises rather than being silently dropped."""
    monkeypatch.setattr(
        stop_container.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="cid-only-no-run-id\n", stderr=""),
    )

    with pytest.raises(RuntimeError, match="malformed"):
        stop_container.discover_running_containers("docker")


# --- dedup_container_candidates (group 12: container-mode within-source dedup) --


def test_dedup_container_candidates_collapses_shared_run_id_first_seen_wins() -> None:
    """Two running containers sharing a run-id label (the documented kas-shell /
    timeout-escalation auxiliary-container case) collapse to one row, keeping
    whichever candidate the runtime's query returned first for that label."""
    main = stop_container.ContainerCandidate(run_id="20260618-120000-111", container_id="cid-main")
    aux = stop_container.ContainerCandidate(run_id="20260618-120000-111", container_id="cid-aux")

    result = stop_container.dedup_container_candidates([main, aux])

    assert result == [main]


def test_dedup_container_candidates_preserves_runtime_order_not_sorted() -> None:
    """Dedup does not re-sort - distinct run ids keep the runtime's own reported order."""
    second = stop_container.ContainerCandidate(run_id="20260102-000000", container_id="cid-b")
    first = stop_container.ContainerCandidate(run_id="20260101-000000", container_id="cid-a")

    result = stop_container.dedup_container_candidates([second, first])

    assert result == [second, first]


def test_dedup_container_candidates_no_collision_is_a_no_op() -> None:
    """No shared run id across candidates - every entry is kept, unchanged."""
    a = stop_container.ContainerCandidate(run_id="20260101-000000", container_id="cid-a")
    b = stop_container.ContainerCandidate(run_id="20260102-000000", container_id="cid-b")

    assert stop_container.dedup_container_candidates([a, b]) == [a, b]


def test_dedup_container_candidates_empty_list_is_empty() -> None:
    assert stop_container.dedup_container_candidates([]) == []


# --- discover_running_containers_or_warn (group 11: graceful degradation) --


def test_discover_or_warn_success_passes_through_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean query returns the candidates unchanged and no warning."""
    monkeypatch.setattr(
        stop_container.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout="cid-live\t20260101-000000\n",
            stderr="",
        ),
    )

    candidates, warning = stop_container.discover_running_containers_or_warn("docker")

    assert candidates == [stop_container.ContainerCandidate(run_id="20260101-000000", container_id="cid-live")]
    assert warning is None


def test_discover_or_warn_nonzero_exit_degrades_to_empty_list_and_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero exit collapses to an empty list plus a warning naming container discovery."""
    monkeypatch.setattr(
        stop_container.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="daemon unreachable"),
    )

    candidates, warning = stop_container.discover_running_containers_or_warn("docker")

    assert candidates == []
    assert warning is not None
    assert "container discovery" in warning


def test_discover_or_warn_oserror_degrades_to_empty_list_and_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing runtime binary (OSError) degrades the same way as any other query failure."""

    def _boom(*_a: object, **_k: object) -> SimpleNamespace:
        raise OSError("no such file")

    monkeypatch.setattr(stop_container.subprocess, "run", _boom)

    candidates, warning = stop_container.discover_running_containers_or_warn("docker")

    assert candidates == []
    assert warning is not None
    assert "container discovery" in warning


def test_discover_or_warn_timeout_degrades_to_empty_list_and_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timed-out query degrades the same way as any other query failure."""

    def _timeout(*_a: object, **_k: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd="docker", timeout=stop_container._RUNTIME_QUERY_TIMEOUT_S)

    monkeypatch.setattr(stop_container.subprocess, "run", _timeout)

    candidates, warning = stop_container.discover_running_containers_or_warn("docker")

    assert candidates == []
    assert warning is not None
    assert "container discovery" in warning


def test_discover_or_warn_malformed_output_degrades_to_empty_list_and_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed output degrades the same way as any other query failure."""
    monkeypatch.setattr(
        stop_container.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="cid-only-no-run-id\n", stderr=""),
    )

    candidates, warning = stop_container.discover_running_containers_or_warn("docker")

    assert candidates == []
    assert warning is not None
    assert "container discovery" in warning


def test_discover_or_warn_does_not_print(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The wrapper never prints - the (results, warning) tuple is the whole contract."""
    monkeypatch.setattr(
        stop_container.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="daemon unreachable"),
    )

    stop_container.discover_running_containers_or_warn("docker")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# --- _detect_runtime -------------------------------------------------------


def test_detect_runtime_honors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """KAS_CONTAINER_ENGINE=podman selects podman regardless of PATH."""
    monkeypatch.setenv("KAS_CONTAINER_ENGINE", "podman")
    monkeypatch.setattr(stop_container.shutil, "which", lambda _name: "/usr/bin/docker")

    assert stop_container.detect_runtime() == "podman"


def test_detect_runtime_prefers_docker_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env override and docker on PATH, docker wins."""
    monkeypatch.delenv("KAS_CONTAINER_ENGINE", raising=False)
    monkeypatch.setattr(stop_container.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)

    assert stop_container.detect_runtime() == "docker"


def test_detect_runtime_falls_back_to_podman(monkeypatch: pytest.MonkeyPatch) -> None:
    """With only podman installed, podman is detected."""
    monkeypatch.delenv("KAS_CONTAINER_ENGINE", raising=False)
    monkeypatch.setattr(stop_container.shutil, "which", lambda name: "/usr/bin/podman" if name == "podman" else None)

    assert stop_container.detect_runtime() == "podman"


# --- _container_id ---------------------------------------------------------


def test_container_id_empty_stdout_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty `ps -q -f label=` stdout resolves to no container."""
    monkeypatch.setattr(
        stop_container.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert stop_container._container_id("docker", "bakar.run_id=X") is None


def test_container_id_returns_first_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A container id line is returned verbatim."""
    monkeypatch.setattr(
        stop_container.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="deadbeef1234\n", stderr=""),
    )

    assert stop_container._container_id("podman", "bakar.run_id=X") == "deadbeef1234"


# --- _sigint_bitbake_in_container ------------------------------------------


def test_sigint_bitbake_in_container_execs_pkill_main_bitbake(monkeypatch: pytest.MonkeyPatch) -> None:
    """It execs `pkill -INT -f 'bin/bitbake '` (trailing space targets the UI, not workers)."""
    calls: list[list[str]] = []

    def _fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(stop_container.subprocess, "run", _fake_run)

    assert stop_container._sigint_bitbake_in_container("docker", "abc123") is True
    assert calls == [["docker", "exec", "abc123", "pkill", "-INT", "-f", "bin/bitbake "]]


def test_sigint_bitbake_in_container_no_match_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """pkill exit 1 (no bitbake process matched) -> False so the caller can fall back."""
    monkeypatch.setattr(
        stop_container.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )

    assert stop_container._sigint_bitbake_in_container("podman", "cid") is False


def test_sigint_bitbake_in_container_oserror_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError from the exec (runtime binary absent) -> False, never raises."""

    def _boom(*_a: object, **_k: object) -> SimpleNamespace:
        raise OSError("no runtime")

    monkeypatch.setattr(stop_container.subprocess, "run", _boom)

    assert stop_container._sigint_bitbake_in_container("docker", "abc") is False


# --- goneness is decided by listing the id, not by inspecting it ------------


def _ps_result(stdout: str, rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker"], rc, stdout=stdout, stderr="")


def test_container_gone_lists_by_id_including_stopped_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(args)
        assert kwargs["timeout"] == stop_container._RUNTIME_QUERY_TIMEOUT_S
        return _ps_result("")

    monkeypatch.setattr(stop_container.subprocess, "run", fake_run)
    assert stop_container._container_gone("docker", "cafef00d") is True
    assert seen == [["docker", "ps", "-a", "-q", "--filter", "id=cafef00d"]]


def test_container_gone_false_when_the_id_is_still_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stop_container.subprocess, "run", lambda *_a, **_k: _ps_result("cafef00d\n"))
    assert stop_container._container_gone("docker", "cafef00d") is False


@pytest.mark.parametrize("failure", ["rc", "oserror", "timeout"])
def test_container_gone_is_unknown_when_the_query_fails(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    def fake_run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        if failure == "oserror":
            raise FileNotFoundError("docker")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("docker", 1)
        return _ps_result("", rc=1)

    monkeypatch.setattr(stop_container.subprocess, "run", fake_run)
    assert stop_container._container_gone("docker", "cafef00d") is None
