# bakar cache

Push finished downloads to the central store, report the push queue, capture diagnostics for a hung build, and prune downloads the hub or a clone already holds.

## Why this exists

Each builder keeps its own `DL_DIR`. Without a shared store, a source one host already downloaded is fetched again from upstream by every other host. `bakar cache push --watch` closes that gap: it watches the downloads directory and pushes each finished download to the central store (PC1) as soon as BitBake closes its `.done` stamp. Other hosts then find the file through the `dl_mirror_url` mirror instead of going upstream.

`bakar cache capture` serves the other half: when a build hangs, it records process state and NFS/lock diagnostics so the cause can be read after the fact.

## Synopsis

```text
bakar cache push --watch
bakar cache status
bakar cache capture
bakar cache prune-downloads [--apply] [--clone-older-than DAYS]
bakar cache link-mirror [--apply]
```

`bakar cache` with no verb prints help. The group never launches kas and is excluded from the global container flag.

The downloads directory comes from the `DL_DIR` environment variable, else `dl_dir` in config. The push target comes from `cache_push_target`. See [config-reference.md](config-reference.md) for `dl_mirror_url`, `cache_push_target` and the `BAKAR_DL_MIRROR_URL` export.

## Verbs

### push

`bakar cache push --watch` watches the top level of the downloads directory and pushes finished files over rsync/ssh using the key `~/.ssh/bakar-cache-push`. It runs until interrupted; Ctrl-C ends it cleanly.

The push key is the only identity the transfer offers: the ssh command sets `IdentitiesOnly=yes` and `IdentityAgent=none`. Without the second option, an agent or identity file that your ssh config names for the store host is offered as well, and a store host that accepts it runs the push with that key, so the key restricted to write-only `rrsync` is never exercised. To check on the store host, list the key fingerprints that authenticated the pusher's logins (`journalctl -u sshd | grep 'Accepted publickey'`); they must be the push key's `ssh-keygen -lf ~/.ssh/bakar-cache-push.pub` fingerprint, not your login key's.

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

### prune-downloads

```text
bakar cache prune-downloads [--apply] [--clone-older-than DAYS]
```

Plans the removal of downloads that are redundant, and removes them only with `--apply`. Without it the verb is a dry run: it prints what it would remove and deletes nothing. The directory comes from `DL_DIR`, else `dl_dir` in config; without one the verb prints an error and exits 1. Only the top level of the downloads directory and of `git2` is listed, and no symbolic link is followed.

Three classes are planned, each verified before it is listed:

| Class | What is removed | How it is verified |
|-------|-----------------|--------------------|
| `hub` | A top-level payload and its `.done` stamp | The hub (`dl_mirror_url`) answers HEAD for both with HTTP 2xx, and the payload's `Content-Length` equals the local size. A 404, another status, a missing length or a size mismatch keeps the file. After three consecutive unanswered requests the hub counts as down and the whole class removes nothing. Reported `unavailable` when `dl_mirror_url` is unset. |
| `tarball` | A `git2_<n>.tar.gz` mirror tarball and its stamp | The extracted clone `git2/<n>` is a real directory with a `HEAD` file and its own `.done` stamp beside it. |
| `clone` | A `git2/<n>` clone and its stamp | Only with `--clone-older-than DAYS`: the clone's stamp was last written more than DAYS days ago. Reported `not requested` otherwise. |

The clone age is the stamp's last write, not the last time a build used the clone. Both downloads filesystems are mounted `noatime`, so reads leave no trace, and a repository that every build still pins reads as old as its last fetch. Choose DAYS with that in mind, and expect a long-pinned repository to be re-cloned from upstream on its next build.

A clone is renamed to `git2/<name>.pruned.tmp` before it is deleted, so a delete that fails halfway never leaves a half-removed clone where BitBake would reuse it. The report counts that clone as kept (`remove failed`); the leftover directory is ignored by every later run and can be deleted by hand. The stamp is checked again under the lock, so a clone that was fetched after the plan was made is kept. Sizes count a hard-linked file once.

With `--apply`, each item is removed under the per-file lock BitBake uses: the verb takes the item's `.lock` without waiting, and an item whose lock is held is kept as `in use`. Under the lock it checks that the payload still has the planned size and modification time, and keeps it as `changed since planned` if not. The stamp is removed before the payload, so an interrupted removal leaves a payload BitBake treats as not downloaded. Other keep reasons are `lock unavailable` and `remove failed`.

Sample report from a dry run with `--clone-older-than 90`:

```text
hub: examined
  would remove 2 item(s), 1.4 GiB
  kept 1: hub lacks it
  kept 1: size mismatch
tarball: examined
  would remove 1 item(s), 310.0 MiB
  kept 1: no valid clone
clone: examined
  would remove 1 item(s), 2.2 GiB
  kept 3: too recent
dry run: nothing was removed (pass --apply to remove)
```

With `--apply` the verbs read `removed` instead of `would remove`, apply-time keep reasons are added to the `kept` lines, and the last line is `applied`.

When an `--apply` removed at least one `hub` item and `dl_mirror_paths` is configured, the output ends by naming `bakar cache link-mirror --apply`: a build directory that fetched a removed file earlier fails at unpack until a link replaces it.

### link-mirror

```text
bakar cache link-mirror [--apply]
```

Links files from the mounted mirror directories into the downloads directory, so a name the hub holds is also on disk locally. Without `--apply` the verb is a dry run: it prints what it would link and creates nothing. `--apply` is the only option.

The downloads directory comes from `DL_DIR`, else `dl_dir` in config. The mirror directories come from `dl_mirror_paths` only; the `BAKAR_DL_MIRROR_PATHS` variable is ignored. A directory that is not ready prints one yellow warning line and the others are used.

What it links: regular files directly inside a ready mirror directory that have a `<name>.done` stamp beside them in that directory. The first directory listed in `dl_mirror_paths` wins a name. Each link points at `<configured mirror dir>/<name>`, unresolved.

What it never does:

- Link `git2_` or `gitshallow_` names, names ending `.tmp`, `.lock` or `.done`, or the tarballs BitBake extracts with no checksum (`gitsmshallow_`, `gitannexshallow_`, `hg_`, `repo_`). svn, cvs and bzr tarballs have no fixed prefix, so the verb cannot recognise them and links them when stamped.
- Link a name BitBake uses as a downloads subdirectory (`git2`, `svn`, `cvs`, `bzr`, `hg`, `repo`, `p4`, `uninative`, `npm2`, `gomod`).
- Link a name whose `.done` stamp already exists locally without its payload: BitBake would reuse that stamp's checksums and never hash the linked file.
- Replace an existing entry: a file, a directory, and a live or dangling link are all left as they are.
- Create a stamp or a lock file.
- Read the contents of a mirror file.

| Exit | Meaning |
|------|---------|
| 0 | The run completed, including a dry run and a run that linked nothing |
| 1 | A message is printed and nothing is created: no downloads directory, the path is not a directory, `dl_mirror_paths` unset, the user config selects the container build, no mirror directory is ready, or the downloads directory cannot be read |

Report lines, in order:

```text
2 mirror directories listed, 412 payloads examined
unlisted: /mnt/hub/downloads-incoming
would link 3 file(s)
kept 8: already present
kept 5: git mirror tarball
kept 2: no stamp
dry run: nothing was linked (pass --apply to link)
```

The `unlisted: <dir>` line appears once per directory whose listing did not finish. `would link` reads `linked` with `--apply`, and the last line then reads `applied`. The `kept N: <reason>` lines carry one of `no stamp`, `git mirror tarball`, `unchecked tarball`, `reserved name`, `local stamp without payload`, `unsafe name`, `already present` or `link failed`.

See [features/dl-mirror.md](features/dl-mirror.md) for why a local name matters and for the limits.

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

`bakar doctor` carries six rows for this feature. See [doctor.md](doctor.md) for pass and fail conditions.

| Row | Severity | Fails when |
|-----|----------|------------|
| `cache-push-service` | WARN (INFO past 15 minutes) | The unit is inactive, or the oldest queued entry is over 60 minutes old (over 15 minutes is INFO) |
| `sstate-mirror-shared-mount` | BLOCK (WARN when undetermined) | An http(s) sstate mirror is set while the sstate directory is on NFS |
| `dl-mirror-paths-ready` | WARN | A directory listed in `dl_mirror_paths` is not ready for a host-mode build (skipped for container builds and when the key is unset) |
| `dl-mirror-links` | WARN | Host mode only, skipped when `dl_mirror_paths` or the downloads directory is unset. It creates the links `bakar cache link-mirror --apply` would create and reports INFO `linked N file(s) from the mirror: <up to 10 names>` or `nothing to link`. WARN only when a link could not be created (`N link(s) could not be created in <dir>; run bakar cache link-mirror`). INFO skip for a container build, and INFO when no mirror directory is ready (see `dl-mirror-paths-ready`). The mirror listing is bounded to 10 seconds. Never BLOCK |
| `store-stale-tarballs` | WARN | A `git2_*.tar.gz.tmp` file over an hour old sits at the top of the downloads directory |
| `dl-dir-size` | WARN | On a pusher host (`cache_push_target` set), the regular files in the downloads directory total over 60 GB; the message points at `bakar cache prune-downloads`. Skipped when the walk takes over 5 seconds |

## Related

[config-reference.md](config-reference.md), [doctor.md](doctor.md), [hashserv.md](hashserv.md), [capabilities/shared-cache-hub-push.md](capabilities/shared-cache-hub-push.md)
