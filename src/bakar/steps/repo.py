"""repo-tool init and sync."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bakar.config import BuildConfig
    from bakar.observability import RunLogger


class RepoSyncError(RuntimeError):
    """A ``repo init`` or ``repo sync`` call exited non-zero."""

    def __init__(self, cmd: list[str], returncode: int) -> None:
        self.cmd = list(cmd)
        self.returncode = returncode
        super().__init__(f"{' '.join(self.cmd)} exited {returncode}")


def _run_repo(argv: list[str], log: RunLogger, **kwargs: object) -> None:
    try:
        subprocess.run(argv, check=True, **kwargs)  # type: ignore[call-overload]  # pragma: no cover
    except subprocess.CalledProcessError as exc:
        err = RepoSyncError(argv, exc.returncode)
        log.step_fail("repo_sync", reason=str(err), cmd=argv, returncode=exc.returncode)
        raise err from exc


def init_and_sync(
    cfg: BuildConfig,
    log: RunLogger,
    *,
    force_init: bool = False,
) -> None:
    """Run ``repo init`` + ``repo sync`` from ``cfg.workspace``.

    Calls ``repo init`` when ``.repo/`` is missing OR ``force_init`` is
    set. Callers pass ``force_init=True`` when the requested manifest or
    branch has changed since the last sync - ``repo init`` then rewrites
    ``.repo/manifest.xml`` to point at the new manifest, and the
    subsequent ``repo sync`` reshapes ``sources/`` to match.

    Whenever this function runs (init or sync), ``build/conf/`` is
    wiped so ``setup_env`` regenerates ``bblayers.conf`` against the
    current layer set. Sync alone (without init) can still move layer
    SHAs across branches - the prior bblayers.conf is not guaranteed
    to parse against the new tree, so rebuild it.

    Raises ``RepoSyncError`` on ``repo`` failure (after recording ``step_fail``).
    """
    log.step_start(
        "repo_sync",
        manifest=cfg.manifest,
        branch=cfg.repo_branch,
        force_init=force_init,
    )
    root = cfg.workspace / cfg.workspace_subdir
    repo_dir = root / ".repo"
    need_init = force_init or not repo_dir.is_dir()
    if need_init:
        init_argv = [
            "repo",
            "init",
            "-u",
            cfg.repo_url,
            "-b",
            cfg.repo_branch,
            "-m",
            cfg.manifest,
        ]
        # --config-name is a Variscite/NXP-specific repo init flag; the
        # qcom (and any other) manifest layout does not use it.
        if cfg.bsp_family == "nxp":
            init_argv.append("--config-name")
        _run_repo(init_argv, log, cwd=root, stdin=subprocess.DEVNULL)
    # Wipe build/conf/ on every sync (init or drift). bblayers.conf
    # rendered against the old tree may reference layer state that has
    # moved under the sync - setup_env regenerates it correctly.
    build_conf = root / "build" / "conf"
    if build_conf.is_dir():
        shutil.rmtree(build_conf)
    nproc = os.environ.get("NPROC", str(os.cpu_count() or 8))
    _run_repo(["repo", "sync", "-j", nproc, "--force-sync", "--no-clone-bundle"], log, cwd=root)
    count = len(list((root / "sources").iterdir())) if (root / "sources").is_dir() else 0
    log.step_ok("repo_sync", repo_count=count)
