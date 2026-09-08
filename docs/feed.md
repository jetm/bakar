# bakar feed

Manage the local package feed: stage a finished build's RPMs, render the
per-machine repositories, derive the index a client reads, serve the tree over
HTTP, and prune what accumulates. Seven verbs over one feed root, in the same
shape as `bakar prserv` - the workspace is resolved per verb via `--workspace`/`-w`
or by walking up from the current directory.

## Synopsis

```text
bakar feed doctor [KAS_YAML] [OPTIONS]
bakar feed sync    KAS_YAML  [OPTIONS]
bakar feed index  [KAS_YAML] [OPTIONS]
bakar feed serve  [KAS_YAML] [OPTIONS]
bakar feed stop   [KAS_YAML] [OPTIONS]
bakar feed status [KAS_YAML] [OPTIONS]
bakar feed gc     [KAS_YAML] [OPTIONS]
```

Only `bakar feed sync` requires the kas YAML: the deploy tree it stages belongs
to one build and the YAML is what names it. Every other verb addresses the feed
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
| `bakar feed index` | Write `targets.json` from what the channel has actually rendered |
| `bakar feed serve` | Serve the feed root over HTTP in the background |
| `bakar feed stop` | Stop the feed's static server |
| `bakar feed status` | Report what the feed holds, without starting anything |
| `bakar feed gc` | Prune old snapshots, stale metadata and orphaned pool entries |

## Common options

| Flag | Applies to | Description |
|------|------------|-------------|
| `--workspace`, `-w` | all seven | Workspace root; auto-detected if omitted |
| `--release` | `sync`, `index`, `status`, `gc` | Feed release directory (default: `2024`) |
| `--channel` | `sync`, `index`, `status`, `gc` | Feed channel directory (default: `edge`) |
| `--port` | `serve`, `status` | Port the static server binds (default: `8080`) |
| `--bind` | `serve` | Interface to bind (default: `127.0.0.1`, which keeps the feed off the network) |
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
- Extension repositories (the `-ext` suffix) are advertised by the index but
  never staged from a build; their content comes from the extension packaging
  flow.
- `src/bakar/feed_consolidate.py` and `src/bakar/feed_reclaim.py` back the
  consolidation and reclaim capabilities below, but neither is reachable from a
  `bakar feed` subcommand today - there is no CLI surface for them to document.

## See also

- [capabilities/local-package-feed.md](capabilities/local-package-feed.md) -
  staging, pooled rendering, index derivation, snapshot minting and static serving
- [capabilities/feed-consolidation.md](capabilities/feed-consolidation.md) -
  merging scattered RPM deploy trees into one canonical feed under a verification gate
- [capabilities/feed-retention.md](capabilities/feed-retention.md) -
  bounding pool and snapshot growth for a feed written on every build
- [build.md](build.md) - `--feed`, `--feed-release`, `--feed-channel`
- [configuration.md](configuration.md) - `feed_dir` and `feed_shared`
- [doctor.md](doctor.md) - the workspace-wide pre-flight checks
