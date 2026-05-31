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

from bakar.config import BuildConfig
from bakar.observability import RunLogger
from bakar.steps import stress_parse

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _cfg(workspace: Path, family: str = "nxp") -> BuildConfig:
    if family == "ti":
        return BuildConfig(
            workspace=workspace,
            bsp_family="ti",
            machine="am62x-var-som",
            distro="arago",
            image="var-thin-image",
            manifest="processor-sdk-scarthgap-chromium-11.00.09.04-config_var01.txt",
            repo_url="https://example.invalid/none.git",
            repo_branch="scarthgap",
            container_image="jetm/kas-build-env:latest",
        )
    return BuildConfig(
        workspace=workspace,
        bsp_family="nxp",
        machine="imx95-var-dart",
        distro="fsl-imx-xwayland",
        image="core-image-minimal",
        manifest="imx-6.12.49-2.2.0.xml",
        repo_url="https://example.invalid/none.git",
        repo_branch="walnascar",
        container_image="jetm/kas-build-env:latest",
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

    def fake_capture(
        ctx,
        command,
        stdout_path,
        *,
        step="kas_shell_capture",
        python_executable=None,
    ):
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
            cfg,
            log,
            bsp=_bsp_stub(),
            overlay_source=tmp_path / "bakar-tuning-nxp.yml",
            runs=10,
            target="world",
            parse_threads=None,
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
            cfg,
            log,
            bsp=_bsp_stub(),
            overlay_source=tmp_path / "bakar-tuning-nxp.yml",
            runs=5,
            target="world",
            parse_threads=None,
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
            cfg,
            log,
            bsp=_bsp_stub(),
            overlay_source=tmp_path / "bakar-tuning-nxp.yml",
            runs=1,
            target="world",
            parse_threads=None,
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
            cfg,
            log,
            bsp=_bsp_stub(),
            overlay_source=tmp_path / "bakar-tuning-nxp.yml",
            runs=1,
            target="world",
            parse_threads=1,
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
            cfg,
            log,
            bsp=_bsp_stub(),
            overlay_source=tmp_path / "bakar-tuning-nxp.yml",
            runs=1,
            target="core-image-minimal",
            parse_threads=None,
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
            cfg,
            log,
            bsp=_bsp_stub(),
            overlay_source=tmp_path / "bakar-tuning-nxp.yml",
            runs=3,
            target="world",
            parse_threads=None,
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
            cfg,
            log,
            bsp=_bsp_stub(),
            overlay_source=tmp_path / "bakar-tuning-nxp.yml",
            runs=1,
            target="world",
            parse_threads=None,
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
            cfg,
            log,
            bsp=_bsp_stub(),
            overlay_source=tmp_path / "bakar-tuning-nxp.yml",
            runs=1,
            target="world",
            parse_threads=None,
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

    def fake_capture(
        ctx,
        command,
        stdout_path,
        *,
        step="kas_shell_capture",
        python_executable=None,
    ):
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
            cfg,
            log,
            bsp=_bsp_stub(),
            overlay_source=tmp_path / "bakar-tuning-nxp.yml",
            runs=3,
            target="world",
            parse_threads=None,
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
            cfg,
            log,
            bsp=_bsp_stub(),
            overlay_source=tmp_path / "bakar-tuning-nxp.yml",
            runs=1,
            target="world",
            parse_threads=None,
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

    def fake_capture(
        ctx,
        command,
        stdout_path,
        *,
        step="kas_shell_capture",
        python_executable=None,
    ):
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
            cfg,
            log,
            bsp=_bsp_stub(),
            overlay_source=tmp_path / "bakar-tuning-nxp.yml",
            runs=3,
            target="world",
            parse_threads=None,
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
            cfg,
            log,
            bsp=_bsp_stub(),
            overlay_source=tmp_path / "bakar-tuning-nxp.yml",
            runs=1,
            target="world",
            parse_threads=8,
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
            cfg,
            log,
            bsp=_bsp_stub(),
            overlay_source=tmp_path / "bakar-tuning-nxp.yml",
            runs=1,
            target="world",
            parse_threads=None,
            label="blog-py3.15-D-minimal",
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
            cfg,
            log,
            bsp=_bsp_stub(),
            overlay_source=tmp_path / "bakar-tuning-nxp.yml",
            runs=1,
            target="world",
            parse_threads=None,
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
            cfg,
            log,
            bsp=_bsp_stub(),
            overlay_source=tmp_path / "bakar-tuning-nxp.yml",
            runs=1,
            target="world",
            parse_threads=None,
            python_executable=fake_python,
        )

    assert f"BB_PYTHON3={fake_python}" in calls[0]["command"]
    assert calls[0]["python_executable"] == fake_python
    assert summary["python_executable"] == str(fake_python)


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
            cfg,
            log,
            bsp=_bsp_stub(),
            overlay_source=tmp_path / "bakar-tuning-nxp.yml",
            runs=1,
            target="world",
            parse_threads=None,
        )

    assert "python_executable" not in summary
