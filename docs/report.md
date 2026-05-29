# bakar report

Summarize a completed build run: status, duration, deploy directory, image size, peak build-tmp size, and per-layer SHAs.

## Synopsis

```text
bakar report [RUN_ID] [OPTIONS]
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `RUN_ID` | | Run ID (`YYYYMMDD-HHMMSS`). Latest run if omitted |
| `--manifest` | `-f` | Manifest filename for BSP family dispatch |
| `--workspace` | `-w` | Workspace root override |
| `--json` | | Emit the summary as a single JSON object on stdout |

## Examples

```bash
# Report on the most recent build
bakar report

# Report on a specific run
bakar report 20260601-143022

# Machine-readable JSON output (pipe to jq, etc.)
bakar report --json | jq '.duration_s'
bakar report 20260601-143022 --json
```

## Output (human)

```text
:: report 20260601-143022
status: success
duration: 1823s
deploy: /bsp/nxp/build/tmp/deploy/images/imx8mp-var-dart
image size: 245366784 bytes
peak build/tmp: 18432000000 bytes
meta-imx         abc12345  main
meta-variscite   def67890  dunfell-var01
poky             11223344  dunfell
```

## Output (JSON)

```json
{
  "run_id": "20260601-143022",
  "status": "success",
  "duration_s": 1823.4,
  "deploy_dir": "/bsp/nxp/build/tmp/deploy/images/imx8mp-var-dart",
  "image_size": 245366784,
  "peak_tmp_bytes": 18432000000,
  "layers": [
    {"name": "meta-imx", "sha": "abc12345", "branch": "main"},
    ...
  ]
}
```

## Notes

- `--json` writes to stdout; the human-readable output goes to stderr (consistent with all bakar output).
- `image_size`, `peak_tmp_bytes`, and `duration_s` are omitted from JSON when unavailable (build interrupted before deploy, etc.).
- Kernel version and recipe count are best-effort and omitted when unresolvable.

## See also

- [triage.md](triage.md) - failed build post-mortem
- [log.md](log.md) - tail the raw kas.log or events.jsonl
