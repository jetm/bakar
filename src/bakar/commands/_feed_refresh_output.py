"""What ``bakar feed refresh`` shows and does after the delta is published.

Machine output (the overlay stanzas and the three avocado commands) goes to
stdout so a caller can paste or pipe it; every sentence meant for a person goes
to stderr. Serving and the avocado run are opt-in: without ``serve`` no server
starts, and without ``run_avocado`` no avocado process starts.

``start_serving`` and ``port_available`` are re-exported at module level so
tests patch them here and never bind a socket.
"""

from __future__ import annotations

import dataclasses
import gzip
import ipaddress
import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, TextIO

from bakar import feed_preflight
from bakar.commands import _feed_refresh as _refresh
from bakar.diagnostics import Status, markup_safe
from bakar.feed_serve import DEFAULT_BIND, DEFAULT_PORT, port_available, start_serving
from bakar.steps.avocado_cli import (
    OverlayRepo,
    UrlRepo,
    command_lines,
    render_overlay_stanza,
    render_url_stanza,
    run_avocado_commands,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bakar import feed

__all__ = [
    "OutputOptions",
    "apply_snapshot",
    "check_port",
    "port_available",
    "preflight_blocks",
    "present_refresh",
    "run_avocado_commands",
    "start_serving",
]

_NAME_PREFIX = "bakar-delta-"
_SDK_STAGES = ("sdk",)
_TARGET_STAGES = ("rootfs", "runtime")


@dataclass(frozen=True, kw_only=True, slots=True)
class OutputOptions:
    """The output-side options of ``feed refresh``.

    ``advertise`` is the host printed in the url stanza; ``None`` means the
    bound address, because an address the server is not listening on is never
    advertised unless the caller names it. Resolving an explicit key's loopback
    address for the SDK container is avocado-cli's job, not this module's.
    """

    project_dir: Path
    serve: bool = False
    bind: str = DEFAULT_BIND
    port: int = DEFAULT_PORT
    advertise: str | None = None
    snapshot: bool = False
    run_avocado: bool = False


def apply_snapshot(req: _refresh.RefreshRequest, opts: OutputOptions) -> _refresh.RefreshRequest:
    """Return ``req`` carrying the snapshot choice, which the publish step reads."""
    return dataclasses.replace(req, snapshot=opts.snapshot)


def check_port(opts: OutputOptions) -> str | None:
    """Return a refusal message when serving was asked for and the port is taken, else ``None``.

    Meant to run before the build, so a taken port costs seconds rather than a
    finished build followed by a failed serve.
    """
    if opts.serve and not port_available(opts.bind, opts.port):
        return f"cannot serve: {opts.bind}:{opts.port} is already in use; pass --port to pick another"
    return None


def preflight_blocks(
    *,
    scripts: Path | None,
    roots: feed.FeedRoots | None,
    release: str,
    channel: str,
    err: TextIO | None = None,
) -> bool:
    """Run the pre-build preflight (no deploy directory yet) and report whether any check blocks.

    ``run_refresh`` does not run it, so the caller does before the build.
    Failures print to stderr with their fix hints.
    """
    err = err or sys.stderr
    results = _refresh.preflight_results(
        scripts=scripts, deploy_dir=None, roots=roots, release=release, channel=channel
    )
    for result in (r for r in results if r.status is Status.FAIL):
        print(f"[{result.severity}] {result.name}: {markup_safe(result.message)}", file=err)
        if result.fix_hint:
            print(f"    -> {markup_safe(result.fix_hint)}", file=err)
    blockers = feed_preflight.blocking(results)
    if blockers:
        print(f"{len(blockers)} prerequisite(s) missing; nothing was built", file=err)
    return bool(blockers)


def _stages_for(root: str) -> tuple[str, ...]:
    return _SDK_STAGES if root.split("/", 1)[0] == "sdk" else _TARGET_STAGES


def _repo_name(root: str) -> str:
    return _NAME_PREFIX + root.replace("/", "-")


_PACKAGES_ATTR = re.compile(rb"<metadata\b[^>]*\bpackages=\"(\d+)\"")
_PACKAGE_ELEMENT = re.compile(rb"<package\b[^>]*\btype=\"rpm\"")


def repo_package_count(path: Path) -> int:
    """Count the packages a published repository serves.

    A pooled repository (``render-pool-local``) holds only ``repodata/``; its
    RPMs sit in the channel's ``_pkgs`` pool, so files under the repository
    directory say nothing. The primary metadata's ``packages`` attribute is the
    count, with the number of ``<package type="rpm">`` elements as the fallback.
    ``*.rpm`` files are counted only when there is no readable primary file, and
    a directory with neither gives 0.
    """
    if not path.is_dir():
        return 0
    for primary in sorted((path / "repodata").glob("*primary.xml*")):
        try:
            raw = primary.read_bytes()
            data = gzip.decompress(raw) if primary.suffix == ".gz" else raw
        except OSError, EOFError, gzip.BadGzipFile:
            continue
        if primary.suffix not in {".gz", ".xml"}:
            continue
        match = _PACKAGES_ATTR.search(data)
        return int(match.group(1)) if match else len(_PACKAGE_ELEMENT.findall(data))
    return sum(1 for _ in path.rglob("*.rpm"))


def _overlay_repos(result: _refresh.RefreshResult) -> list[OverlayRepo]:
    roots = [str(r) for r in result.sync.get("repos", [])]  # type: ignore[attr-defined]
    repos = []
    for root in roots:
        path = result.channel_dir / root
        count = repo_package_count(path)
        repos.append(OverlayRepo(name=_repo_name(root), path=path, stages=_stages_for(root), package_count=count))
    return repos


def _is_ipv6(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).version == 6
    except ValueError:
        return False


def check_advertise(address: str | None) -> str | None:
    """Return a refusal message when ``address`` is not a bare host, else None.

    ``--advertise`` takes a host only; the port is ``--port``. A value holding
    ``:`` that is not an IPv6 address (``myhost:9000``) would otherwise be
    bracketed into a url that points nowhere.
    """
    if address is None or ":" not in address:
        return None
    bare = address[1:-1] if address.startswith("[") and address.endswith("]") else address
    if _is_ipv6(bare):
        return None
    return f"--advertise takes a host only, got {address!r}; set the port with --port"


def _host(address: str) -> str:
    if address.startswith("[") and address.endswith("]"):
        return address
    return f"[{address}]" if _is_ipv6(address) else address


def present_refresh(
    req: _refresh.RefreshRequest,
    result: _refresh.RefreshResult,
    opts: OutputOptions,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Print the stanzas and commands, optionally serve and run avocado; return the exit code.

    A failed serve still prints the directory stanzas (they stand without a
    server) but advertises no url and skips the avocado run, whose result would
    depend on a feed that is not up.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    repos = _overlay_repos(result)
    channel_rel = f"{req.release}/{req.channel}"
    print(f"refreshed {len(result.recipes)} recipe(s) into {result.channel_dir}", file=err)

    stanza = render_overlay_stanza(opts.project_dir, repos)
    if stanza:
        print(stanza, end="", file=out)
    else:
        print("no repository in the delta holds packages; no stanza to print", file=err)

    if opts.serve:
        refusal = check_advertise(opts.advertise)
        if refusal is not None:
            print(refusal, file=err)
            return 1
        pid = start_serving(req.delta_root, port=opts.port, bind=opts.bind)
        if pid is None:
            print(
                f"failed to serve {req.delta_root}: nothing came up on {opts.bind}:{opts.port}; "
                "the port is most likely already in use, pass --port to pick another",
                file=err,
            )
            return 1
        host = _host(opts.advertise or opts.bind)
        base = f"http://{host}:{opts.port}/{channel_rel}"
        roots = {_repo_name(str(r)): str(r) for r in result.sync.get("repos", [])}  # type: ignore[attr-defined]
        url_stanza = render_url_stanza(
            [UrlRepo(r.name, f"{base}/{roots[r.name]}", r.stages, r.package_count) for r in repos]
        )
        if url_stanza:
            print("# served alternative to the directory repos above", file=out)
            print(url_stanza, end="", file=out)
        print(f"serving {req.delta_root} on {opts.bind}:{opts.port} (pid {pid}), advertised as {host}", file=err)

    print("\n".join(command_lines()), file=out)

    if opts.run_avocado:
        run = run_avocado_commands(opts.project_dir)
        if not run.ok:
            why = "timed out" if run.timed_out else f"exited {run.returncode}"
            print(f"avocado {run.failed_command} {why}; stopped", file=err)
            return 1
    return 0
