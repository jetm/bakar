"""Replace the BSP-bundled bitbake with a local upstream checkout.

The BSP ships its own bitbake. NXP carries it under the poky umbrella
at ``nxp/sources/poky/bitbake/``; TI consumes oe-core directly and
ships bitbake at the top of ``sources/`` as ``ti/sources/bitbake/``.
To test in-flight upstream fixes (e.g. the parser fork-race work
tracked in VARIS-13) without waiting for them to land in NXP/TI
releases, this step swaps that directory for a symlink to a
``--shared`` clone of the user's upstream bitbake repo.

Mechanics:

* The clone lives at ``<bsp_root>/upstream-bitbake/`` so it sits inside
  the kas-container bind mount (KAS_WORK_DIR=workspace/<bsp>).
  ``--shared`` reuses the source repo's object store, so commits made
  in the source appear here after a ``git fetch``.
* The symlink replaces the BSP-bundled bitbake dir with a relative link
  to ``upstream-bitbake/``. Depth differs by BSP - NXP resolves to
  ``../../upstream-bitbake`` (sources/poky/bitbake is two levels deep);
  TI resolves to ``../upstream-bitbake`` (sources/bitbake is one level
  deep). ``os.path.relpath`` computes the correct depth automatically.
  Either way the link resolves to ``/work/upstream-bitbake`` inside the
  container.
* The original BSP-bundled tree is removed - ``repo sync --force-sync``
  (NXP) or ``oe-layertool-setup.sh -r`` (TI) is the canonical path back
  if you ever need it.

Branch resolution (first match wins):

1. ``branch`` argument (CLI ``--branch``).
2. ``VARIS_BITBAKE_OVERRIDE_BRANCH`` env var.
3. Auto: read ``__version__`` from the BSP-bundled
   ``bitbake/lib/bb/__init__.py`` (e.g. ``2.12.1``) and compute
   ``br-<major>.<minor>`` (e.g. ``br-2.12``). The user maintains one
   branch per tracked Yocto/BSP bitbake major.minor.

The step is idempotent: if the symlink is already correct and the clone
is on the right branch with the latest tip, it no-ops. After every
manifest re-sync the BSP tree is back, and re-running ``apply`` re-swaps.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from varis_build.config import BuildConfig
    from varis_build.observability import RunLogger


DEFAULT_OVERRIDE_REPO = Path.home() / "repos" / "personal" / "yocto" / "bitbake"

_VERSION_RE = re.compile(r'^__version__\s*=\s*"(?P<ver>\d+\.\d+(?:\.\d+)?)"', re.MULTILINE)


@dataclass(frozen=True)
class OverrideStatus:
    """Snapshot of where the override currently stands.

    ``active`` means the BSP-bundled bitbake path is the symlink we
    expect AND the clone is checked out. ``stale`` means a real BSP
    directory is in place (post-resync, or never applied). ``disabled``
    means the user set ``VARIS_BITBAKE_OVERRIDE=0`` and we should leave
    it alone.

    ``poky_bitbake`` is the BSP-bundled bitbake path. The field name is
    historical (the override originally only ran for NXP, where the
    path lived under ``sources/poky/bitbake``). It now resolves to the
    BSP-aware path - ``sources/bitbake`` for TI, unchanged for NXP -
    but the field name is kept to avoid churning event log consumers.
    """

    state: str  # "active" | "stale" | "disabled" | "missing"
    branch: str | None
    sha: str | None
    upstream_version: str | None
    bsp_version: str | None
    poky_bitbake: Path
    upstream_dir: Path
    detail: str


def _bsp_bitbake(cfg: BuildConfig) -> Path:
    """BSP-bundled bitbake dir. NXP: ``sources/poky/bitbake``. TI: ``sources/bitbake``."""
    return cfg.bsp_bitbake_path


def _upstream_dir(cfg: BuildConfig) -> Path:
    return cfg.bsp_root / "upstream-bitbake"


def _override_repo() -> Path:
    raw = os.environ.get("VARIS_BITBAKE_OVERRIDE_REPO")
    return Path(raw).expanduser() if raw else DEFAULT_OVERRIDE_REPO


def _disabled() -> bool:
    return os.environ.get("VARIS_BITBAKE_OVERRIDE", "1") == "0"


def _read_bb_version(bitbake_dir: Path) -> str | None:
    """Return ``__version__`` from ``<bitbake_dir>/lib/bb/__init__.py``.

    Resolves through symlinks so calling on the swapped poky/bitbake/
    returns the upstream version, not the missing BSP one.
    """
    init = bitbake_dir / "lib" / "bb" / "__init__.py"
    try:
        text = init.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _VERSION_RE.search(text)
    return m.group("ver") if m else None


def _major_minor(version: str) -> str:
    parts = version.split(".")
    return f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else version


def _auto_branch(cfg: BuildConfig) -> str | None:
    """Compute ``br-<major>.<minor>`` from a bitbake checkout's version.

    Tries the BSP-bundled tree first, then the upstream clone (so the
    auto-detect still works after ``--revert`` removed the symlink but
    left the clone in place).
    """
    for path in (_bsp_bitbake(cfg), _upstream_dir(cfg)):
        version = _read_bb_version(path)
        if version is not None:
            return f"br-{_major_minor(version)}"
    return None


def resolve_branch(cfg: BuildConfig, override: str | None = None) -> str:
    if override:
        return override
    env_branch = os.environ.get("VARIS_BITBAKE_OVERRIDE_BRANCH")
    if env_branch:
        return env_branch
    auto = _auto_branch(cfg)
    if auto is None:
        raise RuntimeError(
            "could not auto-detect override branch: "
            f"{_bsp_bitbake(cfg) / 'lib/bb/__init__.py'} unreadable. "
            "Pass --branch or set VARIS_BITBAKE_OVERRIDE_BRANCH."
        )
    return auto


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _branch_exists(repo: Path, branch: str) -> tuple[bool, bool]:
    """Return (local_exists, remote_exists) for a branch in ``repo``."""
    local = _git(["rev-parse", "--verify", f"refs/heads/{branch}"], cwd=repo)
    remote = _git(["rev-parse", "--verify", f"refs/remotes/origin/{branch}"], cwd=repo)
    return local.returncode == 0, remote.returncode == 0


def _ensure_clone(
    upstream_dir: Path,
    source_repo: Path,
    branch: str,
) -> None:
    """Clone (first-time) or fetch+reset (subsequent) the upstream worktree.

    Uses ``--shared`` so the clone references the source repo's object
    store via ``objects/info/alternates``. That keeps disk usage low and
    means new commits in the source show up after ``git fetch``. The
    alternates path is outside the kas-container mount; bitbake at
    runtime does no git ops, so this is fine.
    """
    if not source_repo.is_dir():
        raise RuntimeError(f"override source repo missing: {source_repo}")

    local_in_src, remote_in_src = _branch_exists(source_repo, branch)
    if not (local_in_src or remote_in_src):
        raise RuntimeError(
            f"branch {branch!r} not found in {source_repo} "
            "(neither local nor origin/<branch>). Create it or pass --branch."
        )

    if not upstream_dir.exists():
        upstream_dir.parent.mkdir(parents=True, exist_ok=True)
        clone = _git(
            [
                "clone",
                "--shared",
                "--branch",
                branch,
                str(source_repo),
                str(upstream_dir),
            ]
        )
        if clone.returncode != 0:
            raise RuntimeError(f"git clone failed: {clone.stderr.strip()}")
        return

    fetch = _git(["fetch", "origin"], cwd=upstream_dir)
    if fetch.returncode != 0:
        raise RuntimeError(f"git fetch failed in {upstream_dir}: {fetch.stderr.strip()}")

    # After fetch, the clone's `origin/<branch>` ref reflects whatever
    # the source repo carries at <branch> (local or remote-tracking),
    # but the clone's local `<branch>` head is stale - fetch never
    # updates local heads. Always reset to the clone's own
    # `origin/<branch>` so new commits in the source land in the
    # checkout. Falling back to a non-prefixed `<branch>` reset would
    # silently re-pin to the stale local tip.
    local_in_clone, remote_in_clone = _branch_exists(upstream_dir, branch)
    reset_ref = f"origin/{branch}" if remote_in_clone else branch
    if local_in_clone:
        co = _git(["checkout", branch], cwd=upstream_dir)
    else:
        co = _git(["checkout", "-B", branch, reset_ref], cwd=upstream_dir)
    if co.returncode != 0:
        raise RuntimeError(f"git checkout {branch} failed: {co.stderr.strip()}")

    reset = _git(["reset", "--hard", reset_ref], cwd=upstream_dir)
    if reset.returncode != 0:
        raise RuntimeError(f"git reset --hard {reset_ref} failed: {reset.stderr.strip()}")


def _head_sha(repo: Path) -> str | None:
    out = _git(["rev-parse", "--short", "HEAD"], cwd=repo)
    return out.stdout.strip() if out.returncode == 0 else None


# Directives whose regex arity must match BSP call-site arity. Walnascar
# tripped this when bitbake.conf:831 calls `addfragments` with 3 tokens
# but upstream br-2.18 expanded the regex to require 4. Extend this
# tuple when new directive mismatches surface.
_CHECKED_DIRECTIVES: tuple[str, ...] = ("addfragments",)

_DIRECTIVE_REGEXP_RE = re.compile(
    r'__(?P<name>\w+)_regexp__\s*=\s*re\.compile\(\s*r"(?P<pat>[^"]+)"',
)

_GROUP_RE = re.compile(r"\(\.\+\??\)")


def _bsp_bitbake_conf(cfg: BuildConfig) -> Path:
    return cfg.bsp_bitbake_conf


def _override_conf_handler(upstream_dir: Path) -> Path:
    return upstream_dir / "lib" / "bb" / "parse" / "parse_py" / "ConfHandler.py"


def _override_directive_arity(handler_path: Path, directive: str) -> int | None:
    """Number of `(.+)`-style capture groups the override's regex for
    ``directive`` expects, or ``None`` if the directive isn't present.

    ConfHandler regexes for argument-style directives all follow the
    shape ``r"<name>\\s+(.+)\\s+(.+)..."`` so a textual count of
    ``(.+)``/``(.+?)`` groups is sufficient - we don't need to actually
    compile the regex.
    """
    if not handler_path.is_file():
        return None
    text = handler_path.read_text(errors="replace")
    for m in _DIRECTIVE_REGEXP_RE.finditer(text):
        if m.group("name") == directive:
            return len(_GROUP_RE.findall(m.group("pat")))
    return None


def _scan_directive_calls(conf_path: Path, directive: str) -> list[tuple[int, int]]:
    """Return ``(lineno, arg_count)`` for each call site of ``directive``
    in ``conf_path``. ``arg_count`` is the number of whitespace-split
    tokens after the directive keyword.
    """
    if not conf_path.is_file():
        return []
    out: list[tuple[int, int]] = []
    prefix = directive + " "
    for i, line in enumerate(conf_path.read_text(errors="replace").splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        tokens = stripped.split()[1:]
        out.append((i, len(tokens)))
    return out


def _check_parser_compat(
    cfg: BuildConfig,
    upstream_dir: Path,
    log: RunLogger | None,
) -> None:
    """Static compatibility check between override clone and BSP conf.

    For each entry in ``_CHECKED_DIRECTIVES``, compare the override's
    regex arity against actual call-site arity in the BSP's
    ``bitbake.conf``. Warns via ``log`` on mismatch; never raises - the
    override may be intentionally diverged and the user just wants
    awareness, not a hard fail.
    """
    if log is None:
        return
    handler = _override_conf_handler(upstream_dir)
    conf = _bsp_bitbake_conf(cfg)
    for directive in _CHECKED_DIRECTIVES:
        calls = _scan_directive_calls(conf, directive)
        if not calls:
            continue
        expected = _override_directive_arity(handler, directive)
        if expected is None:
            log.warn(
                f"override bitbake has no '{directive}' directive support, "
                f"but {conf} uses it at {len(calls)} site(s); parse will fail",
                directive=directive,
                file=str(conf),
                sites=[lineno for lineno, _ in calls],
            )
            continue
        for lineno, actual in calls:
            if actual != expected:
                log.warn(
                    f"override bitbake's '{directive}' regex expects {expected} args "
                    f"but {conf}:{lineno} passes {actual}; parse will fail",
                    directive=directive,
                    expected_args=expected,
                    actual_args=actual,
                    file=str(conf),
                    line=lineno,
                )


def _swap_to_symlink(poky_bitbake: Path, target: Path) -> tuple[str, str | None]:
    """Replace ``poky_bitbake`` with a relative symlink to ``target``.

    Returns ``(action, recorded_bsp_version)``:

    * ``"linked-fresh"`` - the symlink was already correct, no change.
    * ``"linked"`` - real directory was removed and replaced. The
      recorded BSP version is read before deletion so events.jsonl can
      record what we displaced.
    * ``"linked-broken"`` - existing symlink pointed elsewhere (or was
      dangling); replaced.
    """
    rel_target = Path(os.path.relpath(target, poky_bitbake.parent))

    if poky_bitbake.is_symlink():
        try:
            current = os.readlink(poky_bitbake)
        except OSError:
            current = ""
        if Path(current) == rel_target and target.is_dir():
            return "linked-fresh", None
        poky_bitbake.unlink()
        poky_bitbake.symlink_to(rel_target)
        return "linked-broken", None

    bsp_version: str | None = None
    if poky_bitbake.is_dir():
        bsp_version = _read_bb_version(poky_bitbake)
        shutil.rmtree(poky_bitbake)
    elif poky_bitbake.exists():
        # Some other file kind (extremely unlikely). Remove.
        poky_bitbake.unlink()
    poky_bitbake.symlink_to(rel_target)
    return "linked", bsp_version


def _is_correct_symlink(poky_bitbake: Path, upstream_dir: Path) -> bool:
    if not poky_bitbake.is_symlink():
        return False
    rel = Path(os.path.relpath(upstream_dir, poky_bitbake.parent))
    try:
        return Path(os.readlink(poky_bitbake)) == rel and upstream_dir.is_dir()
    except OSError:
        return False


def status(cfg: BuildConfig) -> OverrideStatus:
    poky_bitbake = _bsp_bitbake(cfg)
    upstream_dir = _upstream_dir(cfg)

    if _disabled():
        return OverrideStatus(
            state="disabled",
            branch=None,
            sha=None,
            upstream_version=None,
            bsp_version=_read_bb_version(poky_bitbake) if poky_bitbake.is_dir() else None,
            poky_bitbake=poky_bitbake,
            upstream_dir=upstream_dir,
            detail="VARIS_BITBAKE_OVERRIDE=0",
        )

    if not poky_bitbake.exists() and not upstream_dir.exists():
        return OverrideStatus(
            state="missing",
            branch=None,
            sha=None,
            upstream_version=None,
            bsp_version=None,
            poky_bitbake=poky_bitbake,
            upstream_dir=upstream_dir,
            detail="poky tree absent (pre-bootstrap)",
        )

    if _is_correct_symlink(poky_bitbake, upstream_dir):
        branch_out = _git(["symbolic-ref", "--short", "HEAD"], cwd=upstream_dir)
        branch = branch_out.stdout.strip() if branch_out.returncode == 0 else None
        return OverrideStatus(
            state="active",
            branch=branch,
            sha=_head_sha(upstream_dir),
            upstream_version=_read_bb_version(upstream_dir),
            bsp_version=None,
            poky_bitbake=poky_bitbake,
            upstream_dir=upstream_dir,
            detail="symlink ok",
        )

    if poky_bitbake.is_dir() and not poky_bitbake.is_symlink():
        detail = "poky/bitbake is a real directory (post-resync); run apply"
    elif poky_bitbake.is_symlink():
        detail = "poky/bitbake is a symlink pointing elsewhere; run apply"
    elif not poky_bitbake.exists():
        detail = "poky/bitbake missing (run apply or `varis build` to re-sync)"
    else:
        detail = "poky/bitbake exists but is neither a directory nor a symlink"
    return OverrideStatus(
        state="stale",
        branch=None,
        sha=None,
        upstream_version=None,
        bsp_version=_read_bb_version(poky_bitbake) if poky_bitbake.is_dir() else None,
        poky_bitbake=poky_bitbake,
        upstream_dir=upstream_dir,
        detail=detail,
    )


def apply(
    cfg: BuildConfig,
    log: RunLogger | None = None,
    *,
    branch: str | None = None,
    repo_path: Path | None = None,
) -> OverrideStatus:
    """Apply the override. Idempotent.

    Returns the post-apply :class:`OverrideStatus`. Caller is
    responsible for surfacing the result; this function emits structured
    events on ``log`` when one is supplied.
    """
    if _disabled():
        if log is not None:
            log.step_skip("bitbake_override", reason="VARIS_BITBAKE_OVERRIDE=0")
        return status(cfg)

    poky_bitbake = _bsp_bitbake(cfg)
    upstream_dir = _upstream_dir(cfg)
    source_repo = (repo_path or _override_repo()).expanduser().resolve()

    if not poky_bitbake.parent.is_dir():
        if log is not None:
            log.step_skip("bitbake_override", reason="poky/ missing (pre-bootstrap)")
        return status(cfg)

    target_branch = resolve_branch(cfg, override=branch)
    if log is not None:
        log.step_start(
            "bitbake_override",
            branch=target_branch,
            source_repo=str(source_repo),
        )

    _ensure_clone(upstream_dir, source_repo, target_branch)
    action, displaced_bsp_version = _swap_to_symlink(poky_bitbake, upstream_dir)
    _check_parser_compat(cfg, upstream_dir, log)

    upstream_version = _read_bb_version(upstream_dir)
    sha = _head_sha(upstream_dir)
    result = OverrideStatus(
        state="active",
        branch=target_branch,
        sha=sha,
        upstream_version=upstream_version,
        bsp_version=displaced_bsp_version,
        poky_bitbake=poky_bitbake,
        upstream_dir=upstream_dir,
        detail=action,
    )

    if log is not None:
        if (
            displaced_bsp_version
            and upstream_version
            and _major_minor(displaced_bsp_version) != _major_minor(upstream_version)
        ):
            log.warn(
                f"bitbake major.minor mismatch: BSP shipped {displaced_bsp_version}, override is {upstream_version}",
                bsp_version=displaced_bsp_version,
                upstream_version=upstream_version,
            )
        log.step_ok(
            "bitbake_override",
            action=action,
            branch=target_branch,
            sha=sha,
            upstream_version=upstream_version,
            bsp_version=displaced_bsp_version,
            source_repo=str(source_repo),
        )
    return result


def revert(cfg: BuildConfig, log: RunLogger | None = None) -> None:
    """Remove the symlink so the next ``repo sync --force-sync`` restores
    the BSP-bundled bitbake.

    We do not invoke ``repo sync`` here ourselves - the next ``varis
    build`` will detect the missing tree and force a re-sync via the
    existing workspace step. Keeping revert minimal avoids surprising
    network/I/O when the user just wanted to disable the override.
    """
    poky_bitbake = _bsp_bitbake(cfg)
    if poky_bitbake.is_symlink():
        poky_bitbake.unlink()
        if log is not None:
            log.info(
                f"removed symlink {poky_bitbake}; next `varis build` will restore the BSP-bundled bitbake via repo sync"
            )
    elif log is not None:
        log.info(f"{poky_bitbake} is not a symlink; nothing to revert")
