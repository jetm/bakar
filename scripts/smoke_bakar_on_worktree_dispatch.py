#!/usr/bin/env python3
"""End-to-end smoke for building from a linked meta-avocado worktree.

Builds a real ``meta-avocado`` main checkout and a linked worktree under
``<root>/.worktrees/meta-avocado/wt1`` in a temporary directory, then drives
bakar's worktree resolution and a real ``kas dump`` of the generated wrapper.
The worktree's kas file pins a repository to a commit the main checkout's copy
does not name, so a wrapper that silently resolved to the main checkout is
caught by the dump rather than passing.

It also reads the worktree's release with ``bakar.steps.kas_pins.probe_release``
(skipped with a printed reason when kas is not on PATH) and classifies an
off-pin clone with ``bakar.steps.repo_reuse.classify``, which must report it as
``side`` without moving its HEAD.

The live steps (a local routed build, and dispatch to pc3 into the shared
release workspace) need a person and are printed as the manual remainder.

Usage:
    python3 scripts/smoke_bakar_on_worktree_dispatch.py
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SMOKE_LIB = Path.home() / ".claude" / "skills" / "ds-archive" / "scripts" / "smoke_lib.py"
_REEXEC_MARKER = "BAKAR_SMOKE_REEXEC"

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
GIT_IDENT = ["-c", "user.email=smoke@example.invalid", "-c", "user.name=smoke"]

WORKTREE_DIR = Path("/home/tiamarin/repos/work/.worktrees/meta-avocado/rpi5-lgpio-feed")
CENTRAL_DIR = Path("/home/tiamarin/repos/work/peridio")
RELEASE_WS = Path("/home/tiamarin/repos/work/peridio-wrynose")
BUILD_DIR_NAME = "build-raspberrypi5-rpi5-lgpio-feed"
RELEASE = "wrynose"
CHECKS_WITH_KAS = 7
KAS_FILE = WORKTREE_DIR / "kas" / "machine" / "raspberrypi5.yml"
MANUAL_STEPS_TEXT = (
    f'With `[workspaces] template = "/home/tiamarin/repos/work/peridio-{{release}}"` in ~/.config/bakar/config.toml, '
    f"run from {CENTRAL_DIR}: `bakar build {KAS_FILE} --target python3-lgpio` (local routed build). The doctor "
    f"must show the workspace as {RELEASE_WS}, kas-yaml-syntax must PASS naming {KAS_FILE} (never a temporary "
    f"wrapper), host-preflight must find the wrynose buildtools, the run directory must land under "
    f"{RELEASE_WS}/{BUILD_DIR_NAME}/build/runs/, and {RELEASE_WS}/{BUILD_DIR_NAME}/avocado-reuse.yml must exist.",
    "Install this bakar on PC1 and pc3 (the identity-parity check refuses a mismatch). Record a pc3 listing of "
    f"{RELEASE_WS} first: every top-level entry, and for every repository `git rev-parse HEAD`, `git branch -a`, "
    "`git status --porcelain` and `git remote get-url origin`. Then from "
    f"{CENTRAL_DIR} run `bakar build {KAS_FILE} --target python3-lgpio --on pc3 </dev/null`: it must complete "
    "without a prompt, and the printed plan must list only additions "
    f"({RELEASE_WS}/.bakar/worktrees/rpi5-lgpio-feed, refs/bakar/rpi5-lgpio-feed/<commit12>, {BUILD_DIR_NAME}) and "
    "no deletion. Re-take the pc3 listing: it must be unchanged except the new delivered worktree "
    f"(.bakar/worktrees/rpi5-lgpio-feed), the new .bakar entry and {BUILD_DIR_NAME}; every pre-existing "
    "repository keeps its HEAD, branches, status and origin URL. A person runs this because it writes into "
    "pc3's shared release workspace.",
)


def _ensure_project_python() -> None:
    """Re-run under the project environment when bakar's dependencies are missing.

    ``python3`` outside the venv cannot import bakar (it needs tomli_w and
    friends), so a bare ``python3`` invocation hands off to ``uv run`` once.
    """
    sys.path.insert(0, str(REPO / "src"))
    try:
        import yaml  # noqa: F401

        import bakar.feed  # noqa: F401
    except ImportError:
        if os.environ.get(_REEXEC_MARKER):
            raise
        os.environ[_REEXEC_MARKER] = "1"
        os.execvp("uv", ["uv", "run", "--project", str(REPO), "python", __file__, *sys.argv[1:]])


def _load_smoke_lib():
    spec = importlib.util.spec_from_file_location("smoke_lib", SMOKE_LIB)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {SMOKE_LIB}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_lib"] = module
    spec.loader.exec_module(module)
    return module


def git(*args: str) -> str:
    out = subprocess.run(
        ["git", *GIT_IDENT, *args],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    return out.stdout.strip()


def kas_yaml_text(pin_url: str, pin_commit: str) -> str:
    return (
        "header:\n  version: 16\nmachine: qemux86-64\n"
        "repos:\n  meta-avocado:\n    path: meta-avocado\n"
        f"  pinned:\n    url: {pin_url}\n    branch: main\n    commit: {pin_commit}\n"
    )


def release_kas_yaml_text(pin_url: str, pin_commit: str) -> str:
    """A kas file whose openembedded-core pin names the wrynose release."""
    return (
        "header:\n  version: 16\nmachine: qemux86-64\n"
        "repos:\n  meta-avocado:\n    path: meta-avocado\n"
        f"  openembedded-core:\n    url: {pin_url}\n    branch: {RELEASE}-avocado\n    commit: {pin_commit}\n"
    )


@dataclass(frozen=True)
class Layout:
    ws: Path
    main: Path
    worktree: Path
    kas_yaml: Path
    release_kas_yaml: Path
    main_pin: str
    worktree_pin: str
    pin_url: str
    offpin: Path


def build_layout(root: Path) -> Layout:
    """Create the pin source, the workspace, its meta-avocado and a linked worktree.

    The main checkout pins ``pinned`` at the first commit; only the worktree's
    branch moves the pin to the second commit and adds ``scripts/``.
    """
    pinsrc = root / "pinsrc"
    git("init", "-q", "-b", "main", str(pinsrc))
    (pinsrc / "layer.txt").write_text("one\n")
    git("-C", str(pinsrc), "add", "-A")
    git("-C", str(pinsrc), "commit", "-q", "-m", "one")
    main_pin = git("-C", str(pinsrc), "rev-parse", "HEAD")
    (pinsrc / "layer.txt").write_text("two\n")
    git("-C", str(pinsrc), "commit", "-q", "-am", "two")
    worktree_pin = git("-C", str(pinsrc), "rev-parse", "HEAD")
    pin_url = pinsrc.as_uri()

    ws = root / "ws"
    ws.mkdir()
    (ws / ".bakar.toml").write_text("")
    main = ws / "meta-avocado"
    (main / "kas" / "machine").mkdir(parents=True)
    git("init", "-q", "-b", "main", str(main))
    (main / "kas" / "machine" / "m.yml").write_text(kas_yaml_text(pin_url, main_pin))
    git("-C", str(main), "add", "-A")
    git("-C", str(main), "commit", "-q", "-m", "init")

    worktree = root / ".worktrees" / "meta-avocado" / "wt1"
    worktree.parent.mkdir(parents=True)
    git("-C", str(main), "worktree", "add", "-q", str(worktree), "-b", "wt1")
    (worktree / "kas" / "machine" / "m.yml").write_text(kas_yaml_text(pin_url, worktree_pin))
    (worktree / "kas" / "machine" / "rel.yml").write_text(release_kas_yaml_text(pin_url, worktree_pin))
    (worktree / "scripts").mkdir()
    (worktree / "scripts" / "render-pool-local.py").write_text("")
    git("-C", str(worktree), "add", "-A")
    git("-C", str(worktree), "commit", "-q", "-m", "worktree-only pin")

    # A shared clone sitting at the second commit while the pin names the first.
    offpin = ws / "offpin"
    git("clone", "-q", pin_url, str(offpin))

    return Layout(
        ws=ws,
        main=main,
        worktree=worktree,
        kas_yaml=worktree / "kas" / "machine" / "m.yml",
        release_kas_yaml=worktree / "kas" / "machine" / "rel.yml",
        main_pin=main_pin,
        worktree_pin=worktree_pin,
        pin_url=pin_url,
        offpin=offpin,
    )


def attempt(fn):
    """Return (value, None) or (None, error text) so one raising scenario cannot skip the rest."""
    try:
        return fn(), None
    except Exception as exc:  # noqa: BLE001 - every failure is reported as a FAIL, never swallowed
        return None, f"{type(exc).__name__}: {exc}"


def main() -> int:
    _ensure_project_python()
    import yaml

    from bakar.bsp_detect import detect_kas_workspace, linked_worktree_at, worktree_wrapper_text
    from bakar.feed import meta_avocado_scripts
    from bakar.steps.kas_pins import probe_release
    from bakar.steps.repo_reuse import classify

    smoke_lib = _load_smoke_lib()
    manual = [smoke_lib.ManualStep(reason="human", text=text) for text in MANUAL_STEPS_TEXT]
    have_kas = shutil.which("kas") is not None
    run = smoke_lib.SmokeRun(expected=CHECKS_WITH_KAS if have_kas else CHECKS_WITH_KAS - 1, manual_steps=manual)

    with run, smoke_lib.isolated_env("smoke-bakar-worktree-") as (tmp, env):
        root = tmp.resolve()
        lay = build_layout(root)
        ws, main_co, worktree, kas_yaml = lay.ws, lay.main, lay.worktree, lay.kas_yaml
        worktrees_dir = root / ".worktrees"

        # (a) the worktree is recognised and tied to its main checkout.
        lw, err = attempt(lambda: linked_worktree_at(worktree))
        ok = lw is not None and lw.checkout == worktree and lw.main_checkout == main_co
        run.check("linked_worktree_at returns the worktree and its main checkout", ok, err or f"got {lw!r}")

        # (b) the workspace is the main checkout's parent, never the .worktrees directory.
        got_ws, err = attempt(lambda: detect_kas_workspace(kas_yaml))
        ok = got_ws == ws and got_ws is not None and worktrees_dir not in (got_ws, *got_ws.parents)
        run.check("detect_kas_workspace returns the main checkout's parent", ok, err or f"got {got_ws}, want {ws}")

        # (c) scripts come from the worktree, which alone carries them.
        scripts, err = attempt(lambda: meta_avocado_scripts(kas_yaml))
        want = worktree / "scripts"
        run.check("meta_avocado_scripts returns the worktree's scripts", scripts == want, err or f"got {scripts}")

        # (d) the wrapper pins meta-avocado to the absolute worktree path.
        def wrapper_path() -> str:
            assert lw is not None, "no LinkedWorktree from scenario (a)"
            return yaml.safe_load(worktree_wrapper_text(lw, kas_yaml))["repos"]["meta-avocado"]["path"]

        pinned_path, err = attempt(wrapper_path)
        run.check(
            "worktree_wrapper_text pins repos.meta-avocado.path to the worktree",
            pinned_path == str(worktree),
            err or f"got {pinned_path!r}",
        )

        # (e) kas resolves the wrapper to the worktree's pin, not the main checkout's.
        def dump_pin() -> str:
            kas = shutil.which("kas")
            if kas is None:
                raise FileNotFoundError("kas is not on PATH")
            assert lw is not None, "no LinkedWorktree from scenario (a)"
            bsp_root = ws / "build-m"
            bsp_root.mkdir(exist_ok=True)
            wrapper = bsp_root / "avocado-wrapper.yml"
            wrapper.write_text(worktree_wrapper_text(lw, kas_yaml), encoding="utf-8")
            proc = subprocess.run(
                [kas, "dump", str(wrapper)],
                capture_output=True,
                text=True,
                env={**env, "KAS_WORK_DIR": str(ws)},
                cwd=bsp_root,
                stdin=subprocess.DEVNULL,
                timeout=300,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"kas dump exited {proc.returncode}: {proc.stderr.strip()[-500:]}")
            return yaml.safe_load(proc.stdout)["repos"]["pinned"]["commit"]

        pin, err = attempt(dump_pin)
        run.check(
            "kas dump of the wrapper exits 0 and carries the worktree-only pin",
            pin == lay.worktree_pin,
            err or f"got {pin}, want {lay.worktree_pin} (main checkout pins {lay.main_pin})",
        )

        # (f) the release is read from the pinned openembedded-core branch without touching any workspace.
        if have_kas:
            release, err = attempt(lambda: probe_release(lay.release_kas_yaml, worktree))
            run.check(
                f"probe_release returns the pinned release {RELEASE!r}",
                release == RELEASE,
                err or f"got {release!r}",
            )
        else:
            print("SKIP probe_release: kas is not on PATH, so the release cannot be read; expected lowered by one")

        # (g) an off-pin shared clone is routed to a side checkout and never moved.
        def classify_offpin() -> tuple[list, str, str]:
            before = git("-C", str(lay.offpin), "rev-parse", "HEAD")
            repos = {"offpin": {"url": lay.pin_url, "commit": lay.main_pin, "path": str(lay.offpin)}}
            states = classify(repos, ws)
            after = git("-C", str(lay.offpin), "rev-parse", "HEAD")
            return states, before, after

        result, err = attempt(classify_offpin)
        ok = False
        detail = err or ""
        if result is not None:
            states, before, after = result
            want_side = ws / ".bakar" / "pins" / "offpin" / lay.main_pin[:12]
            ok = (
                len(states) == 1
                and states[0].state == "side"
                and states[0].side_path == want_side
                and before == after == lay.worktree_pin
            )
            detail = f"states {states!r}, HEAD before {before}, after {after}"
        run.check("classify reports an off-pin repo as side and leaves its HEAD unchanged", ok, detail)

    return smoke_lib.finish(run)


if __name__ == "__main__":
    sys.exit(main())
