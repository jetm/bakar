"""Invariant guard: no unprotected kas command runs against a shared release workspace for a layer entry.

Every kas command bakar runs for a layer entry against a configured release
workspace must either carry the reuse overlay in its file list or run in a
throwaway work directory with ``repos_checkout``, ``finish_setup_repos`` and
``repos_apply_patches`` skipped. A plain ``kas dump`` with the workspace as its
work directory moves every clean repository to its pin, so one such call
anywhere in the layer path silently rewrites a clone other builds are using.

The guard records, at the subprocess boundary, every kas process a layer build,
a doctor run and the post-build graph capture spawn, and lets the real code run
behind it. A second half scans ``src/bakar`` for the spawn patterns and fails
when a call site is neither exercised by those scenarios nor on an allowlist
that says why a layer build never reaches it, so a kas call added later cannot
slip past unrecorded.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

import bakar
from bakar.config import BuildConfig
from bakar.diagnostics import Status, check_kas_yaml_syntax
from bakar.observability import RunLogger
from bakar.steps import kas_build, kas_graph_capture
from bakar.steps.kas_build import (
    GENERATED_BUILD_YAML,
    KasBuildContext,
    KasSubcommand,
    ShellCapture,
    graph_capture_command,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.unit

SRC_ROOT = Path(bakar.__file__).resolve().parent
_SRC_PREFIX = f"{SRC_ROOT}/"

REUSE_OVERLAY = "avocado-reuse.yml"
SKIPS = frozenset({"repos_checkout", "finish_setup_repos", "repos_apply_patches"})
KAS_SUBCOMMANDS = frozenset({"build", "dump", "shell", "checkout", "lock", "diff", "for-all-repos"})
CANNED_DUMP = "header:\n  version: 16\nmachine: m\ntarget: core-image-minimal\nrepos: {}\n"

# The spawn patterns the scan looks for: the executable chosen by host mode, and a
# literal argument list starting with "kas".
SPAWN_PATTERNS = (re.compile(r'exe = "kas"'), re.compile(r'\["kas",'))

# Call sites a layer build and a doctor run do not execute, each with the reason
# it cannot reach a shared workspace unprotected. Keys are "<path under
# src/bakar>::<function qualname>".
_BUILD_LAUNCH = (
    "launches kas against the generated self-contained dump (avocado-bakar.yml), which the protected "
    "layer dump wrote with the reuse overlay merged in; protected by construction"
)
_NOT_A_LAYER_PATH = "takes its own kas file; a layer build never reaches it"
_NO_SPAWN = "builds text for a message, preview or script; spawns nothing"
ALLOWLIST: dict[str, str] = {
    "steps/kas_build.py::run_build": f"the kas build launch: {_BUILD_LAUNCH}",
    "steps/kas_build.py::run_shell_live": f"the live `bakar bitbake` launch: {_BUILD_LAUNCH}",
    "commands/_build_common.py::_finish_build": f"names the kas executable in the failure message; {_NO_SPAWN}",
    "commands/show.py::_build_command_str": f"`bakar show` preview; {_NO_SPAWN}",
    "steps/kas_build.py::dry_run_preview_lines": f"dry-run preview; {_NO_SPAWN}",
    "steps/kas_build.py::_sync_step_lines": f"dry-run script text; {_NO_SPAWN}",
    "steps/kas_build.py::generate_dry_run_script": f"dry-run script text; {_NO_SPAWN}",
    "commands/dump.py::dump": (
        f"`bakar dump` names the kas executable in an error; its spawn is run_kas_subcommand, exercised "
        f"here. {_NOT_A_LAYER_PATH}"
    ),
    "commands/lock.py::lock": f"`bakar lock` BYO path; {_NOT_A_LAYER_PATH}",
    "commands/diff.py::diff": f"`bakar diff` read-only command; {_NOT_A_LAYER_PATH}",
    "diagnostics.py::check_kas_yaml_syntax.<locals>._dump": (
        "the plain `kas dump` of the non-layer doctor path; a layer entry returns through the "
        "throwaway-work-dir layer reader before it is defined"
    ),
}


# ---------------------------------------------------------------------------
# Static scan of the package for kas spawn sites
# ---------------------------------------------------------------------------


def _function_spans(tree: ast.AST) -> list[tuple[int, int, str]]:
    """Return (first line, last line, qualname) of every function, qualnames as ``co_qualname`` spells them."""
    spans: list[tuple[int, int, str]] = []

    def visit(node: ast.AST, qual: str, in_func: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                sep = ".<locals>." if in_func else "."
                name = f"{qual}{sep}{child.name}" if qual else child.name
                if not isinstance(child, ast.ClassDef):
                    spans.append((child.lineno, child.end_lineno or child.lineno, name))
                visit(child, name, in_func or not isinstance(child, ast.ClassDef))
            else:
                visit(child, qual, in_func)

    visit(tree, "", False)
    return spans


def discover_call_sites(root: Path) -> set[str]:
    """Return every ``<relative path>::<function qualname>`` under ``root`` matching a kas spawn pattern."""
    sites: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        hits = [no for no, line in enumerate(text.splitlines(), 1) if any(p.search(line) for p in SPAWN_PATTERNS)]
        if not hits:
            continue
        spans = _function_spans(ast.parse(text))
        rel = path.relative_to(root).as_posix()
        for hit in hits:
            enclosing = sorted((s for s in spans if s[0] <= hit <= s[1]), key=lambda s: s[1] - s[0])
            sites.add(f"{rel}::{enclosing[0][2] if enclosing else '<module>'}")
    return sites


def unaccounted_sites(sites: set[str], exercised: set[str], allowlist: dict[str, str]) -> list[str]:
    """Return the call sites that were neither exercised nor allowlisted."""
    return sorted(site for site in sites if site not in exercised and site not in allowlist)


# ---------------------------------------------------------------------------
# Classifying a recorded kas invocation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Invocation:
    scenario: str
    argv: tuple[str, ...]
    cwd: Path | None
    work_dir: str | None
    skips: frozenset[str]
    files: str | None
    subcommand: str | None


def _parse_kas_argv(argv: list[str]) -> tuple[str | None, frozenset[str], str | None]:
    """Return (subcommand, skipped steps, colon-joined file argument) of a kas argv."""
    sub_at = next((i for i, tok in enumerate(argv) if i > 0 and tok in KAS_SUBCOMMANDS), None)
    if sub_at is None:
        return None, frozenset(), None
    skips: list[str] = []
    files: str | None = None
    rest = argv[sub_at + 1 :]
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            break
        if tok == "--skip" and i + 1 < len(rest):
            skips.append(rest[i + 1])
            i += 2
            continue
        if not tok.startswith("-") and files is None:
            files = tok
        i += 1
    return argv[sub_at], frozenset(skips), files


def classify_invocation(inv: Invocation, workspace: Path, earlier: list[Invocation]) -> str | None:
    """Return why ``inv`` is protected, or None when it is not.

    Three ways to be protected, in the order they are tried:
    ``overlay``: the reuse overlay is in the file list, so no clean clone moves.
    ``throwaway``: all three setup steps are skipped and the work directory is a
    temporary directory outside the workspace.
    ``generated-dump``: the file is the self-contained dump a recorded
    overlay-protected dump wrote earlier, so its repositories carry no url to clone.
    """
    names = [Path(part).name for part in (inv.files or "").split(":") if part]
    if REUSE_OVERLAY in names:
        return "overlay"
    if inv.skips >= SKIPS and inv.work_dir:
        work = Path(inv.work_dir).resolve()
        if not work.is_relative_to(workspace.resolve()) and work.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            return "throwaway"
    if inv.files and Path(inv.files).name == GENERATED_BUILD_YAML:
        for prev in earlier:
            if (
                prev.subcommand == "dump"
                and prev.cwd is not None
                and classify_invocation(prev, workspace, []) == "overlay"
                and Path(inv.files).resolve() == (prev.cwd / GENERATED_BUILD_YAML).resolve()
            ):
                return "generated-dump"
    return None


# ---------------------------------------------------------------------------
# Recording at the subprocess boundary
# ---------------------------------------------------------------------------


def _is_kas(argv: object) -> bool:
    return isinstance(argv, list) and bool(argv) and Path(str(argv[0])).name in {"kas", "kas-container"}


@dataclass
class Recorder:
    """Records every kas spawn; anything else (git, the in-use holder) runs for real."""

    scenario: str = ""
    invocations: list[Invocation] = field(default_factory=list)
    exercised: set[str] = field(default_factory=set)

    def record(self, argv: list[str], kwargs: dict[str, Any]) -> None:
        sub, skips, files = _parse_kas_argv(argv)
        env = kwargs.get("env") or {}
        cwd = kwargs.get("cwd")
        self.invocations.append(
            Invocation(
                scenario=self.scenario,
                argv=tuple(argv),
                cwd=Path(cwd).resolve() if cwd else None,
                work_dir=env.get("KAS_WORK_DIR"),
                skips=skips,
                files=files,
                subcommand=sub,
            )
        )

    def profile(self, frame: Any, event: str, _arg: object) -> None:
        if event != "call":
            return
        code = frame.f_code
        filename = code.co_filename
        if filename.startswith(_SRC_PREFIX):
            self.exercised.add(f"{filename[len(_SRC_PREFIX) :]}::{code.co_qualname}")


@pytest.fixture
def recording(monkeypatch: pytest.MonkeyPatch) -> Iterator[Recorder]:
    rec = Recorder()
    real_run = subprocess.run
    real_popen = subprocess.Popen

    def spy_run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_kas(argv):
            rec.record(argv, kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout=CANNED_DUMP, stderr="")
        return real_run(argv, *args, **kwargs)

    def spy_popen(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_kas(argv):
            rec.record(argv, kwargs)
            # A harmless real child that prints the canned dump, so the caller's own
            # pipe, wait and pid handling all run against a live process.
            argv = [sys.executable, "-I", "-c", "import sys; sys.stdout.write(sys.argv[1])", CANNED_DUMP]
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    yield rec


# ---------------------------------------------------------------------------
# A fixture release workspace with a layer entry
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _make_workspace(tmp_path: Path) -> tuple[BuildConfig, Path, Path]:
    """Return (cfg, layer entry, overlay): a workspace whose meta-avocado clone sits at the entry's pin."""
    clone = tmp_path / "ws" / "meta-avocado"
    (clone / "kas").mkdir(parents=True)
    _git(clone, "init", "-q", "-b", "main")
    (clone / "kas" / "base.yml").write_text("header:\n  version: 16\ndistro: pinned\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-q", "-m", "pinned")
    pin = _git(clone, "rev-parse", "HEAD").strip()

    checkout = tmp_path / "layers" / "meta-board"
    (checkout / "kas").mkdir(parents=True)
    entry = checkout / "kas" / "m.yml"
    entry.write_text(
        "header:\n  version: 16\n  includes:\n    - repo: meta-avocado\n      file: kas/base.yml\n"
        f"machine: m\nrepos:\n  meta-board:\n  meta-avocado:\n    url: file://{clone}\n"
        f"    branch: main\n    commit: {pin}\n"
    )
    _git(checkout, "init", "-q", "-b", "main")

    cfg = BuildConfig(
        workspace=tmp_path / "ws",
        host_mode=True,
        bsp_family="generic",  # type: ignore[arg-type]
        machine="m",
        distro="generic",
        image="generic",
        manifest="",
        repo_url="",
        repo_branch="",
        kas_container_image="jetm/kas-build-env:latest",
        kas_yaml_override=entry,
    )
    cfg.bsp_root.mkdir(parents=True, exist_ok=True)
    # Host mode puts the bundled bitbake bin directory on the launch PATH and refuses without it.
    (cfg.workspace / "bitbake" / "bin").mkdir(parents=True, exist_ok=True)
    overlay = tmp_path / "overlay.yml"
    overlay.write_text("header:\n  version: 16\n", encoding="utf-8")
    return cfg, entry, overlay


@pytest.fixture
def layer_ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[BuildConfig, Path, Path]:
    monkeypatch.setenv("BAKAR_REPO_POLICY", "reuse")
    return _make_workspace(tmp_path)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def _run_all_scenarios(rec: Recorder, cfg: BuildConfig, entry: Path, overlay: Path, tmp_path: Path) -> None:
    rec.scenario = "build step dump"
    kas_build._build_kas_arg(cfg, entry, overlay)

    rec.scenario = "doctor"
    result = check_kas_yaml_syntax(cfg)
    assert result.status is Status.PASS, result.message

    with RunLogger(runs_dir=tmp_path / "runs") as log:
        ctx = KasBuildContext(cfg, log, entry, overlay)

        rec.scenario = "post-build graph capture: target resolution"
        target = kas_graph_capture._resolve_capture_target(ctx, log)
        assert target == "core-image-minimal"

        rec.scenario = "post-build graph capture: shell"
        rc = kas_build.run_shell_capture(
            ctx,
            graph_capture_command(target),
            ShellCapture(
                stdout_path=tmp_path / "depgraph.log",
                step="graph_capture",
                env_overrides={"SHELL": "/bin/bash"},
                timeout=30.0,
                isolate_process_group=True,
            ),
        )
        assert rc == 0

        rec.scenario = "kas subcommand"
        rc = kas_build.run_kas_subcommand(
            ctx,
            KasSubcommand(subcommand="dump", extra_args=[], capture_to=tmp_path / "dump.out"),
        )
        assert rc == 0

        rec.scenario = "shell"
        assert kas_build.run_shell(ctx, [], command="true") == 0


@pytest.fixture
def scenarios(
    recording: Recorder, layer_ws: tuple[BuildConfig, Path, Path], tmp_path: Path
) -> tuple[Recorder, BuildConfig]:
    cfg, entry, overlay = layer_ws
    # Profiling only spans the scenarios: left on, it would slow every test body that reads the result.
    sys.setprofile(recording.profile)
    try:
        _run_all_scenarios(recording, cfg, entry, overlay, tmp_path)
    finally:
        sys.setprofile(None)
    return recording, cfg


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


def _describe(inv: Invocation) -> str:
    return f"[{inv.scenario}] {' '.join(inv.argv)} (KAS_WORK_DIR={inv.work_dir}, cwd={inv.cwd})"


def _unprotected(rec: Recorder, workspace: Path) -> list[str]:
    bad: list[str] = []
    for i, inv in enumerate(rec.invocations):
        if classify_invocation(inv, workspace, rec.invocations[:i]) is None:
            bad.append(_describe(inv))
    return bad


def test_every_recorded_kas_invocation_is_protected(scenarios: tuple[Recorder, BuildConfig]) -> None:
    rec, cfg = scenarios

    assert _unprotected(rec, cfg.workspace) == []


def test_the_scenarios_record_every_kind_of_kas_call_the_layer_path_makes(
    scenarios: tuple[Recorder, BuildConfig],
) -> None:
    rec, cfg = scenarios
    kinds = {
        (inv.scenario, classify_invocation(inv, cfg.workspace, rec.invocations[:i]))
        for i, inv in enumerate(rec.invocations)
    }

    # A guard over an empty recording passes for the wrong reason.
    assert ("build step dump", "throwaway") in kinds  # the pin reader
    assert ("build step dump", "overlay") in kinds  # the build's own dump
    assert ("doctor", "throwaway") in kinds
    assert ("post-build graph capture: target resolution", "generated-dump") in kinds
    assert ("post-build graph capture: shell", "generated-dump") in kinds
    assert {inv.subcommand for inv in rec.invocations} >= {"dump", "shell"}


def test_the_generated_dump_inherits_a_url_less_declaration_of_every_reused_repository(
    scenarios: tuple[Recorder, BuildConfig],
) -> None:
    """Why a consumer of the generated dump counts as protected: kas has no url to clone or check out.

    The recorded kas output is canned, so this checks the half bakar controls: the
    reuse overlay merged into the dump declares each reused repository by path
    with its url cleared.
    """
    import yaml

    _rec, cfg = scenarios
    overlay = yaml.safe_load((cfg.bsp_root / REUSE_OVERLAY).read_text(encoding="utf-8"))

    assert overlay["repos"]
    for name, repo in overlay["repos"].items():
        assert repo.get("path"), name
        assert repo.get("url") is None, name


# ---------------------------------------------------------------------------
# The classifier has teeth
# ---------------------------------------------------------------------------


def _inv(files: str, *, work_dir: str | None, skips: frozenset[str] = frozenset(), sub: str = "dump") -> Invocation:
    return Invocation("synthetic", ("kas", sub, files), Path("/ws"), work_dir, skips, files, sub)


def test_a_dump_with_the_workspace_as_work_dir_and_no_overlay_is_unprotected(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()

    assert classify_invocation(_inv("m.yml", work_dir=str(ws)), ws, []) is None
    assert classify_invocation(_inv("w.yml:avocado-pin.yml", work_dir=str(ws)), ws, []) is None


def test_a_throwaway_dump_needs_all_three_skips_and_a_dir_outside_the_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "work").mkdir(parents=True)
    throwaway = tempfile.mkdtemp(prefix="bakar-kas-pins-work-")
    try:
        assert classify_invocation(_inv("w.yml", work_dir=throwaway, skips=SKIPS), ws, []) == "throwaway"
        partial = frozenset({"repos_checkout", "finish_setup_repos"})
        assert classify_invocation(_inv("w.yml", work_dir=throwaway, skips=partial), ws, []) is None
        assert classify_invocation(_inv("w.yml", work_dir=str(ws / "work"), skips=SKIPS), ws, []) is None
        assert classify_invocation(_inv("w.yml", work_dir=None, skips=SKIPS), ws, []) is None
    finally:
        Path(throwaway).rmdir()


def test_the_generated_dump_is_protected_only_after_an_overlay_protected_dump_wrote_it(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    generated = str(ws / GENERATED_BUILD_YAML)
    protecting = Invocation(
        "synthetic",
        ("kas", "dump", f"w.yml:{REUSE_OVERLAY}"),
        ws,
        str(ws),
        frozenset(),
        f"w.yml:{REUSE_OVERLAY}",
        "dump",
    )
    consumer = _inv(generated, work_dir=str(ws), sub="build")

    assert classify_invocation(consumer, ws, []) is None
    assert classify_invocation(consumer, ws, [protecting]) == "generated-dump"
    elsewhere = _inv(str(tmp_path / "other" / GENERATED_BUILD_YAML), work_dir=str(ws), sub="build")
    assert classify_invocation(elsewhere, ws, [protecting]) is None


def test_the_guard_flags_a_layer_build_whose_dump_loses_the_reuse_overlay(
    recording: Recorder, layer_ws: tuple[BuildConfig, Path, Path]
) -> None:
    """Breaking the code under test makes the guard fail, so a passing run is evidence and not vacuity."""
    cfg, entry, overlay = layer_ws

    with patch.object(kas_build, "_apply_reuse_states", return_value=Path("avocado-noreuse.yml")):
        kas_build._build_kas_arg(cfg, entry, overlay)

    bad = _unprotected(recording, cfg.workspace)
    assert len(bad) == 1
    assert "avocado-noreuse.yml" in bad[0]


# ---------------------------------------------------------------------------
# The scan: every kas call site is exercised or allowlisted
# ---------------------------------------------------------------------------


def test_the_scan_finds_the_known_kas_call_sites() -> None:
    sites = discover_call_sites(SRC_ROOT)

    assert sites
    for expected in (
        "steps/kas_build.py::_run_kas_dump",
        "steps/kas_build.py::run_build",
        "steps/kas_pins.py::_dump_argv",
        "diagnostics.py::check_kas_yaml_syntax.<locals>._dump",
    ):
        assert expected in sites


def test_every_kas_call_site_is_exercised_or_allowlisted(scenarios: tuple[Recorder, BuildConfig]) -> None:
    rec, _cfg = scenarios
    sites = discover_call_sites(SRC_ROOT)

    assert sites
    assert unaccounted_sites(sites, rec.exercised, ALLOWLIST) == []


def test_the_allowlist_names_only_call_sites_that_exist() -> None:
    sites = discover_call_sites(SRC_ROOT)

    assert sorted(set(ALLOWLIST) - sites) == []
    assert all(ALLOWLIST.values())


def test_a_new_kas_call_site_is_flagged_until_exercised_or_allowlisted(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "new_cmd.py").write_text(
        "import subprocess\n\n\ndef fresh_probe(cfg):\n    return subprocess.run(['x'], check=False)\n\n\n"
        "def spawn_literal(path):\n    return subprocess.run(['kas', 'dump', path], check=False)\n\n\n"
        "def spawn_host(cfg):\n    exe = 'kas'\n    exe = \"kas\" if cfg.host_mode else 'kas-container'\n"
        "    return subprocess.run([exe, 'build'], check=False)\n"
    )

    sites = discover_call_sites(pkg)

    assert sites == {"new_cmd.py::spawn_host"}
    assert unaccounted_sites(sites, set(), {}) == ["new_cmd.py::spawn_host"]
    assert unaccounted_sites(sites, {"new_cmd.py::spawn_host"}, {}) == []
    assert unaccounted_sites(sites, set(), {"new_cmd.py::spawn_host": "reason"}) == []


def test_the_scan_sees_a_literal_kas_argument_list(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "lit.py").write_text('import subprocess\n\n\ndef go(p):\n    return subprocess.run(["kas", "dump", p])\n')

    assert discover_call_sites(pkg) == {"lit.py::go"}


def test_a_nested_function_site_is_named_with_its_locals_qualname(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "nest.py").write_text(
        'import subprocess\n\n\ndef outer(p):\n    def inner():\n        return subprocess.run(["kas", "x"])\n'
        "    return inner\n"
    )

    assert discover_call_sites(pkg) == {"nest.py::outer.<locals>.inner"}
