"""What the local feed needs before a sync can work, checked up front.

The feed is the one part of bakar that depends on things pip cannot install.
``render-pool-local.py`` shells straight out to ``createrepo_c`` with no
``which`` guard and no fallback, and ``repo-stage-rpms.sh`` is bash driving a
``tar`` pipe - so on a machine without those, a sync dies partway through with a
``FileNotFoundError`` naming a binary the user never asked for. That is a bad
first experience for something whose actual cause is one missing package.

Everything here is checked TOGETHER and reported as a list. Failing on the first
problem would send someone round the loop once per missing prerequisite, and the
set is large enough that this matters: an interpreter, a native binary, two
shell tools, a layer checkout, and a finished build.

The checks are deliberately cheap and read-only - a ``which`` and a few stats -
so ``feed sync`` can run them every time rather than asking the user to remember
a separate command.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from typing import TYPE_CHECKING

from bakar.diagnostics import CheckResult, Severity, Status

if TYPE_CHECKING:
    from pathlib import Path

# The binary render-pool-local.py invokes. Not a Python package, not installable
# with bakar, and named differently on every distro - which is why the hint below
# spells out the package rather than the command.
_CREATEREPO = "createrepo_c"

_CREATEREPO_HINT = (
    "install it: Fedora/RHEL `dnf install createrepo_c`, "
    "Debian/Ubuntu `apt install createrepo-c`, "
    "Arch `pacman -S createrepo_c` (or the AUR), "
    "openSUSE `zypper install createrepo_c`. "
    "No macOS package exists - build a feed on Linux, or run these steps in a Linux container."
)


def _ok(name: str, message: str, severity: Severity = Severity.BLOCK) -> CheckResult:
    return CheckResult(name=name, severity=severity, status=Status.PASS, message=message)


def _fail(name: str, message: str, hint: str, severity: Severity = Severity.BLOCK) -> CheckResult:
    return CheckResult(name=name, severity=severity, status=Status.FAIL, message=message, fix_hint=hint)


def check_interpreter() -> CheckResult:
    """Report the running interpreter.

    Informational rather than a gate: bakar declares ``requires-python >=3.14``,
    so an older interpreter fails at install time and this code never runs. It is
    reported because "which Python is this" is the first question when a pip
    install lands somewhere unexpected.
    """
    version = platform.python_version()
    return _ok("python", f"{version} at {sys.executable}", severity=Severity.INFO)


def check_createrepo() -> CheckResult:
    """The one hard external dependency, and the usual reason a sync fails."""
    found = shutil.which(_CREATEREPO)
    if found is None:
        return _fail(
            _CREATEREPO,
            "not on PATH - the renderer shells out to it and has no fallback, so a sync would "
            "fail partway through with a FileNotFoundError",
            _CREATEREPO_HINT,
        )
    return _ok(_CREATEREPO, found)


def check_shell_tools() -> list[CheckResult]:
    """bash and tar, which the staging script needs.

    ``stdbuf`` is deliberately not checked: the script guards it with
    ``command -v`` and only uses it to unbuffer its own progress output.
    """
    results = []
    for tool, why in (("bash", "repo-stage-rpms.sh is a bash script"), ("tar", "staging is a tar pipe")):
        found = shutil.which(tool)
        if found is None:
            results.append(_fail(tool, f"not on PATH - {why}", f"install {tool} through your package manager"))
        else:
            results.append(_ok(tool, found))
    return results


def check_scripts(scripts: Path | None) -> list[CheckResult]:
    """The two meta-avocado scripts bakar drives rather than reimplements.

    ``scripts`` is None when the checkout could not be located at all, which is a
    different failure from a checkout that is present but incomplete - so it gets
    its own message rather than two confusing per-file ones.
    """
    if scripts is None:
        return [
            _fail(
                "meta-avocado",
                "no meta-avocado checkout found for this kas YAML",
                "the feed drives meta-avocado's render-pool-local.py and repo-stage-rpms.sh rather than "
                "reimplementing them, so a checkout is required. Pass a kas YAML from inside one.",
            )
        ]

    results = []
    for name in ("render-pool-local.py", "repo-stage-rpms.sh"):
        path = scripts / name
        if not path.is_file():
            results.append(
                _fail(name, f"missing from {scripts}", "update the meta-avocado checkout; this script drives the feed")
            )
        elif not os.access(path, os.X_OK):
            results.append(_fail(name, f"not executable: {path}", f"chmod +x {path}"))
        else:
            results.append(_ok(name, str(path)))
    return results


def check_build_output(deploy_dir: Path) -> CheckResult:
    """A finished build that produced a repo map.

    The map is what declares which repositories a sync renders, so without it
    there is nothing to stage - and its absence usually means the build has not
    run rather than that anything is broken.
    """
    repo_map = deploy_dir / "avocado-repo.map"
    if not deploy_dir.is_dir():
        return _fail(
            "build output",
            f"no RPM deploy directory at {deploy_dir}",
            "run a build first - the feed stages what a build already produced",
        )
    if not repo_map.is_file():
        return _fail(
            "avocado-repo.map",
            f"missing from {deploy_dir}",
            "this build produced no package feed. Build an image, or point --workspace at the build that did.",
        )
    return _ok("avocado-repo.map", str(repo_map))


def check_release_channel(deploy_dir: Path, release: str, channel: str) -> CheckResult:
    """The requested release/channel must match what the build declares.

    ``DISTRO_CODENAME`` IS ``release/channel`` - it is what dnf expands
    ``$releasever`` to, so it decides the path a client composes. bakar's own
    defaults are ``2024/edge``, which is right for some builds and wrong for
    others: the live feed is ``dev/local``. Rendering into the wrong pair
    produces a perfectly valid feed at a path nothing will ever ask for, and
    every symptom appears on the client side minutes later.

    Skipped rather than guessed when the build records no usable codename -
    ``_split_codename`` deliberately refuses a codename with no separator, and
    inventing a channel for it would be the same error one level down.
    """
    from bakar.feed_consolidate import _read_testdata, _split_codename

    # testdata sits under deploy/images/<machine>/, a sibling of deploy/rpm.
    declared = _read_testdata(deploy_dir.parent).get("DISTRO_CODENAME")
    want_release, want_channel = _split_codename(declared)

    if want_release is None or want_channel is None:
        return CheckResult(
            name="release/channel",
            severity=Severity.INFO,
            status=Status.SKIP,
            message=f"build declares no usable DISTRO_CODENAME; rendering into {release}/{channel} as asked",
        )

    if (want_release, want_channel) != (release, channel):
        return _fail(
            "release/channel",
            f"build declares DISTRO_CODENAME={want_release}/{want_channel} but this sync targets "
            f"{release}/{channel} - the feed would render where no client looks",
            f"pass --release {want_release} --channel {want_channel}",
        )

    return _ok("release/channel", f"{release}/{channel} matches the build's DISTRO_CODENAME")


def check_writable(feed_root: Path, stage_root: Path) -> list[CheckResult]:
    """Both roots must be creatable and writable.

    The stage root is checked as well as the feed, because it is a SIBLING of the
    feed rather than inside it - so a writable feed says nothing about it, and a
    read-only stage root fails at the first tar pipe.
    """
    results = []
    for label, root in (("feed root", feed_root), ("stage root", stage_root)):
        existing = root
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        if not os.access(existing, os.W_OK):
            results.append(
                _fail(
                    label,
                    f"{root} is not writable (nearest existing parent: {existing})",
                    "fix ownership or pick another path with `bakar settings set feed_dir <path>`",
                )
            )
        else:
            results.append(_ok(label, str(root)))
    return results


def check_platform() -> CheckResult:
    """Serving the feed needs POSIX process control.

    ``serve``/``stop``/``status`` track the server by PID and signal it with
    SIGTERM, which is POSIX. Rendering and staging are the same story one level
    down (bash, tar, createrepo_c). Reported rather than blocked so a sync on an
    odd platform still tells the user which half is expected to work.
    """
    if os.name != "posix":
        return _fail(
            "platform",
            f"{platform.system()} is not POSIX - serve/stop rely on SIGTERM and a PID, and the "
            "staging and render steps need bash, tar and createrepo_c",
            "run the feed on Linux, or inside a Linux container/WSL",
            severity=Severity.WARN,
        )
    return _ok("platform", f"{platform.system()} {platform.machine()}", severity=Severity.INFO)


def preflight(  # noqa: PLR0913 - each argument scopes a different tier of the check, and collapsing them into an object would hide which tiers a caller opted into
    *,
    feed_root: Path,
    stage_root: Path,
    scripts: Path | None = None,
    deploy_dir: Path | None = None,
    release: str | None = None,
    channel: str | None = None,
) -> list[CheckResult]:
    """Run every prerequisite check and return the results, in report order.

    Everything past the host tools is optional so this can also answer "is this
    machine capable of building a feed at all" before any workspace is known -
    which is what ``bakar feed doctor`` asks with no kas YAML.
    """
    results = [check_interpreter(), check_platform(), check_createrepo(), *check_shell_tools()]
    if scripts is not None or deploy_dir is not None:
        results.extend(check_scripts(scripts))
    if deploy_dir is not None:
        results.append(check_build_output(deploy_dir))
        if release is not None and channel is not None:
            results.append(check_release_channel(deploy_dir, release, channel))
    results.extend(check_writable(feed_root, stage_root))
    return results


def blocking(results: list[CheckResult]) -> list[CheckResult]:
    """Return the failures that must stop a sync.

    A WARN failure is reported and does not block - the platform check is one, on
    the reasoning that someone running an unusual setup deliberately should be
    told rather than refused.
    """
    return [r for r in results if r.status is Status.FAIL and r.severity is Severity.BLOCK]
