"""Local package feed: root resolution, staging layout and repo-map parsing.

This module owns the post-build half of the package pipeline - the part that
turns a finished build's ``tmp/deploy/rpm`` into something ``avocado-cli`` and
``dnf`` can install from. Production reaches the same place through eight stages,
two of which exist only because Pulp is the cloud's content store; running
``createrepo_c`` over a staged tree substitutes for reading a Pulp publication,
which collapses the local pipeline to produce, stage, render, index, serve.

The rendering itself is NOT implemented here. ``meta-avocado``'s
``render-pool-local.py`` and ``repo-stage-rpms.sh`` are driven as subprocesses,
because the entire value of a local feed is fidelity to production and a forked
renderer loses fidelity *silently* - the feed still serves, dnf still resolves,
it simply stops matching production. meta-avocado's mirror is tracked against
the production renderer, so driving it puts bakar behind that tracking rather
than owning a third copy.

Two roots, deliberately separate:

- The **feed root** (``resolve_feed_root``) is served verbatim by a static file
  server, so everything beneath it is public.
- The **stage root** (``resolve_stage_root``) is pre-render input: staged RPMs
  with no repodata over them. It is derived from the feed root but always sits
  *outside* it, because serving a staged tree would advertise packages that no
  repository indexes.

The stage root is shared, and that is the single most load-bearing property in
this module. ``sdk/all`` is release-global, and the renderer runs
``createrepo_c`` over whatever staged tree it is handed - so a stage root
partitioned any more finely than the feed makes each render of that repository
REPLACE the previous one instead of unioning with it. The shared toolchain
repository then quietly shrinks to whatever was staged last, and nothing errors.
Deriving the stage root from the feed root is what keeps the two partitioned
identically no matter how the feed is configured.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bakar.bsp_detect import detect_kas_workspace

if TYPE_CHECKING:
    from pathlib import Path

    from bakar.config import BuildConfig

# Release and channel the feed renders into by default. Both are overridable
# because two releases are live in practice (jetson and raspberrypi5 on 2024,
# imx93-frdm on 2026) and their repository metadata must stay separate - one
# release's target resolving another's packages is the failure being avoided.
DEFAULT_RELEASE = "2024"
DEFAULT_CHANNEL = "edge"

# Suffix appended to the feed root's own name to derive the stage root. A suffix
# on the sibling rather than a subdirectory: `<feed>/_stage` would be inside the
# served tree.
_STAGE_SUFFIX = "-stage"

# Extension repositories are advertised by the index but never staged from a
# build - their content comes from `avocado ext package`. Staging one here would
# render an empty repository over whatever the extension flow already published.
_EXT_SUFFIX = "-ext"

# The map writes repo roots with a `$releasever/` prefix that the renderer
# substitutes. Carrying it into a subpath would create a literal `$releasever`
# directory in the feed.
_RELEASEVER_PREFIX = "$releasever/"


def resolve_feed_root(cfg: BuildConfig) -> Path:
    """Return the served root of the local package feed.

    Thin by design: where the feed lives is a configuration question that
    ``BuildConfig.effective_feed_dir`` already answers, and having one answer
    rather than two is the point.
    """
    return cfg.effective_feed_dir


def resolve_stage_root(cfg: BuildConfig) -> Path:
    """Return the staging root RPMs are copied into before rendering.

    Derived from the feed root as a sibling, so the two are always partitioned
    the same way: a per-workspace feed gets a per-workspace stage root, and a
    feed shared across workspaces gets a stage root shared with it. That
    equivalence is what makes the release-global repository accumulate rather
    than being overwritten - see the module docstring.

    Never a subdirectory of the feed root, because the feed root is served.
    """
    feed = resolve_feed_root(cfg)
    return feed.parent / f"{feed.name}{_STAGE_SUFFIX}"


def channel_root(
    feed_root: Path,
    *,
    release: str = DEFAULT_RELEASE,
    channel: str = DEFAULT_CHANNEL,
) -> Path:
    """Return ``<feed_root>/<release>/<channel>``.

    This is the directory the renderer treats as its channel root, so the
    content pool lands at ``<release>/<channel>/_pkgs`` - per release and
    channel, matching production. A single feed-wide pool was measured and
    rejected: the two live releases share zero byte-identical packages, so a
    global pool saves nothing while placing the pool at a depth production does
    not use.
    """
    return feed_root / release / channel


def meta_avocado_scripts(yaml_path: Path) -> Path:
    """Return the ``meta-avocado/scripts`` directory for a build's kas YAML.

    Resolved through :func:`bakar.bsp_detect.detect_kas_workspace`, which already
    walks up to the ``meta-avocado`` boundary for every build, so no new path
    logic is introduced here.

    Raises:
        FileNotFoundError: the directory is absent, with the path named. Raising
            beats returning None: without these scripts a sync renders nothing,
            and a sync that renders nothing while reporting success is worse
            than one that stops.
    """
    scripts = detect_kas_workspace(yaml_path) / "meta-avocado" / "scripts"
    if not scripts.is_dir():
        raise FileNotFoundError(
            f"meta-avocado scripts not found at {scripts}. The local feed drives "
            "meta-avocado's render-pool-local.py and repo-stage-rpms.sh rather "
            "than reimplementing them, so this checkout is required."
        )
    return scripts


def parse_repo_map(map_path: Path) -> list[str]:
    """Return the repository roots a build declares, in first-seen order.

    Reads the ``repo=`` lines of an ``avocado-repo.map``, which is the build's own
    statement of which repository each package belongs to. Repository membership
    is never inferred from directory names: the arch-keyed lines in the same file
    map an arch directory to a path *inside* a repository, and reading those as
    roots would render a repository per architecture.

    ``$releasever/`` is stripped and ``target/<machine>-ext`` entries are skipped.

    Raises:
        FileNotFoundError: the map is absent, with the path named.
    """
    if not map_path.is_file():
        raise FileNotFoundError(
            f"avocado-repo.map not found at {map_path}. It is written by the build "
            "into its RPM deploy directory; a build that has not produced one has "
            "nothing to stage."
        )

    roots: list[str] = []
    for raw in map_path.read_text().splitlines():
        line = raw.strip()
        if not line.startswith("repo="):
            continue
        root = line.removeprefix("repo=").removeprefix(_RELEASEVER_PREFIX).strip("/")
        if not root or root.endswith(_EXT_SUFFIX) or root in roots:
            continue
        roots.append(root)
    return roots
