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
| `--show-sstate` | | Show the sstate cache summary section (or set `layers.show_sstate_summary`) |

## Examples

```bash
# Report on the most recent build
bakar report

# Report on a specific run
bakar report 20260601-143022

# Machine-readable JSON output (pipe to jq, etc.)
bakar report --json | jq '.duration_s'
bakar report 20260601-143022 --json

# Include the sstate cache summary (one-off flag or persisted toggle)
bakar report --show-sstate
bakar settings set layers.show_sstate_summary true
```

## Output (human)

```text
:: report 20260601-143022
status: success
duration: 1823s
build_revision: a3f9c21b8e04
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
  "build_revision": "a3f9c21b8e04",
  "deploy_dir": "/bsp/nxp/build/tmp/deploy/images/imx8mp-var-dart",
  "image_size": 245366784,
  "peak_tmp_bytes": 18432000000,
  "layers": [
    {"name": "meta-imx", "sha": "abc12345", "branch": "main"},
    ...
  ]
}
```

## build_revision

`build_revision` is a 12-character hex string derived from the sorted per-layer
short SHAs. Two report calls over the same checkout produce the same value,
regardless of layer order. It is omitted from text output and the `--json`
object when no layer SHAs are available.

Use it to correlate a report with sstate-cache entries or to confirm that two
builds used identical layer checkouts.

## sstate summary

bitbake emits an `Sstate summary:` line into every `kas.log`. With
`--show-sstate` (or the persisted `layers.show_sstate_summary` setting),
`bakar report` parses that line and renders the cache hit/miss breakdown:

```text
sstate summary:
  wanted: 2756
  local: 117
  mirrors: 0
  missed: 2639
  current: 0
  match: 4%
  complete: 100%
```

- `wanted` - tasks bitbake wanted from sstate
- `local` - hits from the local `SSTATE_DIR`
- `mirrors` - hits from `SSTATE_MIRRORS`
- `missed` - tasks rebuilt from scratch (no sstate object)
- `current` - tasks whose stamps were already current
- `match` = (local + mirrors) / wanted; `complete` = (local + mirrors + current) / (wanted + current)

A low `match` after a small change usually means a signature drift - a
host-specific variable leaked into a task hash, or the machine/distro changed.
The section is omitted (and its `--json` keys absent) when the toggle is off or
the summary line is missing from the log.

## buildhistory

When the build inherits buildhistory (`INHERIT += "buildhistory"` in a kas
overlay), `bakar report` auto-detects `<bsp-root>/build/buildhistory/` and
parses its static artifacts - no flag, presence of the directory is the gate:

```text
buildhistory:
  image size: 524288 KiB
  packages: 412
  top packages:
    linux-imx: 81920 KiB
    busybox: 4096 KiB
  dirty layers: meta-variscite-bsp
```

- `image size` - rootfs size from `image-info.txt` `IMAGESIZE` (distinct from the deployed-artifact `image_size`)
- `packages` - count from `installed-package-names.txt`
- `top packages` - largest 10 from `installed-package-sizes.txt`
- `dirty layers` - layers flagged `-- modified` in `metadata-revs` (uncommitted tree at build time)

bakar never injects `INHERIT += "buildhistory"`; the section appears only when
the user opted in via their own overlay.

## Notes

- `--json` writes to stdout; the human-readable output goes to stderr (consistent with all bakar output).
- `image_size`, `peak_tmp_bytes`, and `duration_s` are omitted from JSON when unavailable (build interrupted before deploy, etc.).
- `build_revision` is omitted from both outputs when `collect_layer_hashes` returns no layers.
- sstate keys appear in `--json` only when `--show-sstate` / `layers.show_sstate_summary` is set; buildhistory keys appear only when the buildhistory directory exists.
- Kernel version and recipe count are best-effort and omitted when unresolvable.

## See also

- [triage.md](triage.md) - failed build post-mortem
- [log.md](log.md) - tail the raw kas.log or events.jsonl
