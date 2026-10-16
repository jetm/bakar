"""Release-keyed workspace routing: pure helpers.

A meta-avocado kas build is routed to a per-release workspace (for example
``peridio-wrynose``) keyed by the Yocto release its ``openembedded-core`` pin
names. Everything here is a pure function over already-loaded data, so the
probe that produces the merged kas config and the callers that act on the
result live elsewhere.
"""

from __future__ import annotations

import re
from pathlib import Path

_OE_CORE = "openembedded-core"
_AVOCADO_SUFFIX = "-avocado"
_SERIES_ENV = "AVOCADO_YOCTO_SERIES"
DEFAULT_BUILD_DIR_TEMPLATE = "build-{stem}-{worktree}"
_BUILD_DIR_PLACEHOLDERS = frozenset({"stem", "worktree"})
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
_DELIVERED_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
_UNSAFE_CHAR_RE = re.compile(r"[^A-Za-z0-9._-]")


REPO_POLICY_ENV = "BAKAR_REPO_POLICY"
REPO_POLICY_KAS = "kas"
REPO_POLICY_REUSE = "reuse"
_REPO_POLICIES = (REPO_POLICY_KAS, REPO_POLICY_REUSE)


class ReleaseRoutingError(Exception):
    """A release could not be routed; the message is shown to the user as is."""


def parse_repo_policy(value: str | None) -> str:
    """Return the repository policy named by ``BAKAR_REPO_POLICY``'s ``value``.

    Unset or empty means ``kas``. Any other unknown value is refused: falling
    back to ``kas`` would let a typo move every clean clone of a shared
    workspace to its pin.
    """
    if not value:
        return REPO_POLICY_KAS
    if value not in _REPO_POLICIES:
        raise ReleaseRoutingError(
            f"BAKAR_REPO_POLICY={value!r} is not a repository policy; use one of {', '.join(_REPO_POLICIES)}"
        )
    return value


def release_key_from_merged(config: dict) -> str:
    """Return the release key (``wrynose``) from a merged kas config.

    The key is the ``branch:`` of the ``openembedded-core`` pin with its
    ``-avocado`` suffix removed. When the config also sets a non-empty
    ``env.AVOCADO_YOCTO_SERIES`` it must agree with that key.
    """
    repos = config.get("repos")
    entry = repos.get(_OE_CORE) if isinstance(repos, dict) else None
    if not isinstance(entry, dict):
        raise ReleaseRoutingError(f"the merged kas config has no {_OE_CORE} pin, so the release cannot be determined")
    branch = entry.get("branch")
    if not isinstance(branch, str) or not branch:
        raise ReleaseRoutingError(
            f"the {_OE_CORE} pin has no branch:, so the release cannot be determined (a commit-only pin is not enough)"
        )
    key = branch.removesuffix(_AVOCADO_SUFFIX)
    if key == branch or not key:
        raise ReleaseRoutingError(
            f"the {_OE_CORE} pin branch {branch!r} does not end in "
            f"{_AVOCADO_SUFFIX!r}, so the release cannot be determined"
        )
    env = config.get("env")
    series = env.get(_SERIES_ENV) if isinstance(env, dict) else None
    if isinstance(series, str) and series and series != key:
        raise ReleaseRoutingError(f"the {_OE_CORE} pin names release {key!r} but {_SERIES_ENV} is {series!r}")
    return key


def map_release(release: str, template: str | None, releases: dict[str, str] | None) -> Path | None:
    """Map a release key to its workspace path, or None when nothing is configured.

    A per-release entry wins over the template.
    """
    if releases and release in releases:
        raw = releases[release]
    elif template:
        raw = template.replace("{release}", release)
    else:
        return None
    return Path(raw).expanduser()


def worktree_build_dir_name(
    stem: str,
    worktree: str | None,
    template: str | None,
    *,
    source: str = "build.worktree_build_dir",
) -> str:
    """Return the build directory name for a kas file stem and worktree name.

    A main-checkout build (``worktree=None``) always uses ``build-{stem}``.
    The template is re-validated here because one arriving through
    ``BAKAR_WORKTREE_BUILD_DIR`` never passed the config loader's check.
    """
    if worktree is None:
        return f"build-{stem}"
    tmpl = template or DEFAULT_BUILD_DIR_TEMPLATE
    unknown = sorted(set(_PLACEHOLDER_RE.findall(tmpl)) - _BUILD_DIR_PLACEHOLDERS)
    if unknown:
        names = ", ".join("{" + u + "}" for u in unknown)
        raise ReleaseRoutingError(
            f"{source} {tmpl!r} uses unsupported placeholder(s) {names}; only {{stem}} and {{worktree}} are allowed"
        )
    return tmpl.replace("{stem}", stem).replace("{worktree}", worktree)


def sanitize_delivered_name(raw: str) -> str:
    """Turn a branch or directory name into a name safe for remote shell text."""
    name = _UNSAFE_CHAR_RE.sub("-", raw).strip("-.")
    if not name:
        raise ReleaseRoutingError(
            f"the name {raw!r} has no usable characters after sanitising; rename the worktree or branch"
        )
    return name


def is_valid_delivered_name(name: str) -> bool:
    """True when ``name`` consists only of ``[A-Za-z0-9._-]`` and is non-empty."""
    return _DELIVERED_NAME_RE.fullmatch(name) is not None
