# bakar log

Tail a run's log file live (`tail -f` behavior). Shows the last 40 lines of existing content, then streams new writes.

## Synopsis

```text
bakar log [KAS_YAML] [OPTIONS]
```

## Options

| Flag | Description |
|------|-------------|
| `--run` | Run ID (`YYYYMMDD-HHMMSS-PID`). Latest run if omitted |
| `--which` | Log file to follow: `kas`, `console`, or `events` (default: `kas`) |
| `--manifest`, `-f` | Manifest filename for BSP family dispatch |
| `--workspace`, `-w` | Workspace root override |
| `--on` | Follow the log of a run on this host over ssh instead of locally. Refuses a positional kas YAML, `-w` and `-f` (exit 2) |

## Log files

| `--which` | File | Content |
|-----------|------|---------|
| `kas` | `kas.log` | stdout+stderr from kas-container (raw knotty output); falls back to `console.log` if build hasn't reached kas_build yet |
| `console` | `console.log` | Human-readable step progress lines |
| `events` | `events.jsonl` | Structured JSON events (step_start, step_ok, step_fail) |

## Examples

```bash
# Follow kas.log for the current build (most common - opened mid-build)
bakar log

# Follow console.log for high-level step progress
bakar log --which console

# Follow events.jsonl for structured output
bakar log --which events

# Follow log for a specific past run
bakar log --run 20260601-143022-48213

# Follow log for a BYO build
bakar log my-project.yml

# Follow events for a BYO build
bakar log my-project.yml --which events
```

## Following a run on another host

`bakar log --on HOST` runs `bakar log` on the host over ssh and streams its stdout to your stdout. The `following <path>` line and any errors stay on stderr, so a pipe sees only log text.

- `--run` takes a run id or a detached unit name that the host registered. Without `--run` it follows the newest unit's latest run.
- `--which` is forwarded. A positional kas YAML, `-w` and `-f` each name a local path, so `--on` refuses them with exit 2 and starts no ssh.
- The remote bakar must contain this change. Like every other `--on` command, it is refused when its version differs from the local one.
- Ctrl+C ends the stream and exits 0. An ssh transport failure exits 2 with the failure named on stderr.
- The ssh link is bounded by the keepalive options, so a dead link ends the stream.
- No terminal is allocated, so a follower on the host can linger until its next output after the link drops.

```bash
# Follow the newest registered run on pc3
bakar log --on pc3

# Follow a specific run's console log on pc3
bakar log --on pc3 --which console --run 20260601-143022-48213
```

## Notes

- Press Ctrl+C to stop following.
- When `kas.log` does not exist yet (build is between steps), `bakar log` automatically falls back to `console.log` with a note.
- `--which` accepts exactly `kas`, `console`, or `events`; any other value exits 2.
- Outside a workspace, `bakar log --run ID` finds a run that a detached or dispatched unit registered. A workspace, a positional kas YAML or `-w` always takes precedence over the registry.

## See also

- [triage.md](triage.md) - post-mortem using the same log files
- [report.md](report.md) - summary of a completed run
