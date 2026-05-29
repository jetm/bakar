# bakar log

Tail a run's log file live (`tail -f` behavior). Shows the last 40 lines of existing content, then streams new writes.

## Synopsis

```text
bakar log [KAS_YAML] [OPTIONS]
```

## Options

| Flag | Description |
|------|-------------|
| `--run` | Run ID (`YYYYMMDD-HHMMSS`). Latest run if omitted |
| `--which` | Log file to follow: `kas`, `console`, or `events` (default: `kas`) |
| `--manifest`, `-f` | Manifest filename for BSP family dispatch |
| `--workspace`, `-w` | Workspace root override |

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
bakar log --run 20260601-143022

# Follow log for a BYO build
bakar log my-project.yml

# Follow events for a BYO build
bakar log my-project.yml --which events
```

## Notes

- Press Ctrl+C to stop following.
- When `kas.log` does not exist yet (build is between steps), `bakar log` automatically falls back to `console.log` with a note.
- `--which` accepts exactly `kas`, `console`, or `events`; any other value exits 2.

## See also

- [triage.md](triage.md) - post-mortem using the same log files
- [report.md](report.md) - summary of a completed run
