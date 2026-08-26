"""``bakar build --cve``: produce the package-to-CVE report from a finished build.

``avocado-cve-report`` carries ``EXCLUDE_FROM_WORLD = "1"`` and joins cve-check
results across the WHOLE tree with pkgdata, so it is a second bitbake invocation
rather than a node in the image's graph. Wiring it as a dependency of the image
would run it alongside the ``do_cve_check`` tasks it reads and summarise a scan
still in progress - which the recipe's own strict mode would then fail on.

That is why this hangs off ``_finish_build`` the way ``--feed`` does: the
function raises on a non-zero rc before reaching its success path, so "only on a
build that actually happened" is structural rather than a guard each call site
remembers to write. The dry-run filter is separate and earlier, because
``run_build`` returns 0 after printing a preview - rc alone reads a dry run as a
success.

The skip-when-there-is-no-CVE-data test is the one carrying real weight. Without
it the flag pays a full kas startup to be told, by ``bb.fatal``, something a
``glob`` answers for free - and it tells the user only after their build looked
like it succeeded.
"""

from __future__ import annotations

import re
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from bakar import cve_report
from bakar.commands import build as build_mod

pytestmark = pytest.mark.unit

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

MACHINE = "avocado-qemux86-64"


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


@pytest.fixture
def cfg(tmp_path: Path) -> SimpleNamespace:
    """The minimum ``_finish_build`` and the report step read off a BuildConfig."""
    return SimpleNamespace(
        host_mode=True,
        resolved_tmpdir=tmp_path / "tmp",
        machine=MACHINE,
        workspace=tmp_path,
        kas_yaml=tmp_path / "machine.yml",
        effective_feed_dir=tmp_path / "_feed",
    )


@pytest.fixture
def log_stub(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        run_id="r0",
        run_dir=tmp_path / "run",
        start_monotonic=time.monotonic(),
        step_skip=lambda *_a, **_kw: None,
        info=lambda *_a, **_kw: None,
    )


@pytest.fixture
def kas_ctx(cfg, log_stub) -> object:
    """A context shaped like the one the build just ran with.

    The real type is used rather than a namespace: the report step derives its
    own context from this one with ``dataclasses.replace``, which rejects a
    field that does not exist. A stub would let a renamed field pass here and
    raise on the first real ``--cve`` build.
    """
    from bakar.steps.kas_build import KasBuildContext

    return KasBuildContext(cfg, log_stub, cfg.kas_yaml, Path("/overlay"), target=None)


@pytest.fixture
def cve_request(kas_ctx) -> object:
    """What ``--cve`` carries into ``_finish_build``: the context and its overlays."""
    return build_mod._CveRequest(kas_ctx=kas_ctx, extra_overlays=[])


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, dict]]:
    """Record every ``run_build`` the report step makes, without running kas.

    The kwargs are recorded alongside the context because ``run_build`` reads
    ``extra_overlays`` from its KEYWORD and never from the context's own field -
    so a context that carries them is not the same as a call that passes them.
    """
    recorded: list[tuple[object, dict]] = []

    def record(ctx, **kwargs):
        recorded.append((ctx, kwargs))
        return 0

    monkeypatch.setattr(build_mod.step_kas, "run_build", record)
    return recorded


def _contexts(recorded: list[tuple[object, dict]]) -> list[object]:
    return [ctx for ctx, _kwargs in recorded]


def _write_cve_data(cfg, machine: str = MACHINE) -> None:
    cve_dir = cve_report.cve_data_dir(cfg, machine)
    cve_dir.mkdir(parents=True, exist_ok=True)
    (cve_dir / "glibc_cve.json").write_text("{}", encoding="utf-8")


def test_failed_build_does_not_run_the_report(runs, cfg, log_stub, cve_request) -> None:
    """The report summarises what shipped, and a failed build shipped nothing.

    It would not merely be useless: cve-check results from the previous build
    survive in CVE_CHECK_DIR, so the report would be produced, look valid, and
    describe a package set this build never wrote.
    """
    _write_cve_data(cfg)

    with pytest.raises(typer.Exit) as exc:
        build_mod._finish_build(cfg, log_stub, 1, MACHINE, cve=cve_request)

    assert exc.value.exit_code == 1
    assert runs == []


def test_successful_build_runs_the_report_target(runs, cfg, log_stub, cve_request) -> None:
    _write_cve_data(cfg)

    build_mod._finish_build(cfg, log_stub, 0, MACHINE, cve=cve_request)

    assert [ctx.target for ctx in _contexts(runs)] == ["avocado-cve-report"]


def test_the_report_run_never_inherits_dry_run(runs, cfg, log_stub, kas_ctx) -> None:
    """A dry run is filtered before this point, so a ``True`` here could only be
    stale - and would print a preview while reporting the report as produced.

    The CVE data is written deliberately: without it the step skips, ``runs`` is
    empty, and an assertion over its contents holds vacuously no matter what the
    implementation does with ``dry_run``.
    """
    _write_cve_data(cfg)

    build_mod._finish_build(
        cfg,
        log_stub,
        0,
        MACHINE,
        cve=build_mod._CveRequest(kas_ctx=replace(kas_ctx, dry_run=True), extra_overlays=[]),
    )

    assert [ctx.dry_run for ctx in _contexts(runs)] == [False]


def test_the_report_run_gets_the_overlays_the_build_used(runs, cfg, log_stub, kas_ctx) -> None:
    """``run_build`` layers overlays from its KEYWORD, never from the context.

    Dropping them is not a cosmetic difference. ``kas/feature/cve-check.yml`` is
    what puts ``meta-avocado-sbom`` in bblayers, and a user stacks it with colon
    syntax - so a report run missing the overlays would be run against a config
    where ``avocado-cve-report`` is not a known target at all, on precisely the
    invocation the flag exists to serve.
    """
    _write_cve_data(cfg)
    overlays = [Path("/overlays/cve-check.yml"), Path("/overlays/tuning.yml")]

    build_mod._finish_build(
        cfg, log_stub, 0, MACHINE, cve=build_mod._CveRequest(kas_ctx=kas_ctx, extra_overlays=overlays)
    )

    assert [kwargs.get("extra_overlays") for _ctx, kwargs in runs] == [overlays]


def test_the_byo_path_hands_over_the_overlays_the_build_ran_with(
    runs, cfg, log_stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End of the same thread, one level up.

    ``_make_kas_ctx`` never populates ``KasBuildContext.extra_overlays``, so a
    request built from ``kas_ctx.extra_overlays`` is always empty and the report
    silently runs against the bare YAML. The two are indistinguishable at the
    ``_generate_cve_report`` boundary - both are lists - which is why the
    assertion has to reach the caller that chooses between them.
    """
    _write_cve_data(cfg)
    overlays = [Path("/overlays/cve-check.yml")]
    monkeypatch.setattr(build_mod, "_run_doctor_gate", lambda *_a, **_kw: None)

    build_mod._run_byo_build(
        cfg,
        log_stub,
        build_mod._BuildCtx(
            overlay_source=Path("/overlay.yml"),
            extra_overlays=overlays,
            bsp=None,
            family="generic",
            effective_show_layers=False,
            dry_run=False,
            keep_going=False,
            skip_sync=True,
            cve=True,
        ),
    )

    # Two runs: the build itself, then the report. Both get the same overlays.
    assert [kwargs.get("extra_overlays") for _ctx, kwargs in runs] == [overlays, overlays]
    assert [ctx.target for ctx in _contexts(runs)] == [None, "avocado-cve-report"]


def test_no_cve_data_skips_the_invocation_and_says_why(
    runs, cfg, log_stub, cve_request, capsys: pytest.CaptureFixture[str]
) -> None:
    """No cve-check inherited is the common miss, and it must not cost a kas start."""
    build_mod._finish_build(cfg, log_stub, 0, MACHINE, cve=cve_request)

    assert runs == []
    assert "cve-check" in _plain(capsys.readouterr().err)


def test_optout_markers_alone_do_not_count_as_cve_data(runs, cfg, log_stub, cve_request) -> None:
    """``avocado-cve-optout.bbclass`` writes into the same directory.

    A build where every recipe opted out has markers and no results, and the
    recipe fails on it - so it must read as "nothing scanned" here.
    """
    cve_dir = cve_report.cve_data_dir(cfg, MACHINE)
    cve_dir.mkdir(parents=True)
    (cve_dir / "packagegroup-base_optout.json").write_text("{}", encoding="utf-8")

    build_mod._finish_build(cfg, log_stub, 0, MACHINE, cve=cve_request)

    assert runs == []


def test_a_failing_report_does_not_fail_the_build(
    monkeypatch: pytest.MonkeyPatch, cfg, log_stub, cve_request, capsys: pytest.CaptureFixture[str]
) -> None:
    """The build succeeded and its artifacts are on disk. Turning a post-step
    failure into a non-zero build exit discards hours of work over something the
    user can repeat with one command, which the message names.
    """
    _write_cve_data(cfg)
    monkeypatch.setattr(build_mod.step_kas, "run_build", lambda ctx, **_kw: 1)

    build_mod._finish_build(cfg, log_stub, 0, MACHINE, cve=cve_request)

    assert "avocado-cve-report" in _plain(capsys.readouterr().err)


def test_the_report_is_produced_for_the_machine_that_was_built(runs, cfg, log_stub, cve_request) -> None:
    """``machine`` rather than ``cfg.machine``: on the bbsetup path they differ,
    and the report would then be looked for beside another machine's images.
    """
    other = "avocado-imx93-frdm"
    _write_cve_data(cfg, other)

    build_mod._finish_build(cfg, log_stub, 0, other, cve=cve_request)

    assert [ctx.target for ctx in _contexts(runs)] == ["avocado-cve-report"]


def test_no_request_runs_nothing(runs, cfg, log_stub) -> None:
    """The flag is opt-in; an ordinary build must not gain a second bitbake run."""
    _write_cve_data(cfg)

    build_mod._finish_build(cfg, log_stub, 0, MACHINE)

    assert runs == []


def test_dry_run_resolves_to_no_request(capsys: pytest.CaptureFixture[str]) -> None:
    """``run_build`` returns 0 after a preview, so the filter cannot live at rc."""
    assert build_mod._resolve_cve_request(cve=True, dry_run=True) is False
    assert "--cve" in _plain(capsys.readouterr().err)


def test_the_flag_off_resolves_to_no_request() -> None:
    assert build_mod._resolve_cve_request(cve=False, dry_run=False) is False


def test_the_flag_on_a_real_build_resolves_to_a_request() -> None:
    assert build_mod._resolve_cve_request(cve=True, dry_run=False) is True
