# `concern-single-ownership`: per-workspace daemon state reading and lifecycle helpers have one implementation shared by hashserv and prserv

**Delivered by:** improve-iteration-3
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar build -f MANIFEST -m MACHINE` (add `--on HOST` for dispatch), `bakar clean`, `bakar doctor`, `bakar triage RUN_ID`, `bakar report RUN_ID`, `bakar insights RUN_ID`, `bakar monitor --once`, with the global `--container` flag placed before the subcommand.
Precondition: none - no new config key, environment variable, migration or hook registration; the user-tier config loader now warns on unknown keys instead of ignoring them.
Success signal: exit codes and messages as listed under E2E Testing, `events.jsonl` carrying `cve_report`, `sbom_filter` and `feed_sync` step records, `bakar doctor` passing the hashserv check on a bound daemon, and `uv run --frozen ty check src/` plus the full suite passing on typer 0.25.1 and on typer 0.27.3 with the pin untouched.
Silent failure: a lock ownership marker that reads empty or unreadable after the retry window is now refused on a network-mounted TOPDIR and on one where `is_path_on_nfs` cannot tell (a confirmed-local TOPDIR still replaces it); a user who sees "unattributable lock marker" has a stale marker to clear, not a running peer.
