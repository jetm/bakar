"""Release-keyed workspace routing: pure helpers.

A meta-avocado kas build is routed to a per-release workspace (for example
``peridio-wrynose``) keyed by the Yocto release its ``openembedded-core`` pin
names. Everything here is a pure function over already-loaded data, so the
probe that produces the merged kas config and the callers that act on the
result live elsewhere.
"""

from __future__ import annotations

import os
import re
import string
from pathlib import Path

_OE_CORE = "openembedded-core"
_AVOCADO_SUFFIX = "-avocado"
_SERIES_ENV = "AVOCADO_YOCTO_SERIES"
DEFAULT_BUILD_DIR_TEMPLATE = "build-{stem}-{worktree}"
_BUILD_DIR_PLACEHOLDERS = frozenset({"stem", "worktree"})
_DELIVERED_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
_UNSAFE_CHAR_RE = re.compile(r"[^A-Za-z0-9._-]")
# A name ending in one of these is either an invalid git ref component (``.lock``)
# or clashes with the per-name lock files the delivery script keeps beside the
# worktree (``.<name>.lock`` of ``a.build`` is ``.a.build.lock``, the build lock of ``a``).
_RESERVED_NAME_SUFFIXES = (".lock", ".build")


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
    if "/" in key or key in {".", ".."}:
        raise ReleaseRoutingError(
            f"the {_OE_CORE} pin branch {branch!r} gives release {key!r}, which is not usable as a directory name"
        )
    env = config.get("env")
    series = env.get(_SERIES_ENV) if isinstance(env, dict) else None
    if isinstance(series, str) and series and series != key:
        raise ReleaseRoutingError(f"the {_OE_CORE} pin names release {key!r} but {_SERIES_ENV} is {series!r}")
    return key


def check_template(label: str, value: str, allowed: frozenset[str]) -> None:
    """Raise ReleaseRoutingError naming ``label`` unless ``value`` is a valid template.

    A valid template uses only bare placeholders from ``allowed``: no format
    spec, no conversion, no attribute or index access, no malformed braces. The
    config loader and the runtime both call this, so a template cannot load and
    then fail every build, or the reverse.
    """
    try:
        fields = list(string.Formatter().parse(value))
    except ValueError as exc:
        raise ReleaseRoutingError(f"'{label}' is not a valid template ({exc}): {value!r}") from None
    allowed_text = ", ".join(f"{{{n}}}" for n in sorted(allowed))
    for _literal, name, spec, conversion in fields:
        if name is None:
            continue
        if name not in allowed:
            raise ReleaseRoutingError(f"'{label}' may only use {allowed_text}, got {{{name}}} in {value!r}")
        if spec or conversion:
            raise ReleaseRoutingError(
                f"'{label}' placeholders take no format spec or conversion, got {value!r}; use plain {allowed_text}"
            )


def check_dir_name(label: str, name: str) -> str:
    """Return ``name`` after checking it is one directory name, not a path.

    A rendered build-directory name is joined onto the workspace, so a separator,
    ``.``, ``..`` or an empty value would place it elsewhere (an absolute name
    replaces the workspace outright).
    """
    if not name or name in {".", ".."} or "/" in name or "\0" in name:
        raise ReleaseRoutingError(f"{label} renders {name!r}, which is not a single directory name")
    return name


def check_absolute_path(label: str, raw: str) -> Path:
    """Return ``raw`` as a ``Path`` after checking it is non-empty and absolute.

    For a path on ANOTHER host: ``~`` is refused rather than expanded, because
    it would resolve to this host's home. A path on this host goes through
    :func:`_local_absolute`, which expands ``~``.
    """
    if not raw or not raw.startswith("/"):
        raise ReleaseRoutingError(f"'{label}' must be an absolute path, got {raw!r}")
    return Path(raw)


def _local_absolute(label: str, raw: str) -> Path:
    path = Path(raw).expanduser()
    if not raw or not path.is_absolute():
        raise ReleaseRoutingError(f"'{label}' must be an absolute path, got {raw!r}")
    return path


def map_release(release: str, template: str | None, releases: dict[str, str] | None) -> Path | None:
    """Map a release key to its workspace path, or None when nothing is configured.

    A per-release entry wins over the template. The result is always absolute.
    """
    if releases and release in releases:
        return _local_absolute(f"workspaces.releases.{release}", releases[release])
    if template:
        check_template("workspaces.template", template, frozenset({"release"}))
        return _local_absolute("workspaces.template", template.format(release=release))
    return None


def _template_regex(template: str, field: str = "[^/]+") -> str:
    # Formatter.parse unescapes ``{{``/``}}`` in the literal pieces, so the
    # pattern matches what ``str.format`` (and therefore ``map_release``) renders.
    pieces: list[str] = []
    for literal, name, _spec, _conversion in string.Formatter().parse(template):
        pieces.append(re.escape(literal))
        if name is not None:
            pieces.append(field)
    return "".join(pieces)


def _has_field(segment: str) -> bool:
    return any(name is not None for _lit, name, _spec, _conv in string.Formatter().parse(segment))


def _unescape(literal_template: str) -> str:
    return "".join(lit for lit, _name, _spec, _conv in string.Formatter().parse(literal_template))


def _instances_under(real_dir: str, segment: str, rest: str) -> set[str]:
    """Realpaths of every existing workspace the template names under ``real_dir``.

    Resolving the literal prefix cannot see a symlink at or after the
    ``{release}`` segment (``peridio-wrynose -> /data/peridio-wrynose``), because
    the release is unknown until a directory name supplies it.
    """
    segment_re = re.compile(_template_regex(segment, "([^/]+)"))
    try:
        names = os.listdir(real_dir)
    except OSError:
        return set()
    found: set[str] = set()
    for name in names:
        match = segment_re.fullmatch(name)
        if match is None or not match.groups():
            continue
        rendered = os.path.join(real_dir, (segment + rest).format(release=match.group(1)))
        found.add(os.path.realpath(rendered))
    return found


def _template_matches(spellings: set[str], template: str) -> bool:
    normalised = os.path.normpath(os.path.expanduser(template))
    if any(re.fullmatch(_template_regex(normalised), s) for s in spellings):
        return True
    if not normalised.startswith("/"):
        return False
    segments = normalised.split("/")
    index = next((i for i, seg in enumerate(segments) if _has_field(seg)), len(segments))
    # The literal directory prefix is resolved because the ``-w`` callback has
    # already resolved the workspace, so a symlink anywhere in it would otherwise
    # make a configured release workspace look unconfigured.
    real_prefix = os.path.realpath(_unescape("/".join(segments[:index])) or "/")
    if index == len(segments):
        return real_prefix in spellings
    segment = segments[index]
    rest = "".join("/" + seg for seg in segments[index + 1 :])
    real_pattern = re.escape(real_prefix.rstrip("/")) + "/" + _template_regex(segment + rest)
    if any(re.fullmatch(real_pattern, s) for s in spellings):
        return True
    return bool(_instances_under(real_prefix, segment, rest) & spellings)


def matches_release_workspace(path: Path, template: str | None, releases: dict[str, str] | None) -> bool:
    """True when ``path`` is a workspace the ``[workspaces]`` configuration names.

    Compares the path as spelled and with symlinks resolved, against every
    per-release entry and against the template with ``{release}`` standing for
    one path component, both as written and with symlinks in it resolved. Needs
    no kas dump, so it can run before any build.
    """
    spellings = {os.path.normpath(path.expanduser()), os.path.realpath(path.expanduser())}
    if releases:
        for raw in releases.values():
            configured = Path(raw).expanduser()
            if {os.path.normpath(configured), os.path.realpath(configured)} & spellings:
                return True
    if template:
        try:
            check_template("workspaces.template", template, frozenset({"release"}))
        except ReleaseRoutingError:
            return False
        return _template_matches(spellings, template)
    return False


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
    ``BAKAR_WORKTREE_BUILD_DIR`` never passed the config loader's check, and the
    rendered name must be one directory name.
    """
    if worktree is None:
        return f"build-{stem}"
    tmpl = template or DEFAULT_BUILD_DIR_TEMPLATE
    check_template(source, tmpl, _BUILD_DIR_PLACEHOLDERS)
    return check_dir_name(f"{source} {tmpl!r}", tmpl.format(stem=stem, worktree=worktree))


def _name_problem(name: str) -> str | None:
    if ".." in name:
        return "contains '..', which git refuses in a ref name"
    for suffix in _RESERVED_NAME_SUFFIXES:
        if name.endswith(suffix):
            return f"ends in {suffix!r}, which clashes with a git ref or a bakar lock file name"
    return None


def sanitize_delivered_name(raw: str) -> str:
    """Turn a branch or directory name into a name safe for remote shell text and a git ref."""
    name = _UNSAFE_CHAR_RE.sub("-", raw).strip("-.")
    if not name:
        raise ReleaseRoutingError(
            f"the name {raw!r} has no usable characters after sanitising; rename the worktree or branch"
        )
    problem = _name_problem(name)
    if problem:
        raise ReleaseRoutingError(f"the name {name!r} (from {raw!r}) {problem}; rename the worktree or branch")
    return name


def is_valid_delivered_name(name: str) -> bool:
    """True when ``name`` is non-empty, made of ``[A-Za-z0-9._-]`` and usable as a ref and lock name."""
    return _DELIVERED_NAME_RE.fullmatch(name) is not None and _name_problem(name) is None
