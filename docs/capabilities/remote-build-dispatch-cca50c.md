# `remote-build-dispatch`: the shared-clone pin check runs on the host before any remote write; a meta-avocado dispatch can be previewed with `--dry-run`; the dispatch plan names the resolved workspace and its source

**Delivered by:** remote-dispatch-stale-workspace
**Modules touched:** other, tests

## Delivery Note

Invocation: bakar build /home/tiamarin/repos/work/peridio/meta-avocado/kas/machine/jetson-orin-nano.yml -t nativesdk-tegraflash-tools --on pc3 --dry-run ; bakar sync /home/tiamarin/repos/work/peridio/meta-avocado/kas/machine/jetson-orin-nano.yml --on pc3 --dry-run
Precondition: `[workspaces]` template or `[workspaces.releases]` in ~/.config/bakar/config.toml; ssh BatchMode key auth to the host; the same bakar build installed on both hosts (the repair itself needs no remote bakar, the dispatch capability probe does); `bash`, `git` and `flock` on the host
Success signal: dry run prints workspace, per-repo pin table and artifact list with exit 0 and creates nothing; sync prints one line per repo then `batch complete: N/N` and writes a revert record; a refused real dispatch exits non-zero with one message naming every blocking clone
Silent failure: `bakar drift` on a kas file without a kas.lock reports all clean (the defect being fixed); a host whose bakar predates this change running the old in-build check, which the capability probe does not detect
