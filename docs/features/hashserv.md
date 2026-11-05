# Hash equivalence server

The hashserv feature runs a local bitbake hash equivalence server so sstate objects are reused when different inputs produce identical output. The deep page is [hashserv](../hashserv.md).

## What your host needs

1. Keep the daemon state off NFS (hard requirement, doctor check `daemon-state`). The daemon keeps its SQLite, PID and port state in a state directory, and a second node writing the same SQLite file is unsafe. The check refuses a state directory on a confirmed NFS filesystem, and also one whose filesystem type cannot be determined, because an autofs trap may resolve to NFS on first access. Point `sstate_dir` at local disk, or set `build.bb_hashserve` and `build.prserv_host` to the central tier instead.
2. After enabling, sync the workspace so `bitbake-hashserv` exists, then run `bakar hashserv start`. Run `bakar doctor` and read the `hashserv` check (soft requirement) to confirm the daemon is up.

If this host also uses the central-node or central-client feature, the central hashserv endpoint overrides the per-workspace hashserv.

## Enable

Interactive: run `bakar init` and select the hashserv feature.

Non-interactive: `bakar init --feature hashserv --set build.hashserv=true`

`build.hashserv` is the only key this feature writes, so it is the only key `--set` accepts for it.

## Turn off

Run `bakar init --disable hashserv`. It unsets `build.hashserv` in your user config. It does not stop a running daemon; use the commands on the [hashserv](../hashserv.md) page for that.

## See also

- [hashserv](../hashserv.md)
- `bakar hashserv start`
- `bakar doctor`
