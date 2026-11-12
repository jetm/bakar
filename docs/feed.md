# bakar feed

Manage the local package feed: stage a finished build's RPMs, render the
per-machine repositories, build chosen recipes and publish only their packages
as a delta, derive the index a client reads, serve the tree over HTTP, mirror a
published feed for offline use, and prune what accumulates. Nine verbs over one
feed root, in the same shape as `bakar prserv` - the
workspace is resolved per verb via `--workspace`/`-w` or by walking up from
the current directory.

## Synopsis

```text
bakar feed doctor [KAS_YAML] [OPTIONS]
bakar feed sync    KAS_YAML  [OPTIONS]
bakar feed refresh KAS_YAML --recipe RECIPE... --project DIR [OPTIONS]
bakar feed mirror  SOURCE_URL [--target MACHINE]... [--repo PATH]... [--metadata-only] [OPTIONS]
bakar feed index  [KAS_YAML] [OPTIONS]
bakar feed serve  [KAS_YAML] [OPTIONS]
bakar feed stop   [KAS_YAML] [OPTIONS]
bakar feed status [KAS_YAML] [OPTIONS]
bakar feed gc     [KAS_YAML] [OPTIONS]
```

`bakar feed sync` and `bakar feed refresh` require the kas YAML: the deploy tree
they stage belongs to one build and the YAML is what names it. Every other verb addresses the feed
itself, which the configuration already locates, so the YAML is optional there.
Pass it anyway when it is to hand - without it the workspace comes from a CWD
walk, which can land on a different root than the one a sync used. Every verb
prints the feed root it resolved, so a wrong root is visible before anything
destructive happens.

## Subcommands

| Command | What it does |
|---------|--------------|
| `bakar feed doctor` | Check everything a sync needs, without touching the feed |
| `bakar feed sync` | Stage a finished build and render every repository it declares |
| `bakar feed refresh` | Build the named recipes and publish only their packages as a delta feed |
| `bakar feed mirror` | Copy repositories of a published feed into the local feed, or only their metadata |
| `bakar feed index` | Write `targets.json` from what the channel has actually rendered |
| `bakar feed serve` | Serve the feed root over HTTP in the background |
| `bakar feed stop` | Stop the feed's static server |
| `bakar feed status` | Report what the feed holds, without starting anything |
| `bakar feed gc` | Prune old snapshots, stale metadata and orphaned pool entries |

## Common options

| Flag | Applies to | Description |
|------|------------|-------------|
| `--workspace`, `-w` | all nine | Workspace root; auto-detected if omitted |
| `--release` | `sync`, `refresh`, `mirror`, `index`, `status`, `gc` | Feed release directory. For `sync`, `refresh`, and `index` given a kas YAML, the default is the release the finished build declares in its distribution codename; `2024` applies when it declares none, and for `mirror`, `status`, `gc` and `index` without a kas YAML |
| `--channel` | `sync`, `refresh`, `mirror`, `index`, `status`, `gc` | Feed channel directory. For `sync`, `refresh`, and `index` given a kas YAML, the default is the channel the finished build declares in its distribution codename; `edge` applies when it declares none, and for `mirror`, `status`, `gc` and `index` without a kas YAML |
| `--base-release`, `--base-channel` | `refresh` only | Release and channel of the published feed the delta is built against; each defaults to the resolved `--release` / `--channel` |
| `--base-snapshot` | `refresh` only | Snapshot id of the published feed the delta is built against, recorded in `delta.json`; `null` when omitted. It is never read from the local synced feed |
| `--recipe` | `refresh` only | Recipe to build and publish into the delta (repeatable, at least one required) |
| `--project` | `refresh` only | The avocado project directory the printed repository stanzas are relative to (required) |
| `--serve` | `refresh` only | Serve the delta over HTTP after publishing |
| `--advertise` | `refresh` only | Host printed in the served stanza; defaults to the bound address |
| `--snapshot` | `refresh` only | Mint a snapshot in the delta, as `sync` does |
| `--run-avocado` | `refresh` only | Run the printed `avocado unlock`, `install` and `build` commands in `--project` |
| `--metadata-only` | `mirror` only | Mirror repository metadata and no package files |
| `--target` | `mirror` only | Machine name to resolve through the source's `targets.json` (repeatable) |
| `--repo` | `mirror` only | Repository path to mirror directly, e.g. `sdk/all` (repeatable) |
| `--port` | `serve`, `status`, `refresh` (with `--serve`) | Port the static server binds (default: `8080`) |
| `--bind` | `serve`, `refresh` (with `--serve`) | Interface to bind (default: `127.0.0.1`, which keeps the feed off the network) |
| `--keep` | `gc` | Snapshots to retain by age, minimum `1` (default: `3`) |
| `--confirm` | `gc` | Actually remove; without it `gc` only previews |

Two releases are live in practice and their repository metadata must stay
separate, which is why `--release`/`--channel` exist at all - one release's
target resolving another release's packages is the failure being avoided.

## bakar feed doctor

Runs with or without a kas YAML, and without a workspace at all. The host-tool
tier (interpreter, `createrepo_c`, the shell tools, platform) has an answer
before any workspace exists, so the first command run on a fresh machine reports
the real problem rather than a workspace-detection failure. Adding a workspace
extends the checks to the feed paths; adding a kas YAML extends them again to
the layer checkout and the build output. Exits `1` when any check blocks.

`bakar feed sync` runs the same prerequisite set up front and reports only the
failures, so a first run on a fresh machine surfaces every missing prerequisite
at once instead of one round trip per exception.

## bakar feed sync

Stages the build's deploy tree, renders each repository the machine's repo map
declares, mints an immutable snapshot, and writes the `snapshots-latest.json`
pointer clients auto-pin against. It prints the feed root, the snapshot id, the
channel root, the rendered repositories, the pinned machines, and any repository
that was declared but not built - the last is reported, not treated as a
failure, because a repo map lists what a machine *could* publish.

The staging and render steps are shell/Python scripts from the layer checkout,
run with `check=True`. A non-zero exit leaves the snapshot pointer unwritten by
design, so the feed stays intact and the command exits `1` naming the script
that failed.

`bakar build --feed` runs a sync and then an index once the build has succeeded,
with `--feed-release`/`--feed-channel` mirroring the flags here. A failed build
never syncs, and neither does `--dry-run`.

## bakar feed refresh

Builds the named recipes with kas and publishes only their packages as a delta
feed, then prints what an avocado project needs to consume it. It is the
quick loop for a recipe under development: the full `sync` renders every
repository a machine declares from a finished image build, while `refresh`
renders just the packages of the recipes you named.

```bash
bakar feed refresh meta-avocado/kas/machine/qemux86-64.yml \
  --recipe htop --recipe my-app \
  --project ~/avocado-project --release 2026 --channel edge
```

At least one `--recipe` and a `--project` directory that exists are required;
without them the command exits `2` before resolving anything. The workspace
comes from `--workspace`/`-w` or the usual walk up from the current directory.

**Where the delta lands.** Both locations are siblings of the feed root, never
inside it, because the feed root is what `serve` exposes and a refresh wipes
them:

| Path | Holds |
|------|-------|
| `<feed_root>-delta` | The delta feed root; the published repositories are under `<release>/<channel>` |
| `<feed_root>-delta-stage` | The delta's stage root, which the publish step unions into |

The `<release>/<channel>` directory of each is wiped and rewritten on every
refresh, so the delta always equals the last request. The ordinary feed and its
stage root are not touched.

**Pipeline.** The recipes build first; a build that fails leaves the previous
delta untouched. Then bakar builds a filtered view of the deploy directory that
holds only the packages of the named recipes (the repo map is kept, because the
staging script exits when it is missing), and publishes that view head-only: the
repositories are rendered at the live head, and no snapshot, no SBOM and no
`snapshots-latest.json` pointer is written, so a client cannot pin against the
delta. Pass `--snapshot` to mint a snapshot as `sync` does. The delta
directory also receives two files:

| File | Content |
|------|---------|
| `recipe-map.json` | One entry per recipe: its RPM package names, layer (the layer repository's directory name, e.g. `meta-openembedded`), layer commit, `epoch`, `pv`, `pr`, `srcrev`, machine, release and `task_hash` |
| `delta.json` | The base release and channel the delta was built against, the recipes built, the build's run id and the delta path |

In `recipe-map.json`, `pr` is the RPM release as it appears in the package file
name (the `PKGR` value in the runtime package data, for example `r0.1` with the
PR-server suffix), not the recipe's own `PR`. `task_hash` is the bitbake
taskhash read from the build's stamps and is written as `null` when it cannot be
read unambiguously; the comparison between two builds of a recipe then falls
back to `epoch`, `pv`, `pr`, `srcrev` and the layer commit. `layer_commit` is written as `null`
when the layer repository's commit cannot be determined, never as an empty
string. A recipe that several layers provide is recorded under the layer
holding the built recipe's own file when pkgdata names it, else the first
layer, and the refresh prints a warning naming every layer.

**Guards.** Each of these refuses with exit `1` before anything is written, or
(for the build-dependent ones) before the delta is wiped, and names the way out:

- A second refresh holds the refresh lock, `<feed root>-delta.lock`, a file beside
  the feed root (never inside it). Refreshes are not
  queued, because the second would wipe the delta the first is still building.
- A build is running in the workspace. A running build rewrites the deploy tree
  the refresh stages from; wait for it or run `bakar stop`.
- A repository under the delta channel directory carries `.bakar-mirror.json`,
  so `bakar feed mirror` created it. A refresh would replace another publisher's
  metadata; choose another feed root or remove the mirrored repository.
- The delta stage root resolves (symlinks followed) to the synced feed root or
  to the stage root `feed sync` uses. A refresh would wipe the packages those
  hold.
- A named recipe is in none of the configured layers, or the build produced no
  packages for it.
- `avocado-repo.map` in the deploy directory is older than packages already
  there that this refresh did not build, so a sync from it could omit them.
  Rebuild an image or the `avocado-complete` target to rewrite the map, then
  retry.

The map check is shaped by one measured behavior: a build of a leaf recipe does
not rewrite `avocado-repo.map` (it is written by the `avocado-complete`
target, not by the recipe). The refresh therefore compares the map only against
packages that were in the deploy tree before the build; comparing against the
packages it just built would refuse every refresh.

The preflight that `bakar feed doctor` reports runs before the build, so a
missing prerequisite costs seconds. With `--serve` the port is also checked
before the build, and a port already in use exits `1` without building.

**Output.** The sentences meant for a person go to stderr; the stanza and the
commands go to stdout, so you can paste or pipe them. The stanza is a `repos:`
map keyed by feed name, one entry per delta repository that holds packages. A
directory entry carries the repository `path`, relative to `--project`, and its
`stages`:

```yaml
repos:
  bakar-delta-target-qemux86-64:
    path: ../avocado-feed-delta/2026/edge/target/qemux86-64
    stages: [rootfs, runtime]
  bakar-delta-sdk-all:
    path: ../avocado-feed-delta/2026/edge/sdk/all
    stages: [sdk]
```

Feed names are `bakar-delta-` plus the repository path with `/` replaced by
`-`. Repositories under `sdk/` list the `sdk` stage; all others list `rootfs`
and `runtime`. The three avocado commands follow the stanza:

```text
avocado unlock
avocado install
avocado build
```

The paths and repository names above are illustrative; the command prints the
real ones for your feed root.

With `--serve`, bakar also starts the static server over the delta root on
`--bind`/`--port` (defaults `127.0.0.1` and `8080`) and prints a second `repos:`
map of `url:` entries under a `# served alternative to the directory repos above`
comment. The URL host is the bound address unless `--advertise HOST` names
another, because an address the server is not listening on is never advertised
on its own. If nothing comes up on the address, the command exits `1`, prints
the directory stanza without a URL stanza and skips the avocado run.

With `--run-avocado`, bakar runs `avocado unlock`, `avocado install` and
`avocado build` in `--project`, one after another, stopping at the first
failure (exit `1`). Each command has a one-hour limit. Without the flag, no
avocado process starts, and no file in the project is modified: the stanza is
printed for you to paste.

## Mirroring a published feed

`bakar feed mirror SOURCE_URL` copies repositories of a remote, already-published
feed into the local feed, so a lab or bench host with no board-support checkout
can still build offline against a real package set. It requires at least one
`--target` or `--repo`; called with neither it refuses before making any request.

**What is copied.** Only a repository's current head: `repomd.xml`, the metadata
files it names, and every package those list. A source's `snapshots-latest.json`
pointer and its `snapshots/` tree are never requested or written - the mirror has
no notion of the source's snapshot history, only of what its head currently
publishes.

**Selection.** `--target MACHINE` resolves through the *source's own*
`targets.json` to the repository paths that machine locks, the same way a
client resolves a machine today. `--repo PATH` names a repository directly (for
example `sdk/all`), bypassing target resolution. Both are repeatable and can be
combined. A repository the index declares but the source does not currently
publish is skipped and reported as `declared but not published: ...`, the same
shape `sync` uses for a repo a repo map lists but a build did not stage.

**Integrity.** Every metadata file and every package is verified against the
checksum its own `repomd.xml`/primary listing declares before it is written;
a mismatch retries and then fails the run naming the file. `repomd.xml` itself
is written last and atomically, after every package it names has verified -
so a client can never resolve a `repomd.xml` pointing at packages that are only
partially there. A source-published detached signature (`repomd.xml.asc`) is
copied immediately before `repomd.xml`, never after.

**Confinement.** The mirror writes only inside the target repository directory
or the channel's shared `_pkgs` pool - never outside the channel root, however a
source's metadata names a file. A location outside those two places is refused
before any request is made for that repository.

**Ownership.** A repository this run did not create is never overwritten: if
the local repository already carries a `repomd.xml` or a snapshot pointer with
no matching `.bakar-mirror.json` marker (or a marker naming a different
source), the run is refused for that repository before its first request, and
the message says to choose another feed root (`-w` or `[build] feed_dir`) or
remove the local repository directory. `.bakar-mirror.json` is the provenance
marker mirror writes into each repository it publishes, recording the source
channel that repository came from - this is what the ownership check reads
back on a later run.

**Resume.** Re-running the same command is safe and cheap: a package whose
existing bytes already hash to the declared checksum is reused rather than
re-fetched, so an interrupted mirror picks up where it left off.

**Disk space** is checked with a preflight against every unique package this
run needs, before any package is requested - a run that would not fit fails up
front rather than partway through.

`bakar feed gc` needs no mirror-specific handling: it prunes stale metadata and
orphaned pool files exactly as it does after a `sync`, so re-running `mirror`
against an updated source and then `gc` reclaims what the previous mirror left
behind.

### Metadata-only mirrors

`bakar feed mirror ... --metadata-only` fetches and verifies each repository's
`repomd.xml`, its signature and the metadata files it names, and downloads no
package. The repository then resolves and lists what the source publishes, and
a client that needs the files fetches them elsewhere. Because nothing is
downloaded, the disk-space preflight is skipped and the summary reports
`downloaded: 0 packages`.

The marker `.bakar-mirror.json` of every mirrored repository, full or not,
holds the keys `source`, `revision`, `mirrored`, `repomd_sha256` and `mode`.
`mode` is `metadata-only` or `full`. A metadata-only mirror writes no snapshot
pointer, like any mirror.

Because its metadata names packages the mirror never fetched, a metadata-only
repository would read as full of dangling references to `bakar feed gc`. `gc`
therefore skips its reference audit for a repository whose marker says
`metadata-only`. Any doubt (no marker, no `mode` key, an unreadable marker)
keeps the repository audited, so a real dangling reference is still reported.

### Do not mix sync and mirror

Do not mix `sync` and `mirror` for the same repository. `sync` renders it from
a local build's deploy tree and `mirror` copies it from a remote source's head.
The mirror side is guarded by the ownership check above, which refuses to
overwrite a repository it did not create. The sync side is guarded too: a sync
(and so `bakar build --feed` and `bakar feed refresh`) refuses to publish into a
channel directory in which any repository carries `.bakar-mirror.json`, whether
or not the build declares that repository, and says to choose another feed root
(`-w` or `[build] feed_dir`) or to remove the mirrored repository. A
metadata-only mirror holds no packages, so rendering a local build over it
would leave two publishers under one head and a repository that resolves
packages it does not hold. Give each publisher its own feed root.

### Offline-lab walkthrough

A bench host with no board-support layer checkout at all - only `bakar` and
Python - can still stand up a local feed from a published one:

```bash
bakar feed mirror https://repo.avocadolinux.org --target rzv2n-sr-som -w ~/avocado-lab
bakar feed index -w ~/avocado-lab
bakar feed serve --bind 0.0.0.0 -w ~/avocado-lab
```

`mirror` and `serve` need only Python - neither touches a layer checkout or a
build tool. `bakar feed doctor`'s `createrepo_c` check applies to `sync`, which
this workflow never runs.

## bakar feed index

Derives `targets.json` from the repositories that have actually rendered in the
channel, rather than from what a map declares. Refuses with exit `1` when the
channel directory does not exist, because writing the index would otherwise
`mkdir` a channel created by a typo in `--release`/`--channel` and then report
`(none rendered yet)` as though it were merely unbuilt.

## bakar feed serve and bakar feed stop

`bakar feed serve` starts a static server over the feed root in the background
and records its pid and port in a state file under the feed root. If a server is
already up for that root it says so and returns without starting a second one.
When nothing comes up on the requested address it exits `1` and points at
`--port`, the port already being in use being the usual cause.

`bakar feed stop` stops that server, printing `stopped` or `not running`.

The `127.0.0.1` default is deliberate: the feed is not exposed to the network
unless you pass `--bind` explicitly.

## bakar feed status

Reports the feed root, the channel root, the rendered targets, the pool entry
count, the snapshots present, and whether a server is running (with its URL).
It also prints the size of the stage root, which is a separate growth source
that `bakar feed gc` does not touch - without that line, "gc freed nothing"
would read as "nothing is using disk".

## bakar feed gc

Previews by default; `--confirm` is required before anything is removed. The
asymmetry is deliberate: a retained snapshot costs disk, a wrongly removed pool
entry costs a rebuild.

`gc` refuses with exit `1` when the channel directory does not exist, and says
that nothing was examined - planning an absent channel would report a clean
no-op for a tree you never synced.

It also refuses to do partial damage when it cannot see the whole picture:

- Pointers that exist but name no snapshot mean the set of snapshots clients are
  pinning is unknown, so no snapshot is removed.
- Package lists that cannot be read mean the set of referenced pool entries is
  unknown, so pool reclamation is suppressed.
- Entries under `snapshots/` that are not snapshot ids (or are symlinks) are
  left alone and not counted against `--keep`.
- Repositories whose `repomd.xml` cannot be read are skipped with their metadata
  untouched.

After a confirmed run it audits the result. A dangling reference (an index still
naming a package that is gone) or an unreadable package list makes the command
exit `1` - a dangling reference fails at download rather than at resolve, which
is worse than a missing repository.

> Gap: `--keep` is the only retention control on the CLI, and it bounds
> snapshots by age only. The rules deciding which pool entries count as orphaned
> and which metadata counts as stale are not surfaced in `--help`; see
> `src/bakar/feed_retention.py` for the plan/apply model behind them.

## Concurrent writers

Every command that writes the feed holds an exclusive lock on
`<feed root>/.bakar-feed.lock` for its whole write sequence: `sync` (stage,
render and index, including the sync that `bakar build --feed` runs), `mirror`,
`index` and `gc --confirm`. A second writer waits, printing `waiting for the feed lock
<path>` once, and proceeds when the first finishes. `status` and `serve` only
read and never take the lock, and neither does the default `gc` preview, which writes nothing.
`refresh` writes only the `-delta` and `-delta-stage` siblings of the feed root,
never the feed root itself, so it holds its own `<feed root>-delta.lock`
(a sibling file) instead; `mirror --metadata-only` writes the feed root and takes the lock like `mirror`.

- `BAKAR_FEED_LOCK_TIMEOUT` is the number of seconds to wait; the default is 900.
- On timeout the command exits 1 naming the feed and the lock file, and writes
  nothing. Under `bakar build --feed` the timeout is reported as "the feed was
  not updated" and the build's own exit code is left alone, as for any other feed
  failure; re-run `bakar feed sync` afterwards.
- The lock is an advisory POSIX lock (`lockf`), so a writer that is killed
  (including SIGKILL) releases it by itself. Do not delete `.bakar-feed.lock`.
- It is a POSIX lock, not `flock`, so a writer on the main node's local disk and
  a writer on an NFS client exclude each other: NFS clients send both kinds to
  the server as POSIX locks, and on the server `flock` and POSIX locks do not
  conflict. Every host that writes the feed must run a build with this lock; a
  host still on the `flock` build is not excluded from the others.
- The feed root is created if it does not exist. If it or the lock file cannot be
  written the command fails with the reason rather than running unlocked.
- The lock covers the feed root only; the stage root beside it is not separately
  locked.

## Feed layout and configuration

The feed root is per-workspace by default (`<workspace>/_feed`), so two
workspaces do not render into one another's feed. Two settings change that:

| Setting | Effect |
|---------|--------|
| `[build] feed_dir` | Explicit feed path, honored verbatim |
| `[build] feed_shared` | Single shared feed under the XDG data home (`~/.local/share/bakar/feed`) |

Inside the root, a channel is `<feed_root>/<release>/<channel>`; the content pool
lands at `<release>/<channel>/_pkgs`, per-machine repositories under `target/`,
immutable snapshots under `snapshots/`, and the client pointer at
`snapshots-latest.json`. Staging happens in a *sibling* of the feed root named
`<feed_root>-stage`, never inside it, because the feed root is what gets served.
A repository `bakar feed mirror` publishes also carries a `.bakar-mirror.json`
marker alongside its `repomd.xml`, recording the source channel it was copied
from; the ownership guard in "Mirroring a published feed" above reads this file
back on every later mirror run.

`bakar feed refresh` uses two further siblings of the feed root:
`<feed_root>-delta` and `<feed_root>-delta-stage`, described under
"bakar feed refresh".

## Examples

```bash
# Check prerequisites on a fresh machine, before any workspace exists
bakar feed doctor

# Full check for one build's feed sync
bakar feed doctor meta-avocado/kas/machine/qemux86-64.yml

# Stage a finished build and render its repositories
bakar feed sync meta-avocado/kas/machine/qemux86-64.yml

# Same, into a non-default release/channel
bakar feed sync meta-avocado/kas/machine/qemux86-64.yml --release 2026 --channel stable

# Stage a wrynose build with no flags: release and channel come from the
# distribution codename the build declares. An explicit pair that disagrees with the build is still refused by the preflight.
bakar feed sync meta-avocado/kas/machine/qemux86-64.yml

# Build two recipes and publish only their packages as a delta,
# serving it to an avocado project
bakar feed refresh meta-avocado/kas/machine/qemux86-64.yml \
  --recipe htop --recipe my-app --project ~/avocado-project \
  --release 2026 --channel edge --serve --advertise 192.0.2.10

# Mirror only the metadata of a published feed
bakar feed mirror https://repo.avocadolinux.org --target qemux86-64 --metadata-only

# Rewrite the client index from what has rendered
bakar feed index

# Serve it locally and check what is there
bakar feed serve --port 8080
bakar feed status
bakar feed stop

# Preview a prune, then apply it
bakar feed gc --keep 5
bakar feed gc --keep 5 --confirm
```

## Notes

- The feed depends on `createrepo_c` plus shell tools that `pip` cannot install.
  `bakar feed doctor` is the fastest way to find out which are missing.
- A layer repository that is a shallow clone breaks the ancestor test that
  decides whether the loop's cache verdict treats a recipe's cached package as current: the test asks
  whether the commit a delta recorded for the recipe's layer is an ancestor of
  the layer's current commit, and a shallow clone may not hold that history.
  Fetch the full history of a shallow layer clone (`git fetch --unshallow`).
  The workspace the loop was measured in had no shallow layer: all 45 layer
  repositories reported not shallow, and the smallest held 7 commits.
- Extension repositories (the `-ext` suffix) are advertised by the index but
  never staged from a build; their content comes from the extension packaging
  flow.
- `src/bakar/feed_consolidate.py` backs the consolidation capability below, but
  it exposes no `bakar feed` subcommand, so there is no CLI surface to document.
  The module is still live: `feed_preflight.py` imports `_read_testdata` and
  `_split_codename` from it, and `bakar feed doctor` and `bakar feed sync` both
  run that preflight. Do not read "no CLI surface" as "safe to delete".

## See also

- [capabilities/local-package-feed.md](capabilities/local-package-feed.md) -
  staging, pooled rendering, index derivation, snapshot minting and static serving
- [capabilities/feed-consolidation.md](capabilities/feed-consolidation.md) -
  merging scattered RPM deploy trees into one canonical feed under a verification gate
- [capabilities/feed-retention.md](capabilities/feed-retention.md) -
  bounding pool and snapshot growth for a feed written on every build
- [capabilities/feed-mirror.md](capabilities/feed-mirror.md) -
  populating the local feed from a published feed
- [build.md](build.md) - `--feed`, `--feed-release`, `--feed-channel`
- [configuration.md](configuration.md) - `feed_dir` and `feed_shared`
- [doctor.md](doctor.md) - the workspace-wide pre-flight checks
