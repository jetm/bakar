"""Unit tests for bakar.steps.stress_parse.

Mocks ``run_shell_capture`` so the loop logic, signature scanning,
and ``summary.json`` aggregation can be exercised without a live
kas-container. Each test injects a controlled stdout payload into
the iteration log and checks the resulting summary structure.

The negative-path tests inject documented fork-race symptoms (drawn
from ``test_fork_race_signatures.POSITIVE_CASES``) so a regression
in either the signatures module or the scan glue would break here.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from bakar import build_stop
from bakar.config import BuildConfig
from bakar.observability import RunLogger
from bakar.steps import stress_parse

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

pytestmark = pytest.mark.unit


def _cfg(workspace: Path, family: str = "nxp") -> BuildConfig:
    """A container-mode cfg for the stress-parse command-shape tests.

    The host path injects BB_PYTHON3 and a generated ``host-mode.conf`` into
    the bitbake invocation; the assertions below spell out the container
    command exactly. ``test_host_mode_*`` covers the host spelling by
    overriding this via ``dataclasses.replace``.
    """
    if family == "ti":
        return BuildConfig(
            workspace=workspace,
            host_mode=False,
            bsp_family="ti",
            machine="am62x-var-som",
            distro="arago",
            image="var-thin-image",
            manifest="processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
            repo_url="https://example.invalid/none.git",
            repo_branch="scarthgap",
            kas_container_image="jetm/kas-build-env:latest",
        )
    return BuildConfig(
        workspace=workspace,
        host_mode=False,
        bsp_family="nxp",
        machine="imx95-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.12.49-2.2.0.xml",
        repo_url="https://example.invalid/none.git",
        repo_branch="walnascar",
        kas_container_image="jetm/kas-build-env:latest",
    )


def _bsp_stub() -> MagicMock:
    """Bare BspModel placeholder; stress_parse never reads its fields."""
    bsp = MagicMock()
    bsp.family = "nxp"
    return bsp


def _patch_run_shell_capture(monkeypatch: pytest.MonkeyPatch, payloads: list[str]) -> list[dict]:
    """Replace run_shell_capture with a stub that writes ``payloads[i]`` to disk.

    Returns the calls list so individual tests can assert on the
    composed in-container command and stdout path.
    """
    calls: list[dict] = []
    payload_iter = iter(payloads)

    def fake_capture(ctx, command, capture):
        stdout_path, step, python_executable = capture.stdout_path, capture.step, capture.python_executable
        body = next(payload_iter)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(body)
        calls.append(
            {
                "command": command,
                "stdout_path": stdout_path,
                "step": step,
                "kas_yaml": ctx.kas_yaml,
                "overlay_source": ctx.overlay_source,
                "python_executable": python_executable,
            }
        )
        return 0

    monkeypatch.setattr(stress_parse.step_kas, "run_shell_capture", fake_capture)
    return calls


def _patch_override_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub step_override.status so summaries do not depend on disk state."""
    fake = MagicMock()
    fake.state = "active"
    fake.branch = "br-2.12"
    fake.sha = "d092d2436"
    fake.upstream_version = "2.12.1"
    fake.bsp_version = "2.12.1"
    monkeypatch.setattr(stress_parse.step_override, "status", lambda cfg: fake)


def _clear_fork_race_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip env vars the fork-race forwarding logic injects into commands.

    Tests that pin exact command strings need a deterministic environment;
    leaking PYTHONMALLOC / BB_FORK_DIAG / MALLOC_ARENA_MAX / LD_PRELOAD from
    the dev shell would change the assertion target.
    """
    for name in ("BB_FORK_DIAG", "MALLOC_ARENA_MAX", "LD_PRELOAD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PYTHONMALLOC", raising=False)


def _benign_payload() -> str:
    return "\n".join(
        [
            "Loading cache: 100% |#######| Time: 0:00:00",
            "Loaded 4421 entries from dependency cache.",
            "Parsing recipes: 100% |#####| Time: 0:00:30",
            "Parsing of 1289 .bb files complete (0 cached, 1289 parsed).",
            "Summary: There was 1 INFO message shown.",
        ]
    )


def _variant_a_payload() -> str:
    return "\n".join(
        [
            "Loading cache: 100% |#######| Time: 0:00:00",
            "kernel: Parser-12[34567]: segfault at 10 ip 00007f8e6c4d8b3a sp 00007fff",
            "ERROR: ParseError in None: Not all recipes parsed, parser thread killed/died?",
        ]
    )


def _variant_b_payload() -> str:
    return "\n".join(
        [
            "Parsing recipes: 87% |####### | Time: 0:00:23",
            "SystemError: Type does not define the tp_name field",
        ]
    )


def test_all_iterations_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """10 benign parse runs produce passed=10, failed=0, no signatures."""
    cfg = _cfg(tmp_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    _patch_override_status(monkeypatch)
    _clear_fork_race_env(monkeypatch)
    payloads = [_benign_payload() for _ in range(10)]
    calls = _patch_run_shell_capture(monkeypatch, payloads)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        summary = stress_parse.run(
            ctx=stress_parse.StressParseContext(
                cfg=cfg,
                log=log,
                overlay_source=tmp_path / "bakar-tuning-nxp.yml",
                runs=10,
                target="world",
                parse_threads=None,
            )
        )

    assert summary["runs"] == 10
    assert summary["passed"] == 10
    assert summary["failed"] == 0
    assert summary["failure_signatures"] == []
    assert len(calls) == 10
    expected = "BB_ENV_PASSTHROUGH_ADDITIONS='PYTHONMALLOC' PYTHONMALLOC=malloc bitbake -p world"
    assert all(c["command"] == expected for c in calls)


def test_one_iteration_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Variant-A line in run 3 of 5 flips that iteration to failed."""
    cfg = _cfg(tmp_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    _patch_override_status(monkeypatch)
    payloads = [
        _benign_payload(),
        _benign_payload(),
        _variant_a_payload(),
        _benign_payload(),
        _benign_payload(),
    ]
    _patch_run_shell_capture(monkeypatch, payloads)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        summary = stress_parse.run(
            ctx=stress_parse.StressParseContext(
                cfg=cfg,
                log=log,
                overlay_source=tmp_path / "bakar-tuning-nxp.yml",
                runs=5,
                target="world",
                parse_threads=None,
            )
        )

    assert summary["passed"] == 4
    assert summary["failed"] == 1
    assert len(summary["failure_signatures"]) >= 2  # segfault + parser-thread-killed
    assert all(sig["run"] == 3 for sig in summary["failure_signatures"])


def test_summary_json_persisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """summary.json lands under stress-parse/ and round-trips through json."""
    cfg = _cfg(tmp_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    _patch_override_status(monkeypatch)
    _patch_run_shell_capture(monkeypatch, [_benign_payload()])

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        stress_parse.run(
            ctx=stress_parse.StressParseContext(
                cfg=cfg,
                log=log,
                overlay_source=tmp_path / "bakar-tuning-nxp.yml",
                runs=1,
                target="world",
                parse_threads=None,
            )
        )
        summary_path = log.run_dir / "stress-parse" / "summary.json"

    assert summary_path.is_file()
    payload = json.loads(summary_path.read_text())
    assert payload["bsp_family"] == "nxp"
    assert payload["target"] == "world"
    assert payload["runs"] == 1
    assert payload["override"]["state"] == "active"
    assert payload["override"]["branch"] == "br-2.12"


def test_parse_threads_prepended_to_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--parse-threads N renders as BB_NUMBER_PARSE_THREADS=N before bitbake -p."""
    cfg = _cfg(tmp_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    _patch_override_status(monkeypatch)
    _clear_fork_race_env(monkeypatch)
    calls = _patch_run_shell_capture(monkeypatch, [_benign_payload()])

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        stress_parse.run(
            ctx=stress_parse.StressParseContext(
                cfg=cfg,
                log=log,
                overlay_source=tmp_path / "bakar-tuning-nxp.yml",
                runs=1,
                target="world",
                parse_threads=1,
            )
        )

    assert (
        calls[0]["command"] == "BB_ENV_PASSTHROUGH_ADDITIONS='PYTHONMALLOC' PYTHONMALLOC=malloc "
        "BB_NUMBER_PARSE_THREADS=1 bitbake -p world"
    )


def test_custom_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--target image overrides the default 'world'."""
    cfg = _cfg(tmp_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    _patch_override_status(monkeypatch)
    _clear_fork_race_env(monkeypatch)
    calls = _patch_run_shell_capture(monkeypatch, [_benign_payload()])

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        stress_parse.run(
            ctx=stress_parse.StressParseContext(
                cfg=cfg,
                log=log,
                overlay_source=tmp_path / "bakar-tuning-nxp.yml",
                runs=1,
                target="core-image-minimal",
                parse_threads=None,
            )
        )

    assert (
        calls[0]["command"] == "BB_ENV_PASSTHROUGH_ADDITIONS='PYTHONMALLOC' PYTHONMALLOC=malloc "
        "bitbake -p core-image-minimal"
    )


def test_per_iteration_logs_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each iteration produces run-NN.log under stress-parse/."""
    cfg = _cfg(tmp_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    _patch_override_status(monkeypatch)
    _patch_run_shell_capture(monkeypatch, [_benign_payload() for _ in range(3)])

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        stress_parse.run(
            ctx=stress_parse.StressParseContext(
                cfg=cfg,
                log=log,
                overlay_source=tmp_path / "bakar-tuning-nxp.yml",
                runs=3,
                target="world",
                parse_threads=None,
            )
        )
        out_dir = log.run_dir / "stress-parse"

    assert (out_dir / "run-01.log").is_file()
    assert (out_dir / "run-02.log").is_file()
    assert (out_dir / "run-03.log").is_file()
    assert (out_dir / "summary.json").is_file()


def test_ti_bsp_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TI BuildConfig flows through to summary['bsp_family']='ti'."""
    cfg = _cfg(tmp_path, family="ti")
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    _patch_override_status(monkeypatch)
    _patch_run_shell_capture(monkeypatch, [_benign_payload()])

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        summary = stress_parse.run(
            ctx=stress_parse.StressParseContext(
                cfg=cfg,
                log=log,
                overlay_source=tmp_path / "bakar-tuning-nxp.yml",
                runs=1,
                target="world",
                parse_threads=None,
            )
        )

    assert summary["bsp_family"] == "ti"
    assert summary["machine"] == "am62x-var-som"
    assert "processor-sdk" in summary["manifest"]


def test_variant_b_signature_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CPython tp_name SystemError path also flips the run to failed."""
    cfg = _cfg(tmp_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    _patch_override_status(monkeypatch)
    _patch_run_shell_capture(monkeypatch, [_variant_b_payload()])

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        summary = stress_parse.run(
            ctx=stress_parse.StressParseContext(
                cfg=cfg,
                log=log,
                overlay_source=tmp_path / "bakar-tuning-nxp.yml",
                runs=1,
                target="world",
                parse_threads=None,
            )
        )

    assert summary["failed"] == 1
    assert any("tp_name" in sig["match"] for sig in summary["failure_signatures"])


def test_cache_cleared_between_iterations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each iteration removes both parse-cache directories so bitbake re-parses fresh.

    Without this, iterations 2..N are cache-hit walks with almost no
    actual recipe parsing - which means the fork-race code path is
    barely exercised. The summary records per-iteration cleanup state
    for forensic transparency.

    Both ``build/cache/`` (codeparser/unihash) and ``build/tmp/cache/``
    (per-multiconfig ``bb_cache.dat.<hash>``) must be wiped; missing
    the latter was the original cache-clear gap that let the
    dep-cache count creep up across iterations.
    """
    cfg = _cfg(tmp_path)
    cache_dir = cfg.bsp_root / "build" / "cache"
    tmp_cache_dir = cfg.bsp_root / "build" / "tmp" / "cache"
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "bb_codeparser.dat").write_text("stale-marker")
    (tmp_cache_dir / "bb_cache.dat.deadbeef").write_text("stale-mc-marker")
    _patch_override_status(monkeypatch)

    cleared_states: list[tuple[bool, bool]] = []

    def fake_capture(ctx, command, capture):
        stdout_path, step = capture.stdout_path, capture.step
        # Capture observed cache state at the moment kas-container would
        # have run; the implementation must clear it BEFORE invoking
        # capture, not after.
        cleared_states.append((not cache_dir.is_dir(), not tmp_cache_dir.is_dir()))
        # Re-create both caches so the next iteration has something to clear.
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "bb_codeparser.dat").write_text(f"warm-after-{step}")
        (tmp_cache_dir / "bb_cache.dat.deadbeef").write_text(f"warm-mc-{step}")
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(_benign_payload())
        return 0

    monkeypatch.setattr(stress_parse.step_kas, "run_shell_capture", fake_capture)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        summary = stress_parse.run(
            ctx=stress_parse.StressParseContext(
                cfg=cfg,
                log=log,
                overlay_source=tmp_path / "bakar-tuning-nxp.yml",
                runs=3,
                target="world",
                parse_threads=None,
            )
        )

    assert cleared_states == [(True, True), (True, True), (True, True)], (
        f"expected both caches absent at every iteration, got {cleared_states}"
    )
    assert summary["cache_cleared_pre_iter"] == [True, True, True]


def test_cache_clear_no_op_on_first_iteration_with_empty_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No build/cache/ on first run means cache_cleared_pre_iter[0] is False."""
    cfg = _cfg(tmp_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    _patch_override_status(monkeypatch)
    _patch_run_shell_capture(monkeypatch, [_benign_payload()])

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        summary = stress_parse.run(
            ctx=stress_parse.StressParseContext(
                cfg=cfg,
                log=log,
                overlay_source=tmp_path / "bakar-tuning-nxp.yml",
                runs=1,
                target="world",
                parse_threads=None,
            )
        )

    assert summary["cache_cleared_pre_iter"] == [False]
    assert summary["runtime_cleared_pre_iter"] == [False]


def test_runtime_cleared_between_iterations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each iteration removes bitbake.lock/sock and override __pycache__.

    A long-lived bitbake-cookerdaemon caches the bytecode it imported at
    startup, so source edits to ``upstream-bitbake/`` do not take effect
    until the daemon exits. Removing ``bitbake.lock`` + ``bitbake.sock``
    forces the next ``bitbake -p`` to spawn a fresh daemon. Wiping the
    override clone's ``__pycache__`` dirs additionally guarantees the new
    daemon compiles bytecode against the current source rather than
    re-loading a stale ``.pyc``.
    """
    cfg = _cfg(tmp_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    build_dir = cfg.bsp_root / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    lock_path = build_dir / "bitbake.lock"
    sock_path = build_dir / "bitbake.sock"
    lock_path.write_text("12345\n")
    sock_path.write_text("")

    override_root = cfg.bsp_root / "upstream-bitbake"
    pycache_a = override_root / "lib" / "bb" / "__pycache__"
    pycache_b = override_root / "lib" / "bb" / "server" / "__pycache__"
    pycache_a.mkdir(parents=True, exist_ok=True)
    pycache_b.mkdir(parents=True, exist_ok=True)
    (pycache_a / "cooker.cpython-312.pyc").write_text("stale-bytecode")
    (pycache_b / "process.cpython-312.pyc").write_text("stale-bytecode")

    _patch_override_status(monkeypatch)

    observed: list[tuple[bool, bool, bool, bool]] = []

    def fake_capture(ctx, command, capture):
        stdout_path, step = capture.stdout_path, capture.step
        observed.append(
            (
                lock_path.exists(),
                sock_path.exists(),
                pycache_a.exists(),
                pycache_b.exists(),
            )
        )
        # Re-create everything so the next iteration has work to do.
        lock_path.write_text("23456\n")
        sock_path.write_text("")
        pycache_a.mkdir(parents=True, exist_ok=True)
        pycache_b.mkdir(parents=True, exist_ok=True)
        (pycache_a / "cooker.cpython-312.pyc").write_text(f"stale-after-{step}")
        (pycache_b / "process.cpython-312.pyc").write_text(f"stale-after-{step}")
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(_benign_payload())
        return 0

    monkeypatch.setattr(stress_parse.step_kas, "run_shell_capture", fake_capture)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        summary = stress_parse.run(
            ctx=stress_parse.StressParseContext(
                cfg=cfg,
                log=log,
                overlay_source=tmp_path / "bakar-tuning-nxp.yml",
                runs=3,
                target="world",
                parse_threads=None,
            )
        )

    assert observed == [(False, False, False, False)] * 3, (
        f"expected lock/sock/pycache absent at every iteration, got {observed}"
    )
    assert summary["runtime_cleared_pre_iter"] == [True, True, True]


def test_env_payload_records_parse_threads_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """summary['env']['BB_NUMBER_PARSE_THREADS'] reflects --parse-threads."""
    cfg = _cfg(tmp_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    _patch_override_status(monkeypatch)
    _patch_run_shell_capture(monkeypatch, [_benign_payload()])
    monkeypatch.delenv("BB_NUMBER_PARSE_THREADS", raising=False)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        summary = stress_parse.run(
            ctx=stress_parse.StressParseContext(
                cfg=cfg,
                log=log,
                overlay_source=tmp_path / "bakar-tuning-nxp.yml",
                runs=1,
                target="world",
                parse_threads=8,
            )
        )

    assert summary["env"]["BB_NUMBER_PARSE_THREADS"] == "8"


def test_label_propagates_to_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-empty label arg lands in summary['label'] verbatim."""
    cfg = _cfg(tmp_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    _patch_override_status(monkeypatch)
    _clear_fork_race_env(monkeypatch)
    _patch_run_shell_capture(monkeypatch, [_benign_payload()])

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        summary = stress_parse.run(
            ctx=stress_parse.StressParseContext(
                cfg=cfg,
                log=log,
                overlay_source=tmp_path / "bakar-tuning-nxp.yml",
                runs=1,
                target="world",
                parse_threads=None,
                label="blog-py3.15-D-minimal",
            )
        )

    assert summary["label"] == "blog-py3.15-D-minimal"


def test_label_omitted_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When label is None (default), summary has no 'label' key.

    Keeping the key absent rather than null lets aggregators distinguish
    legacy summaries (pre-flag) from new ones unambiguously.
    """
    cfg = _cfg(tmp_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    _patch_override_status(monkeypatch)
    _clear_fork_race_env(monkeypatch)
    _patch_run_shell_capture(monkeypatch, [_benign_payload()])

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        summary = stress_parse.run(
            ctx=stress_parse.StressParseContext(
                cfg=cfg,
                log=log,
                overlay_source=tmp_path / "bakar-tuning-nxp.yml",
                runs=1,
                target="world",
                parse_threads=None,
            )
        )

    assert "label" not in summary


def test_python_executable_propagates_to_command_and_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--python <bin> sets BB_PYTHON3 inline on the command and records the path.

    Host mode is required - the BB_PYTHON3 inlining only happens when
    cfg.host_mode is True (kas-container builds set BB_PYTHON3 through
    the in-container Python install instead). Validates both that the
    command string carries the user-chosen interpreter and that
    summary.json records it for the audit trail.
    """
    cfg = dataclasses.replace(_cfg(tmp_path), host_mode=True)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    _patch_override_status(monkeypatch)
    _clear_fork_race_env(monkeypatch)
    calls = _patch_run_shell_capture(monkeypatch, [_benign_payload()])

    fake_python = tmp_path / "patched-python"
    fake_python.write_text("#!/bin/sh\nexit 0\n")
    fake_python.chmod(0o755)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        summary = stress_parse.run(
            ctx=stress_parse.StressParseContext(
                cfg=cfg,
                log=log,
                overlay_source=tmp_path / "bakar-tuning-nxp.yml",
                runs=1,
                target="world",
                parse_threads=None,
                python_executable=fake_python,
            )
        )

    assert f"BB_PYTHON3={fake_python}" in calls[0]["command"]
    assert calls[0]["python_executable"] == fake_python
    assert summary["python_executable"] == str(fake_python)


def test_peer_owned_lock_skips_unlink_but_cache_wipe_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A peer-held ``lock_mutation_guard`` refusal skips the lock/sock unlink.

    The lock/socket files must survive the call, while the parse-cache wipe
    (the stress test's actual purpose - forcing bitbake to re-parse) still
    runs unconditionally.
    """
    cfg = _cfg(tmp_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    build_dir = cfg.bsp_root / cfg.build_dir_name
    build_dir.mkdir(parents=True, exist_ok=True)
    lock_path = build_dir / "bitbake.lock"
    sock_path = build_dir / "bitbake.sock"
    lock_path.write_text("12345\n")
    sock_path.write_text("")
    cache_dir = cfg.bsp_root / "build" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "bb_codeparser.dat").write_text("stale-marker")

    refusal = build_stop.LockRefusal(reason="peer-held", host="peer-host", detail="lock marker names peer-host")
    monkeypatch.setattr(build_stop, "lock_mutation_guard", lambda _cfg: refusal)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        runtime_cleared = stress_parse._clear_bitbake_runtime(cfg, log, 1)
        cache_cleared = stress_parse._clear_parse_cache(cfg, log, 1)

    assert lock_path.exists()
    assert sock_path.exists()
    assert not runtime_cleared
    assert cache_cleared
    assert not cache_dir.is_dir()


def test_local_marker_lock_unlink_proceeds_as_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A safe (``None``) guard verdict leaves the lock/sock removal unchanged."""
    cfg = _cfg(tmp_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    build_dir = cfg.bsp_root / cfg.build_dir_name
    build_dir.mkdir(parents=True, exist_ok=True)
    lock_path = build_dir / "bitbake.lock"
    sock_path = build_dir / "bitbake.sock"
    lock_path.write_text("12345\n")
    sock_path.write_text("")

    monkeypatch.setattr(build_stop, "lock_mutation_guard", lambda _cfg: None)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        runtime_cleared = stress_parse._clear_bitbake_runtime(cfg, log, 1)

    assert not lock_path.exists()
    assert not sock_path.exists()
    assert runtime_cleared


def test_lock_removal_uses_build_dir_name_not_hardcoded_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """qcom's ``build-<distro>`` TOPDIR is used, not a hardcoded ``"build"``."""
    cfg = BuildConfig(
        workspace=tmp_path,
        bsp_family="qcom",
        machine="exmp-q911",
        distro="qcom-wayland",
        image="qcom-multimedia-image",
        manifest="qcom-manifest.xml",
        repo_url="https://example.invalid/none.git",
        repo_branch="scarthgap",
        kas_container_image="jetm/kas-build-env:latest",
    )
    assert cfg.build_dir_name == "build-qcom-wayland"
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    correct_build_dir = cfg.bsp_root / cfg.build_dir_name
    correct_build_dir.mkdir(parents=True, exist_ok=True)
    lock_path = correct_build_dir / "bitbake.lock"
    sock_path = correct_build_dir / "bitbake.sock"
    lock_path.write_text("12345\n")
    sock_path.write_text("")
    # A stray "build" dir must NOT be the one consulted.
    wrong_build_dir = cfg.bsp_root / "build"
    wrong_build_dir.mkdir(parents=True, exist_ok=True)
    (wrong_build_dir / "bitbake.lock").write_text("99999\n")

    monkeypatch.setattr(build_stop, "lock_mutation_guard", lambda _cfg: None)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        runtime_cleared = stress_parse._clear_bitbake_runtime(cfg, log, 1)

    assert not lock_path.exists()
    assert not sock_path.exists()
    assert (wrong_build_dir / "bitbake.lock").exists()
    assert runtime_cleared


def test_python_executable_omitted_when_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """summary.json has no 'python_executable' key when --python is not passed.

    Mirrors the label-key contract: aggregators distinguish legacy
    summaries (no --python plumbing) from new ones by key presence.
    """
    cfg = _cfg(tmp_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    _patch_override_status(monkeypatch)
    _clear_fork_race_env(monkeypatch)
    _patch_run_shell_capture(monkeypatch, [_benign_payload()])

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        summary = stress_parse.run(
            ctx=stress_parse.StressParseContext(
                cfg=cfg,
                log=log,
                overlay_source=tmp_path / "bakar-tuning-nxp.yml",
                runs=1,
                target="world",
                parse_threads=None,
            )
        )

    assert "python_executable" not in summary


def _run_two_iterations(cfg: BuildConfig, tmp_path: Path) -> None:
    with RunLogger(runs_dir=cfg.runs_dir) as log:
        stress_parse.run(
            ctx=stress_parse.StressParseContext(
                cfg=cfg,
                log=log,
                overlay_source=tmp_path / "bakar-tuning-nxp.yml",
                runs=2,
                target="world",
                parse_threads=None,
            )
        )


def _seed_between_iteration_state(
    cfg: BuildConfig, monkeypatch: pytest.MonkeyPatch, on_first_iteration: Callable[[], None]
) -> tuple[list[Path], list[dict]]:
    """Seed caches and lock files, and run ``on_first_iteration`` inside iteration 1."""
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    build_dir = cfg.bsp_root / cfg.build_dir_name
    build_dir.mkdir(parents=True, exist_ok=True)
    kept = [
        build_dir / "bitbake.lock",
        build_dir / "bitbake.sock",
        cfg.bsp_root / "build" / "cache" / "bb_codeparser.dat",
    ]
    for path in kept:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    _patch_override_status(monkeypatch)
    _clear_fork_race_env(monkeypatch)
    calls = _patch_run_shell_capture(monkeypatch, [_benign_payload(), _benign_payload()])
    stub_capture = stress_parse.step_kas.run_shell_capture

    def capture_then_appear(ctx, command, capture):
        rc = stub_capture(ctx, command, capture)
        if len(calls) == 1:
            on_first_iteration()
            for path in kept:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("rebuilt-by-iteration-1")
        return rc

    monkeypatch.setattr(stress_parse.step_kas, "run_shell_capture", capture_then_appear)
    return kept, calls


def test_peer_marker_between_iterations_stops_before_second_wipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marker naming a peer that appears after iteration 1 removes nothing in iteration 2."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("bakar.build_stop.socket.gethostname", lambda: "this-host")

    def peer_appears() -> None:
        build_stop.lock_marker_path(cfg).write_text("peer-host\n")

    kept, calls = _seed_between_iteration_state(cfg, monkeypatch, peer_appears)

    with pytest.raises(stress_parse.BuildLiveError, match="peer-host"):
        _run_two_iterations(cfg, tmp_path)

    assert len(calls) == 1
    assert all(p.read_text() == "rebuilt-by-iteration-1" for p in kept)


def test_live_local_build_between_iterations_stops_before_second_wipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live local build appearing after iteration 1 removes nothing in iteration 2."""
    cfg = _cfg(tmp_path)
    live: dict[str, int | None] = {"pid": None}
    monkeypatch.setattr(stress_parse.kas_lock, "local_build_holder", lambda _cfg: live["pid"])

    def build_appears() -> None:
        live["pid"] = 4242

    kept, calls = _seed_between_iteration_state(cfg, monkeypatch, build_appears)

    with pytest.raises(stress_parse.BuildLiveError, match="4242"):
        _run_two_iterations(cfg, tmp_path)

    assert len(calls) == 1
    assert all(p.read_text() == "rebuilt-by-iteration-1" for p in kept)


def test_clear_bitbake_runtime_keeps_lock_while_local_build_holds_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock/socket unlink consults ``local_build_holder`` as well as the marker guard."""
    cfg = _cfg(tmp_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    build_dir = cfg.bsp_root / cfg.build_dir_name
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "bitbake.lock").write_text("12345\n")
    (build_dir / "bitbake.sock").write_text("")
    monkeypatch.setattr(build_stop, "lock_mutation_guard", lambda _cfg: None)
    monkeypatch.setattr(stress_parse.kas_lock, "local_build_holder", lambda _cfg: 4242)

    with RunLogger(runs_dir=cfg.runs_dir) as log:
        assert not stress_parse._clear_bitbake_runtime(cfg, log, 1)

    assert (build_dir / "bitbake.lock").exists()
    assert (build_dir / "bitbake.sock").exists()


def test_one_shot_marker_read_error_between_iterations_is_reread_and_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single failed marker read before iteration 2 must not pass for "no owner"."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("bakar.build_stop.socket.gethostname", lambda: "this-host")
    monkeypatch.setattr("bakar.mounts.is_path_on_nfs", lambda _p: True)
    monkeypatch.setattr("bakar.steps.kas_lock._MARKER_REREAD_DELAY_S", 0)
    marker = build_stop.lock_marker_path(cfg)
    armed = {"fail_once": False}
    path_cls = type(marker)
    real_read_bytes = path_cls.read_bytes

    def flaky_read_bytes(self: Path) -> bytes:
        if self == marker and armed["fail_once"]:
            armed["fail_once"] = False
            raise OSError("transient")
        return real_read_bytes(self)

    monkeypatch.setattr(path_cls, "read_bytes", flaky_read_bytes)

    def peer_appears() -> None:
        marker.write_text("peer-host\n")
        armed["fail_once"] = True

    kept, calls = _seed_between_iteration_state(cfg, monkeypatch, peer_appears)

    with pytest.raises(stress_parse.BuildLiveError, match="peer-host"):
        _run_two_iterations(cfg, tmp_path)

    assert armed["fail_once"] is False  # the failing read really happened
    assert len(calls) == 1
    assert all(p.read_text() == "rebuilt-by-iteration-1" for p in kept)


@pytest.mark.parametrize(
    "case",
    ["peer-held", "unattributable", "shared-inaction", "live-record", "none"],
)
def test_command_gate_and_step_gate_return_equal_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Both gates are thin wrappers over ``kas_lock.removal_gate_verdict``."""
    import typer

    from bakar.commands import _helpers
    from bakar.steps import kas_lock

    cfg = _cfg(tmp_path)
    marker = build_stop.lock_marker_path(cfg)
    refusals = {
        "peer-held": build_stop.LockRefusal(reason="peer-held", host="pc2", marker=marker),
        "unattributable": build_stop.LockRefusal(reason="unattributable", marker=marker),
        "shared-inaction": build_stop.LockRefusal(reason="shared-inaction", marker=marker),
    }
    refusal = refusals.get(case)
    monkeypatch.setattr(build_stop, "lock_mutation_guard", lambda _cfg: refusal)
    monkeypatch.setattr(kas_lock, "local_build_holder", lambda _cfg: 4242 if case == "live-record" else None)
    monkeypatch.setattr(kas_lock, "_MARKER_REREAD_DELAY_S", 0)
    expected = kas_lock.removal_gate_verdict(cfg)

    refuses = case in ("peer-held", "live-record")
    assert expected.refuses is refuses
    try:
        stress_parse._refuse_if_build_live(cfg)
    except stress_parse.BuildLiveError as exc:
        assert exc.verdict == expected
        assert str(exc) == expected.label
        step_refused = True
    else:
        step_refused = False
    try:
        _helpers._refuse_if_build_live(cfg)
    except typer.Exit as exc:
        assert exc.exit_code == 1
        command_refused = True
    else:
        command_refused = False
    assert step_refused is command_refused is refuses


def test_clear_bitbake_runtime_skip_message_escapes_detail_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rich.text import Text

    cfg = _cfg(tmp_path)
    cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    verdict = stress_parse.kas_lock.GateVerdict(kind="warn", reason="pc[/x]", detail="bad [/x]")
    monkeypatch.setattr(stress_parse.kas_lock, "removal_gate_verdict", lambda _cfg: verdict)
    msgs: list[str] = []
    with RunLogger(runs_dir=cfg.runs_dir) as log:
        monkeypatch.setattr(log, "info", lambda msg, **_f: msgs.append(msg))
        stress_parse._clear_bitbake_runtime(cfg, log, 1)
    assert len(msgs) == 1
    assert Text.from_markup(msgs[0]).plain.endswith("(pc[/x]): bad [/x]")
