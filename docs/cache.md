# bakar cache

Push finished downloads to the central store, report the push queue, and capture diagnostics for a hung build.

## Why this exists

Each builder keeps its own `DL_DIR`. Without a shared store, a source one host already downloaded is fetched again from upstream by every other host. `bakar cache push --watch` closes that gap: it watches the downloads directory and pushes each finished download to the central store (PC1) as soon as BitBake closes its `.done` stamp. Other hosts then find the file through the `dl_mirror_url` mirror instead of going upstream.

`bakar cache capture` serves the other half: when a build hangs, it records process state and NFS/lock diagnostics so the cause can be read after the fact.

## Synopsis

```text
bakar cache push --watch
bakar cache status
bakar cache capture
```

`bakar cache` with no verb prints help. The group never launches kas and is excluded from the global container flag.

The downloads directory comes from the `DL_DIR` environment variable, else `dl_dir` in config. The push target comes from `cache_push_target`. See [config-reference.md](config-reference.md) for `dl_mirror_url`, `cache_push_target` and the `BAKAR_DL_MIRROR_URL` export.

## Verbs

### push

`bakar cache push --watch` watches the top level of the downloads directory and pushes finished files over rsync/ssh using the key `~/.ssh/bakar-cache-push`. It runs until interrupted; Ctrl-C ends it cleanly.

| Exit | Meaning |
|------|---------|
| 0 | Interrupted after a normal watch |
| 1 | A prerequisite is missing: `cache_push_target` unset, no downloads directory, `inotifywait` not installed, or the push key file not found. The message names the first missing item. |
| 2 | `--watch` was not given. Prints `bakar cache push only runs with --watch`. |

Every eligible file is recorded in the queue before its transfer starts and removed only after the transfer succeeds, so a crash, a killed process or an unreachable store never loses a file. An unreachable store is retried; a full target disk pauses the queue (shown by `status`). Every failed transfer or reconcile logs one warning with rsync's own error text, so under systemd `journalctl --user -u bakar-cache-push.service` shows why a queue is not draining; `status` alone reports only the backlog.

#### Eligibility rules

Only these files are pushed:

- Top-level regular files in the downloads directory.
- A closed `.done` stamp pushes its regular-file sibling first, then the stamp.

Never pushed: directories, symbolic links, `.tmp` files, `.lock` files, anything under a subdirectory such as the `git2` clones, and git mirror tarballs (names starting `git2_`). The hub does not mirror git sources: BitBake fetches git mirror tarballs without checksum verification, so a tarball planted through a compromised push key would be trusted unchecked. Hosts clone `git` and `gitsm` sources from upstream, and the reconcile pass skips `git2_` names too. Downloads fetched over http(s), ftp, crate and gomod keep checksum verification on use.

#### Pre-push check against the hub

When `dl_mirror_url` is set, the pusher sends the hub a HEAD request (2 second timeout) for each queued payload name, and for its `.done` stamp when the payload answers. A pair is dropped from the queue without being pushed only when both the payload and the stamp answer HTTP 2xx, so a file a host fetched from the hub is not pushed back and a truncated leftover payload cannot hide a good pair. Any other answer, including 404, means the pair is pushed. The first check that errors or times out ends the checks for that drain, as does an overall 10 second budget per drain or a stop request, and every remaining pair is pushed. With `dl_mirror_url` unset no check happens. Git mirror tarballs (`git2_*` and `gitshallow_*`) are never pushed, because the hub mirror does not serve git.

### status

`bakar cache status` reads the queue and prints exactly four lines on standard output, in this order, and always exits 0:

```text
queue: <n>
oldest: <seconds or ->
paused: no
last reconcile: <time or never>
```

| Line | Value |
|------|-------|
| `queue: <n>` | Number of entries waiting to be pushed. |
| `oldest: <seconds or ->` | Age in whole seconds of the oldest queued entry, or `-` when the queue is empty. |
| `paused: no` or `paused: yes (<reason>)` | `yes` with the reason, for example `target disk full`, while the pusher is paused. |
| `last reconcile: <time or never>` | UTC ISO 8601 time of the last full reconcile against the store, or `never`. |

On an empty queue the output is `queue: 0`, `oldest: -`, `paused: no` and `last reconcile: never` (until the first reconcile).

### capture

`bakar cache capture` prints a diagnostics report for a running BitBake hang: BitBake process state plus NFS and lock diagnostics for the downloads, sstate and ccache directories (`DL_DIR`, `SSTATE_DIR` and `CCACHE_DIR` environment variables first, then the configured `dl_dir`, `sstate_dir` and `ccache_dir`). Unset directories are skipped. It exits 0.

## Queue location

The queue is a SQLite database under the XDG state directory, `$XDG_STATE_HOME/bakar/cache-push` (default `~/.local/state/bakar/cache-push`), created with mode 0700. It is deliberately never placed under the downloads, sstate or ccache directories, which may be NFS-backed and unsafe for SQLite locking.

## systemd user unit

On pc2 and pc3 the pusher runs as the user unit `bakar-cache-push.service`, which executes `bakar cache push --watch`. Enable it with:

```text
systemctl --user enable --now bakar-cache-push.service
```

Leave `cache_push_target` unset on the store host; it does not run the pusher.

The push target is a separate receiving directory on the store host (for example `downloads-incoming`), never the store host's own `DL_DIR`. BitBake unpickles `.done` stamps, so a stamp pushed into a directory the store host builds from would run on its next build; the store host reads pushed files only through the `dl_mirror_url` mirror, where the payload checksum is verified and the stamp is inert.

## Doctor rows

`bakar doctor` carries three rows for this feature. See [doctor.md](doctor.md) for pass and fail conditions.

| Row | Severity | Fails when |
|-----|----------|------------|
| `cache-push-service` | WARN (INFO past 15 minutes) | The unit is inactive, or the oldest queued entry is over 60 minutes old (over 15 minutes is INFO) |
| `sstate-mirror-shared-mount` | BLOCK (WARN when undetermined) | An http(s) sstate mirror is set while the sstate directory is on NFS |
| `store-stale-tarballs` | WARN | A `git2_*.tar.gz.tmp` file over an hour old sits at the top of the downloads directory |

## Related

[config-reference.md](config-reference.md), [doctor.md](doctor.md), [hashserv.md](hashserv.md), [capabilities/shared-cache-hub-push.md](capabilities/shared-cache-hub-push.md)
