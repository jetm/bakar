"""Read-only probe of a host's shared clones, run through ``bash -s``.

Dispatch has to classify the HOST's clones by the same rules the host build
applies, without changing anything there and without a bakar install on the
host. The probe is one bash script that observes each pinned repository with
``rev-parse``, ``status`` and ``cat-file`` only and prints one tab-separated
line per repository; :func:`parse_probe` turns those lines back into the
``RepoFacts`` that ``repo_reuse.gather_local_facts`` would have produced.

Entry names never reach the script or its output: the list index identifies an
entry, so a hostile repository name cannot inject shell or forge a line.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess

from bakar.steps import remote_dispatch, repo_reuse
from bakar.steps.repo_reuse import PinnedRepo, RepoFacts

_FIELDS = 9
# How long a killed script's pipes get to drain before its partial output is
# taken from the timeout alone.
_DRAIN_TIMEOUT_S = 5.0


class PinProbeIncomplete(RuntimeError):  # noqa: N818 - name fixed by the dispatch spec
    """The probe could not observe every repository it was asked about."""


def _probeable(entry: PinnedRepo) -> bool:
    return entry.pin is not None and repo_reuse._HEX_PIN.fullmatch(entry.pin) is not None


_HELPERS = """\
set +e
bakar_head_clean() {
  H=$(git -C "$1" --no-optional-locks rev-parse HEAD 2>/dev/null) || H=-
  [ -n "$H" ] || H=-
  if S=$(git -C "$1" --no-optional-locks status --porcelain 2>/dev/null); then
    if [ -z "$S" ]; then C=1; else C=0; fi
  else
    C=-
  fi
}
"""


def probe_script(entries: list[PinnedRepo]) -> str:
    """Build the read-only bash script that reports on every probeable entry."""
    lines = [_HELPERS, "n=0"]
    for index, entry in enumerate(entries):
        if not _probeable(entry) or entry.pin is None:
            continue
        path = shlex.quote(str(entry.path))
        side = shlex.quote(str(entry.side_path)) if entry.side_path is not None else None
        pin_ref = shlex.quote(f"{entry.pin}^{{commit}}")
        lines.append("n=$((n+1))")
        lines.append(f"p={path}")
        lines.append('if [ -d "$p" ]; then')
        lines.append('  bakar_head_clean "$p"; h=$H; c=$C')
        lines.append(
            f'  if git -C "$p" --no-optional-locks cat-file -e {pin_ref} 2>/dev/null; then pp=1; else pp=0; fi'
        )
        if side is not None:
            lines.append(f"  sp={side}")
            lines.append('  if [ -e "$sp" ]; then')
            lines.append('    bakar_head_clean "$sp"; sh=$H; sc=$C; se=1')
            lines.append("  else se=0; sh=-; sc=-; fi")
        else:
            lines.append("  se=0; sh=-; sc=-")
        lines.append(
            f'  printf \'BAKAR_PIN\\t{index}\\t1\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n\' "$h" "$c" "$pp" "$se" "$sh" "$sc"'
        )
        lines.append("else")
        lines.append(f"  printf 'BAKAR_PIN\\t{index}\\t0\\t-\\t-\\t-\\t0\\t-\\t-\\n'")
        lines.append("fi")
    lines.append("printf 'BAKAR_PIN_DONE\\t%s\\n' \"$n\"")
    return "\n".join(lines) + "\n"


def _flag(value: str, field: str) -> bool | None:
    if value == "-":
        return None
    if value in ("0", "1"):
        return value == "1"
    raise PinProbeIncomplete(f"malformed probe field {field}: {value!r}")


def _bit(value: str, field: str) -> bool:
    if value not in ("0", "1"):
        raise PinProbeIncomplete(f"malformed probe field {field}: {value!r}")
    return value == "1"


def _text(value: str) -> str | None:
    return None if value == "-" else value


def parse_probe(stdout: str, entries: list[PinnedRepo]) -> list[RepoFacts]:
    """Map probe output back onto ``entries``; unprobed entries stay unknown."""
    expected = {i for i, e in enumerate(entries) if _probeable(e)}
    facts: dict[int, RepoFacts] = {}
    done: int | None = None
    for line in stdout.splitlines():
        parts = line.split("\t")
        if parts[0] == "BAKAR_PIN_DONE":
            if len(parts) != 2 or not parts[1].isdigit() or done is not None:
                raise PinProbeIncomplete(f"malformed probe line: {line!r}")
            done = int(parts[1])
            continue
        if parts[0] != "BAKAR_PIN":
            continue
        if len(parts) != _FIELDS or not parts[1].isdigit():
            raise PinProbeIncomplete(f"malformed probe line: {line!r}")
        index = int(parts[1])
        if index not in expected:
            raise PinProbeIncomplete(f"probe reported unexpected index {index}")
        if index in facts:
            raise PinProbeIncomplete(f"probe reported index {index} twice")
        facts[index] = RepoFacts(
            exists=_bit(parts[2], "exists"),
            head=_text(parts[3]),
            clean=_flag(parts[4], "clean"),
            pin_present=_flag(parts[5], "pin_present"),
            side_exists=_bit(parts[6], "side_exists"),
            side_head=_text(parts[7]),
            side_clean=_flag(parts[8], "side_clean"),
        )
    if done is None:
        raise PinProbeIncomplete("probe output ended without a done marker")
    if done != len(expected) or len(facts) != len(expected):
        raise PinProbeIncomplete(
            f"probe reported {len(facts)} repositories (done={done}) but {len(expected)} were asked about"
        )
    return [facts.get(i, RepoFacts.unknown()) for i in range(len(entries))]


def run_bash(script: str, *, host: str | None, timeout: float) -> tuple[int, str, str, bool]:
    """Run ``script`` through ``bash -s``, here or on ``host`` over ssh.

    Returns (status, stdout, stderr, timed_out). A timeout reads as status 255
    with whatever output arrived before it, and ``timed_out`` is the only thing
    that tells it from a script that exited 255. The partial output matters to
    the pin sync: a sync that moved clones and then timed out has already
    printed those rows and its revert record. Both the read-only probe and the
    pin sync run this way, so a timeout is named the same everywhere.
    """
    if host is None:
        return _run_local_bash(script, timeout=timeout)
    result = remote_dispatch._ssh_bash_capture(host, script, timeout=timeout)
    if isinstance(result, remote_dispatch.CaptureTimedOut):
        # Its stderr ends in a "could not reach" text, which is wrong for a script that ran.
        return 255, result.stdout or "", result.child_stderr, True
    return result.returncode, result.stdout or "", result.stderr or "", False


def _run_local_bash(script: str, *, timeout: float) -> tuple[int, str, str, bool]:
    """The local half of :func:`run_bash`; a timeout kills the script's whole process group.

    ``subprocess.run`` kills only the bash it started, so a git fetch or checkout
    the script had in flight would keep running after the caller dropped the
    workspace locks. ``start_new_session`` makes bash the group leader, which is
    what lets one ``killpg`` reach every descendant.
    """
    with subprocess.Popen(
        ["bash", "-s"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as proc:
        try:
            out, err = proc.communicate(script, timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            try:
                # With the group gone the pipes close, so this collects the rest.
                out, err = proc.communicate(timeout=_DRAIN_TIMEOUT_S)
            except subprocess.TimeoutExpired as drain_exc:
                # A descendant that left the group still holds a pipe. The drain's
                # own timeout carries everything read so far, the drain window included.
                proc.kill()
                out = remote_dispatch.decode_partial(drain_exc.stdout)
                err = remote_dispatch.decode_partial(drain_exc.stderr)
            return 255, out or "", err or "", True
        except BaseException:
            # A Ctrl-C never reaches the new session, so the script would outlive
            # the caller's locks unless the group is killed here.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            raise
    return proc.returncode, out, err, False


def probe_local(entries: list[PinnedRepo]) -> list[RepoFacts]:
    """Run the probe through local ``bash -s``."""
    timeout = repo_reuse._GIT_READ_TIMEOUT_S
    rc, out, err, timed_out = run_bash(probe_script(entries), host=None, timeout=timeout)
    if timed_out:
        raise PinProbeIncomplete(f"local pin probe timed out after {timeout:g}s")
    if rc != 0:
        raise PinProbeIncomplete(f"local pin probe exited {rc}: {err.strip()}")
    return parse_probe(out, entries)


def probe_host(host: str, entries: list[PinnedRepo]) -> list[RepoFacts]:
    """Run the probe on ``host`` over ssh; any failure to finish refuses."""
    timeout = repo_reuse._GIT_READ_TIMEOUT_S
    rc, out, err, timed_out = run_bash(probe_script(entries), host=host, timeout=timeout)
    if timed_out:
        raise PinProbeIncomplete(f"pin probe on {host} timed out after {timeout:g}s")
    if rc != 0:
        raise PinProbeIncomplete(f"pin probe on {host} failed (exit {rc}): {err.strip()}")
    return parse_probe(out, entries)
