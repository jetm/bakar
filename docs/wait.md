# bakar wait

Poll a run until it reaches a terminal state, then exit with its outcome. The
blocking counterpart of [`bakar status`](status.md): one command that returns
when a detached build is done.

## Synopsis

```text
bakar wait [HANDLE] [OPTIONS]
```

`HANDLE` is a run id or a detached unit name, resolved exactly as
[`bakar status`](status.md#handle-resolution) resolves it. With no handle,
`wait` follows the newest run of the current workspace.

## Options

| Flag | Description |
|------|-------------|
| `--timeout SECONDS` | Give up after this many seconds and exit 124 (default: wait unbounded) |
| `--interval SECONDS` | Seconds between polls (default: 30; values below 1 are raised to 1) |
| `--json` | Print the final status object as the only stdout |
| `-w`, `--workspace PATH` | Workspace to search for a foreground run (default: resolved from the current directory) |
| `--on HOST` | Wait on `HOST` over ssh instead of locally |

## Exit status

| Status | Meaning |
|--------|---------|
| 0 | The run `succeeded` |
| 1 | The run `failed` |
| 2 | The handle does not resolve to a run (or, with `--on`, the host is refused: it starts with `-`, `-w` is combined, bakar is missing there or its version differs) |
| 3 | The run is `unknown`: no recorded exit status and no sign of life (with `--on`, an unreachable host or ssh transport failure also exits 3) |
| 124 | The timeout elapsed while the run was still `starting` or `running` |

Exit 124 means keep waiting, not failure. A caller whose tool calls have a
time cap repeats `bakar wait <handle> --timeout 540` for as long as it exits
124, and acts on the first other status.

## Output

Each poll prints one progress line to stderr, so stdout stays clean for
`--json`. The leading time is UTC, marked with a trailing `Z`:

```text
09:15:42Z 20260701-090002-48213 running tasks=1204/7312 running=24
```

With no handle, `wait` resolves the newest run once, on the first poll, and
keeps polling that run (or its detached unit when it has one); a newer run
started during the wait does not take over.

With `--json`, the final status object (the twelve keys documented in
[status.md](status.md#json-field-schema)) is printed to stdout once, after
the last poll. When the handle does not resolve there is no object and stdout
stays empty.

## Remote runs

`--on HOST` runs `bakar wait` on `HOST` over ssh and mirrors its stdout and
exit status. The ssh call is bounded to the timeout plus 60 seconds; without
`--timeout` it is unbounded. An unreachable host or failed transport exits 3
and names the ssh exit status (255 for an unreachable host), so it reads as
`unknown` rather than as a failed build. With `--json`, a null `host` in the
final object is filled with the `--on` host.

## Examples

```bash
# Block until the newest run in this workspace finishes
bakar wait

# Wait for a detached run for at most nine minutes, then report
bakar wait bakar-detach-20260701-090000-a1b2c3 --timeout 540
echo $?

# Poll every 10 seconds and capture the final state
bakar wait 20260701-090002-48213 --interval 10 --json > final.json

# Wait on a run started elsewhere
bakar wait bakar-detach-20260701-090000-a1b2c3 --on pc3 --timeout 540
```

## See also

- [status.md](status.md) - one-shot state report, handle resolution and the JSON schema
- [build.md](build.md) - `--detach` starts a run that outlives your terminal
- [triage.md](triage.md) - find the failing recipe and task after exit 1
- [agents.md](agents.md) - driving bakar from an agent or a script
