# Distributed sccache

The sccache-dist feature routes bitbake's C/C++ `do_compile` work through an sccache scheduler so a cold build can pool cores across machines. bakar configures only the client side; the scheduler and build-servers are host services you run yourself. The deep page is [sccache-dist](../sccache-dist.md).

## What your host needs

1. The `sccache` binary on PATH (hard requirement, host mode only). Check with `command -v sccache`. If the doctor check reports it missing, install sccache.
2. A reachable scheduler (hard requirement, doctor check `sccache-dist`). The scheduler URL must carry an explicit port, for example `http://localhost:10600`, and the doctor check makes a 1-second TCP connection to that host and port. Starting the scheduler and build-servers is covered in the operator setup section of [sccache-dist](../sccache-dist.md).
3. The client config `~/.config/sccache/config` with the scheduler URL and the auth token. bakar does not write this file; the overlay only exports the scheduler URL, and the token lives here:

   ```toml
   [dist]
   scheduler_url = "http://localhost:10600"

   [dist.auth]
   type = "token"
   token = "<SECRET_KEY>"
   ```

   `<SECRET_KEY>` is the secret the scheduler's `client_auth` section uses. Without this file the client reports `Disabled` for `sccache --dist-status` and every compile runs local-only, which a reachable scheduler alone does not reveal.

## Enable

Interactive: run `bakar init` and select the sccache-dist feature.

Non-interactive: `bakar init --feature sccache-dist --set build.sccache_dist=true --set build.sccache_scheduler_url=http://localhost:10600`

`build.sccache_dist` and `build.sccache_scheduler_url` are the only keys this feature writes, so they are the only keys `--set` accepts for it.

## Turn off

Run `bakar init --disable sccache-dist`. It unsets `build.sccache_dist` and `build.sccache_scheduler_url` in your user config. It does not stop the scheduler or build-servers.

## See also

- [sccache-dist](../sccache-dist.md)
- `bakar cluster-info`
- `bakar doctor`
