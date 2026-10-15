#!/usr/bin/env python3
"""End-to-end smoke for building from a linked meta-avocado worktree.

Builds a real ``meta-avocado`` main checkout and a linked worktree under
``<root>/.worktrees/meta-avocado/wt1`` in a temporary directory, then drives
bakar's worktree resolution and a real ``kas dump`` of the generated wrapper.
The worktree's kas file pins a repository to a commit the main checkout's copy
does not name, so a wrapper that silently resolved to the main checkout is
caught by the dump rather than passing.

The live-remote steps (a real build from a worktree, and dispatch to pc3) need
a person and are printed as the manual remainder.

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
MANUAL_STEPS_TEXT = (
    f"From {WORKTREE_DIR}, run `bakar build kas/machine/raspberrypi5.yml --target python3-lgpio` and read the "
    "doctor output: kas-yaml-syntax must PASS and name the worktree kas file (not a wrapper or the main "
    "checkout), host-preflight must find the release-keyed buildtools, and the run directory must appear under "
    "/home/tiamarin/repos/work/peridio/build-raspberrypi5*/build/runs/ with nothing new under "
    "/home/tiamarin/repos/work/.worktrees/.",
    "Install this bakar on both nodes (the identity-parity check refuses a mismatch), then from the same worktree "
    "run `bakar build kas/machine/raspberrypi5.yml --target python3-lgpio --on pc3` without --yes and answer n: "
    "both `rsync --delete preview -> pc3:/home/tiamarin/repos/work/peridio` and `rsync --delete preview -> "
    f"pc3:{WORKTREE_DIR}` must print before one prompt and nothing may transfer. Re-run with --yes: exactly two "
    "transfers in that order, none for /home/tiamarin/repos/work/.worktrees or a sibling worktree, python3-lgpio "
    f"builds on pc3, and `ssh pc3 cat {WORKTREE_DIR}/.git` names a path that exists on pc3. Finally run the same "
    "command with an absolute kas path and `-w /home/tiamarin/repos/work/.worktrees`: it must exit non-zero "
    "before any rsync preview, naming the worktree and the root's size. Destructive rsync --delete into pc3's "
    "peridio tree, so a person runs it.",
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


@dataclass(frozen=True)
class Layout:
    ws: Path
    main: Path
    worktree: Path
    kas_yaml: Path
    main_pin: str
    worktree_pin: str


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
    (worktree / "scripts").mkdir()
    (worktree / "scripts" / "render-pool-local.py").write_text("")
    git("-C", str(worktree), "add", "-A")
    git("-C", str(worktree), "commit", "-q", "-m", "worktree-only pin")

    return Layout(
        ws=ws,
        main=main,
        worktree=worktree,
        kas_yaml=worktree / "kas" / "machine" / "m.yml",
        main_pin=main_pin,
        worktree_pin=worktree_pin,
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

    smoke_lib = _load_smoke_lib()
    manual = [smoke_lib.ManualStep(reason="human", text=text) for text in MANUAL_STEPS_TEXT]
    run = smoke_lib.SmokeRun(expected=5, manual_steps=manual)

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

    return smoke_lib.finish(run)


if __name__ == "__main__":
    sys.exit(main())
