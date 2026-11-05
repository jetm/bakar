# systemd scope isolation

Scope isolation runs each build inside a transient `systemd-run --user --scope`, so the build survives an SSH disconnect or a closed terminal and is the preferred victim under global memory pressure. It is on by default. The `scope_*` keys are documented in [config-reference.md](../config-reference.md).

## What your host needs

1. A reachable systemd user manager. This is a hard requirement, checked by `systemd-scope`. `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` must be set, and `systemd-run --user` must work. In an SSH session with no live user instance, run `loginctl enable-linger` and log in again. Without it the build runs unscoped and loses session survival and OOM protection.
2. cgroup v2 (the unified hierarchy). This is a soft requirement, checked by `cgroup-v2`, and it matters only when you set a memory cap or a controller weight. Boot with `systemd.unified_cgroup_hierarchy=1` on the kernel command line. Check with `bakar doctor`.

## Enable

Interactive: run `bakar init` and select the scope feature.

Non-interactive: `bakar init --feature scope --set build.scope=true`

The feature writes one key, and `--set` accepts only this one for it: `build.scope` (default `true`).

## Turn off

Run `bakar init --disable scope`. It sets `build.scope` to `false`. It does not unset the key, because an unset `build.scope` means on.

Memory caps (`scope_memory_max`, `scope_memory_high`) and controller weights (`scope_cpu_weight`, `scope_io_weight`) are not written by `bakar init`. They stay off until you edit them by hand, and `--disable` leaves any value you set in place. The risks, from [config-reference.md](../config-reference.md):

- A memory cap backfires on a host with a large zram or zswap swap. Enable it only on a dedicated build host, and size it above your largest single link step or that step is OOM-killed.
- `scope_memory_high` is ignored with a warning unless `scope_memory_max` is also set.
- Setting `scope_cpu_weight` or `scope_io_weight` realizes the controller across the whole session. Under heavy build I/O the io controller can stall the session with no OOM and no panic. Leave both at `0` unless you have measured a need.

## See also

- [config-reference.md](../config-reference.md)
- [doctor.md](../doctor.md)
- `bakar doctor`
