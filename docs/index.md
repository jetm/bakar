# bakar documentation

## Quick navigation

| Command | Doc | One-liner |
|---------|-----|-----------|
| `setup` | [setup.md](setup.md) | Prepare the host once: profile, remediate doctor host checks, persist `[host]` config |
| `init` | [init.md](init.md) | Interactive wizard: scaffold a new workspace and write `.bakar.toml` |
| `build` | [build.md](build.md) | Full pipeline: doctor, sync, gen-kas, kas build |
| `sync` | [sync.md](sync.md) | Sync sources without building |
| `gen-kas` | [gen-kas.md](gen-kas.md) | Regenerate kas YAML from manifest |
| `bitbake` | [bitbake.md](bitbake.md) | Run a single recipe or task through bitbake, with run logging |
| `clean-recipe` | [bitbake.md](bitbake.md) | Clean one recipe's sstate (`bitbake -c cleansstate`) |
| `rebuild` | [bitbake.md](bitbake.md) | Rebuild one recipe from scratch (`cleansstate` then build) |
| `shell` | [shell.md](shell.md) | Interactive kas shell or one-shot command |
| `run` | [run.md](run.md) | Boot avocado-os image in QEMU (meta-avocado only) |
| `ps` | [ps.md](ps.md) | List every live `bakar` build on this host (host + container mode), plain text or `--json` |
| `status` | [status.md](status.md) | Report a run's lifecycle state (starting, running, succeeded, failed, unknown) from a recorded exit status; `--json` for scripting |
| `wait` | [wait.md](wait.md) | Block until a run finishes and exit with its outcome (0, 1, 2, 3, 124 on timeout) |
| `stop` | [stop.md](stop.md) | Gracefully halt a running build (SIGINT, then escalate) |
| `clean` | [clean.md](clean.md) | Remove the build directory |
| `clean-cache` | [clean-cache.md](clean-cache.md) | Prune stale sstate and ccache entries by age |
| `doctor` | [doctor.md](doctor.md) | Run pre-flight checks (add `--post-build` to also scan a finished build's native tree) |
| `triage` | [triage.md](triage.md) | Post-mortem a failed build |
| `report` | [report.md](report.md) | Summarize a completed build run |
| `insights` | [insights.md](insights.md) | Per-recipe/per-task analytics: sstate, timing, pressure, disk; `--natives` explains native/cross rebuilds |
| `log` | [log.md](log.md) | Tail a run log live |
| `monitor` | [monitor.md](monitor.md) | One-view live watch: cluster load, dist stats, task progress |
| `cluster-info` | [cluster-info.md](cluster-info.md) | Print live sccache-dist scheduler capacity: servers, CPUs, jobs in flight |
| `sched-triage` | [sched-triage.md](sched-triage.md) | Post-hoc cluster-utilisation report from the scheduler journal and client error log |
| `layers` | [layers.md](layers.md) | Print layer git hashes, branches, priority, and build status |
| `show` | [show.md](show.md) | Print resolved build picture: config, overlays, layers, sources, command |
| `getvar` | [getvar.md](getvar.md) | Resolve a bitbake variable (or a `--flag` of one) and show where it was set; value on stdout, diagnostics on stderr |
| `inspect` | [inspect.md](inspect.md) | Deep per-recipe report: identity, sources, paths, inherits, packages, deps |
| `graph` | [graph.md](graph.md) | Analyze a recipe's dependency graph: blast radius, longest chain, cycles |
| `diffsigs` | [diffsigs.md](diffsigs.md) | Show what changed in a task signature (why did this rebuild) |
| `for-all` | [for-all.md](for-all.md) | Run a shell command in every source repo |
| `presets` | [presets.md](presets.md) | Manage named build presets (`list`, `show`, `add`, `remove`) |
| `settings` | [settings.md](settings.md) | Read and write `~/.config/bakar/config.toml` |
| `lock` | [lock.md](lock.md) | Pin floating layer SHAs |
| `diff` | [diff.md](diff.md) | Compare two manifest versions |
| `drift` | [drift.md](drift.md) | Compare workspace pinned SHAs against actual checked-out commits |
| `changelog` | [changelog.md](changelog.md) | Generate release notes between two pinned workspace states |
| `prefetch` | [prefetch.md](prefetch.md) | Pre-fetch recipe sources into DL_DIR |
| `mirror` | [mirror.md](mirror.md) | Seed a premirror `git2_*.tar.gz` tarball from a git URL (host-side) |
| `sstate-seed` | [sstate-seed.md](sstate-seed.md) | Populate and inspect the native/cross sstate seed |
| `feed` | [feed.md](feed.md) | Manage the local package feed: stage, mirror, index, serve, and prune a build's RPMs |
| `dump` | [dump.md](dump.md) | Inspect the resolved kas YAML |
| `hashserv` | [hashserv.md](hashserv.md) | Manage the persistent bitbake-hashserv daemon |
| `prserv` | [prserv.md](prserv.md) | Manage the workspace-scoped bitbake-prserv daemon that keeps package revisions monotonic |
| `bitbake-override` | [bitbake-override.md](bitbake-override.md) | Swap BSP-bundled bitbake for upstream |
| `stress-parse` | [stress-parse.md](stress-parse.md) | Stress-test bitbake parser fork race |
| Configuration | [configuration.md](configuration.md) | Env vars, config.toml, vendors.toml, telemetry layout |
| Config reference | [config-reference.md](config-reference.md) | All options: types, defaults, and descriptions for every config key |
| Shell completion | [completion.md](completion.md) | Tab-completion setup for bash, zsh, and fish |
| Workspace | [workspace.md](workspace.md) | Workspace detection, BSP families, directory layouts |

---

## Which command do I need?

**Starting a build:**
- First time on this machine, prepare the host: [setup.md](setup.md)
- First time with a manifest: [build.md](build.md)
- Already synced, just want to rebuild: `bakar build --skip-sync`
- Run a build that outlives your terminal or tool call: `bakar build --detach` ([build.md](build.md)), then follow it with [status.md](status.md) and [wait.md](wait.md); driving bakar from an agent or a script: [agents.md](agents.md)
- Only want to sync sources: [sync.md](sync.md)
- Only want to regenerate the kas YAML: [gen-kas.md](gen-kas.md)

**Build failed:**
- List every live build on this host to find the one to stop: [ps.md](ps.md)
- Stop a running build cleanly so it stays resumable: [stop.md](stop.md)
- Find what went wrong: [triage.md](triage.md)
- Watch a running build (tail one log): [log.md](log.md)
- Watch a running build (cluster + dist + task progress in one view): [monitor.md](monitor.md)
- See how much sccache-dist cluster capacity is available right now: [cluster-info.md](cluster-info.md)
- Explain after the fact why a build distributed poorly: [sched-triage.md](sched-triage.md)
- Check if the environment is sane: [doctor.md](doctor.md)
- Rebuild or re-run a task on one recipe: [bitbake.md](bitbake.md)
- Wipe one recipe's sstate and rebuild it in one go: [bitbake.md](bitbake.md) (`rebuild`)
- Force a from-scratch rebuild: [clean.md](clean.md) or `bakar build --clean`

**After a successful build:**
- Summarize timing, image size, layer SHAs: [report.md](report.md)
- Per-recipe sstate/timing/pressure/disk analytics: [insights.md](insights.md)
- Inspect layer commits: [layers.md](layers.md)

**Inspecting the build before or after:**
- What machine/distro/image will resolve: [show.md](show.md)
- What a variable (or one of its flags, via `--flag`) resolves to and where it was set: [getvar.md](getvar.md)
- Capture a resolved value in a script (`v=$(bakar getvar MACHINE ...)`): [getvar.md](getvar.md) - the value goes to stdout, progress and errors to stderr
- What a recipe pulls in (packages, deps, paths): [inspect.md](inspect.md)
- Per-layer priority, compat, and provided recipes: [layers.md](layers.md) (`layers inspect`)
- Project-level MACHINE, DISTRO, thread/mirror config: [layers.md](layers.md) (`layers status`)
- Why a task missed sstate and rebuilt: [diffsigs.md](diffsigs.md)
- Why native and cross recipes rebuilt across a whole run: [insights.md](insights.md) (`bakar insights --natives`)
- A recipe's dependency graph (blast radius, longest chain, cycles): [graph.md](graph.md)

**Reproducibility and snapshots:**
- Pin current SHAs: [lock.md](lock.md)
- See what changed between manifest versions: [diff.md](diff.md)
- Check how far the workspace has drifted from pinned SHAs: [drift.md](drift.md)
- Generate release notes between two pinned states: [changelog.md](changelog.md)
- Pre-fetch sources for an offline build: [prefetch.md](prefetch.md)
- Seed a premirror tarball from a git URL (host-side): [mirror.md](mirror.md)
- Populate or check the native/cross sstate seed: [sstate-seed.md](sstate-seed.md)
- Inspect the exact config kas will receive: [dump.md](dump.md)

**Exploring the source tree:**
- Interactive shell inside the kas environment: [shell.md](shell.md)
- Run a git or shell command in every layer: [for-all.md](for-all.md)

**Configuration:**
- Persist default machine/image/distro: [settings.md](settings.md)
- Tune sstate mirrors, DL_DIR, container image: [settings.md](settings.md)
- All config keys with types and defaults: [config-reference.md](config-reference.md)
- Understand env vars and priority order: [configuration.md](configuration.md)
- Workspace layout and BSP family auto-detection: [workspace.md](workspace.md)
- Name a build configuration and invoke it by name: [presets.md](presets.md)
- Set up tab-completion for bash, zsh, or fish: [completion.md](completion.md)

**Advanced:**
- Swap BSP bitbake for a local upstream checkout: [bitbake-override.md](bitbake-override.md)
- Boot a QEMU image from the build directory: [run.md](run.md)
- Reproduce and measure the bitbake parser race: [stress-parse.md](stress-parse.md)
- Persistent hash equivalence across builds: [hashserv.md](hashserv.md)
- Keep package revisions monotonic across a wiped build tree: [prserv.md](prserv.md)
- Publish a finished build's RPMs as a local package feed: [feed.md](feed.md)
- Copy a published feed for offline use: [feed.md](feed.md) (`bakar feed mirror`)
- Run a build on an idle remote node (deliver the layer or mirror the tree additively, build over ssh): [build.md](build.md) (`--on <host>`)

---

## Command groups

### Build pipeline

```text
bakar doctor    - pre-flight (runs automatically before build)
bakar sync      - fetch/update sources
bakar gen-kas   - translate manifest → kas YAML
bakar build     - all of the above, then kas build
```

Related: [build.md](build.md), [sync.md](sync.md), [gen-kas.md](gen-kas.md), [doctor.md](doctor.md)

### Inspection

```text
bakar show              - resolved config, overlays, layers, sources (local, no container)
bakar getvar <VAR>      - variable resolution and provenance via bitbake-getvar / bitbake -e
                          (--flag FLAG or VAR[FLAG] for a flag; value on stdout, diagnostics on stderr)
bakar inspect <recipe>  - per-recipe report: identity, sources, paths, inherits, packages, deps
bakar layers inspect    - per-layer priority, compat, version, provides
bakar layers status     - project summary: MACHINE, DISTRO, threads, mirrors, hashserv
bakar diffsigs <r> <t>  - why did this task rebuild (bitbake-diffsigs)
```

Related: [show.md](show.md), [getvar.md](getvar.md), [inspect.md](inspect.md), [layers.md](layers.md), [diffsigs.md](diffsigs.md)

### Recipe operations

```text
bakar bitbake <target>   - run one recipe/task through bitbake, logged (--task, --keep-going)
bakar clean-recipe <r>   - clean one recipe's sstate (bitbake -c cleansstate)
bakar rebuild <r>        - rebuild one recipe from scratch (cleansstate then build)
bakar graph <recipe>     - dependency graph analysis: blast radius, longest chain, cycles
```

Related: [bitbake.md](bitbake.md), [graph.md](graph.md)

### Observability

```text
bakar ps        - list every live build on this host, host + container mode (--json for scripting)
bakar status    - one lifecycle state for a run: starting, running, succeeded, failed, unknown (--json)
bakar wait      - block until a run finishes and exit with its outcome (0, 1, 2, 3, 124 on --timeout)
bakar log       - tail a live build log
bakar monitor   - one-view live watch: cluster load, dist stats, task progress
bakar triage    - surface the failing recipe/task from bitbake-events.json (--run/--preset/--release select the run dir)
bakar report    - summarize a completed run (timing, image size, layers)
bakar insights  - per-recipe/per-task analytics: sstate, timing, pressure, disk (--natives: why natives rebuilt)
bakar layers    - print layer git hashes without running anything
bakar cluster-info  - live sccache-dist scheduler capacity: servers, CPUs, jobs in flight
bakar sched-triage  - post-hoc cluster-utilisation report from the scheduler journal and client error log
```

Related: [ps.md](ps.md), [status.md](status.md), [wait.md](wait.md), [agents.md](agents.md), [log.md](log.md), [monitor.md](monitor.md), [triage.md](triage.md), [report.md](report.md), [insights.md](insights.md), [layers.md](layers.md), [cluster-info.md](cluster-info.md), [sched-triage.md](sched-triage.md)

### Reproducibility

```text
bakar lock       - pin every floating layer SHA to an exact commit
bakar diff       - compare old/new manifest or kas config
bakar drift      - compare workspace pinned SHAs against actual checked-out commits
bakar changelog  - generate release notes between two pinned workspace states
bakar dump       - flatten kas YAML + overlay into a single resolved file
bakar prefetch   - populate DL_DIR for offline builds
bakar mirror     - seed a premirror git2_*.tar.gz tarball from a git URL (host-side)
```

Related: [lock.md](lock.md), [diff.md](diff.md), [drift.md](drift.md), [changelog.md](changelog.md), [dump.md](dump.md), [prefetch.md](prefetch.md), [mirror.md](mirror.md)

### Shell and scripting

```text
bakar shell     - interactive or one-shot kas shell
bakar for-all   - run a command in every source repo (parity with kas for-all-repos)
```

Related: [shell.md](shell.md), [for-all.md](for-all.md)

### Configuration

```text
bakar settings        - CRUD interface for ~/.config/bakar/config.toml
bakar presets list    - list named build presets
bakar presets show    - show a preset's full details
bakar presets add     - add a preset interactively
bakar presets remove  - remove a preset from config.toml
```

Related: [settings.md](settings.md), [presets.md](presets.md), [configuration.md](configuration.md), [workspace.md](workspace.md)

### Advanced / specialized

```text
bakar clean             - remove build/ to force a from-scratch build
bakar clean-cache       - prune stale sstate and ccache entries by age
bakar hashserv          - manage the persistent bitbake-hashserv daemon
bakar prserv            - manage the workspace-scoped bitbake-prserv daemon
bakar feed              - stage, mirror, index, serve and prune the local package feed
bakar bitbake-override  - swap BSP-bundled bitbake for upstream
bakar run               - boot avocado-os QEMU image (meta-avocado only)
bakar stress-parse      - stress-test bitbake parser fork race
```

Related: [clean.md](clean.md), [clean-cache.md](clean-cache.md), [hashserv.md](hashserv.md), [prserv.md](prserv.md), [feed.md](feed.md), [bitbake-override.md](bitbake-override.md), [run.md](run.md), [stress-parse.md](stress-parse.md)

---

## Capability notes

Delivery records for capabilities that span several commands or none. Each is an
archived note rather than a reference page - the gloss below carries the
substance, the page carries the provenance. Where a capability does have a
command page, the gloss links it.

| Capability | Note | What it covers |
|------------|------|----------------|
| Local package feed | [capabilities/local-package-feed.md](capabilities/local-package-feed.md) | Staging, pooled rendering, index derivation, snapshot minting and static serving, and mirroring a published feed - see [feed.md](feed.md) for the commands |
| Feed consolidation | [capabilities/feed-consolidation.md](capabilities/feed-consolidation.md) | Merging scattered RPM deploy trees into one canonical feed under a verification gate (no CLI surface today) |
| Feed retention | [capabilities/feed-retention.md](capabilities/feed-retention.md) | Bounding pool and snapshot growth for a feed written on every build |
| ccache under sccache-dist | [capabilities/ccache-sccache-dist-default.md](capabilities/ccache-sccache-dist-default.md) | Why `[build] ccache` defaults to the resolved `sccache_dist` value instead of `false` when no tier sets it |
| Feed mirror | [capabilities/feed-mirror.md](capabilities/feed-mirror.md) | Populating the local feed from a remote published feed, with integrity checks, write confinement, resume and a disk-space preflight - see [feed.md](feed.md) |
| Build failure visibility | [capabilities/build-failure-visibility.md](capabilities/build-failure-visibility.md) | Indented detail lines of a multi-line error reach the live console - see [build.md](build.md) |
| Build graph capture bounds | [capabilities/build-graph-capture-bounds.md](capabilities/build-graph-capture-bounds.md) | Post-build dependency-graph capture with a bounded wait, an opt-out, provenance that rejects stale captures, and parse failures kept distinct from empty graphs - see [graph.md](graph.md) |
| Build listing | [capabilities/build-listing.md](capabilities/build-listing.md) | The read-only `bakar ps` command that lists every live build on the host - see [ps.md](ps.md) |
| Buildstats discovery | [capabilities/buildstats-discovery.md](capabilities/buildstats-discovery.md) | Locating the buildstats tree for a run and reporting when it is absent, partial or from a different build - see [insights.md](insights.md) |
| Cache mount readiness | [capabilities/cache-mount-readiness.md](capabilities/cache-mount-readiness.md) | Bounded readiness probe for cache directories, the cache-mounts doctor check and the pre-launch gate before bitbake - see [doctor.md](doctor.md) |
| Cluster preflight | [capabilities/cluster-preflight.md](capabilities/cluster-preflight.md) | Shared-cache mount check that accepts softerr, warns on plain soft, and classifies automounts by the top of the mount stack - see [doctor.md](doctor.md) |
| Context object arity | [capabilities/context-object-arity.md](capabilities/context-object-arity.md) | Which functions must take a context object, and the guarantee that they receive the values the invoking command was given |
| CPU floor bound | [capabilities/cpu-floor-bound.md](capabilities/cpu-floor-bound.md) | Per-task CPU seconds, the CPU floor and the concurrency floor against the actual build duration - see [insights.md](insights.md) |
| Daemon state filesystem guard | [capabilities/daemon-state-filesystem-guard.md](capabilities/daemon-state-filesystem-guard.md) | Refusing to place hashserv and prserv SQLite state on a network or undetermined filesystem - see [hashserv.md](hashserv.md) and [prserv.md](prserv.md) |
| Disk free check | [capabilities/disk-free-check.md](capabilities/disk-free-check.md) | The failure message names the threshold the check compared against - see [doctor.md](doctor.md) |
| Doctor check crash isolation | [capabilities/doctor-check-crash-isolation.md](capabilities/doctor-check-crash-isolation.md) | A check past its deadline is reported at its ceiling severity instead of hanging the run - see [doctor.md](doctor.md) |
| KAS worktree resolution | [capabilities/kas-worktree-resolution.md](capabilities/kas-worktree-resolution.md) | How a meta-avocado kas file in a linked worktree finds its workspace, and how the build directory is named - see [build.md](build.md) |
| Module boundary contract | [capabilities/module-boundary-contract.md](capabilities/module-boundary-contract.md) | Which module owns which symbol after the split, and the re-export policy for origin modules |
| Mount stack resolution | [capabilities/mount-stack-resolution.md](capabilities/mount-stack-resolution.md) | How bakar decides which mount covers a path and whether it is NFS, local or undetermined when mounts are stacked |
| Native rebuild attribution | [capabilities/native-rebuild-attribution.md](capabilities/native-rebuild-attribution.md) | The `bakar insights --natives` report with cause grouping and honest non-answers, plus the build-end summary line - see [insights.md](insights.md) |
| Native rebuild forecast | [capabilities/native-rebuild-forecast.md](capabilities/native-rebuild-forecast.md) | The post-build revision-set record and the pre-build doctor check that compares the workspace against it - see [doctor.md](doctor.md) |
| Native signature ledger | [capabilities/native-signature-ledger.md](capabilities/native-signature-ledger.md) | Keeping executed native and cross task signature files beyond the life of the build directory |
| NFS-safe lock clearing | [capabilities/nfs-safe-lock-clearing.md](capabilities/nfs-safe-lock-clearing.md) | A build directory under an automount is never classified as confirmed local |
| Patch target integrity | [capabilities/patch-target-integrity.md](capabilities/patch-target-integrity.md) | A test patch aimed at a relocated symbol fails loudly instead of patching a name no code reads |
| Release workspace routing | [capabilities/release-workspace-routing.md](capabilities/release-workspace-routing.md) | Deriving a meta-avocado build's Yocto release from its pin, mapping it to a workspace, and refusing `--on` with no mapping - see [build.md](build.md) |
| Remote build dispatch | [capabilities/remote-build-dispatch.md](capabilities/remote-build-dispatch.md) | Dispatch with `--on` never deletes on the remote; meta-avocado ships the layer as a git worktree ref - see [build.md](build.md) |
| Repo reuse policy | [capabilities/repo-reuse-policy.md](capabilities/repo-reuse-policy.md) | Under `BAKAR_REPO_POLICY=reuse`, pinned repositories are classified frozen, missing or side so a shared workspace never moves an existing clone - see [build.md](build.md) |
| Setscene join | [capabilities/setscene-join.md](capabilities/setscene-join.md) | Restored tasks joined to the nodes they stand in for, so a seeded build's critical path is computable - see [insights.md](insights.md) |
| Stop host-wide fallback | [capabilities/stop-host-wide-fallback.md](capabilities/stop-host-wide-fallback.md) | When no workspace resolves, `bakar stop` discovers live host-mode builds host-wide and gates any signal behind confirmation - see [stop.md](stop.md) |
| Stop workspace selection | [capabilities/stop-workspace-selection.md](capabilities/stop-workspace-selection.md) | Narrows the no-prompt single-build stop to workspace-scoped discovery and honours the run selector before resolving a workspace - see [stop.md](stop.md) |
| Task churn columns | [capabilities/task-churn-columns.md](capabilities/task-churn-columns.md) | `minflt`, `majflt`, `syscalls` and `GB_wr` aggregated per task type - see [insights.md](insights.md) |
| Task-level critical path | [capabilities/task-level-critical-path.md](capabilities/task-level-critical-path.md) | The duration-weighted longest chain over the task graph, each node weighted by its own elapsed time - see [insights.md](insights.md) |
| Global build mode flags | [capabilities/global-build-mode-flags.md](capabilities/global-build-mode-flags.md) | the global build-mode flags reach every command that launches kas, through one resolution path. |
| Run directory discovery | [capabilities/run-directory-discovery.md](capabilities/run-directory-discovery.md) | triage, report and insights search the same run directories and resolve the same run id to the same directory. |
| Build dir clean reporting | [capabilities/build-dir-clean-reporting.md](capabilities/build-dir-clean-reporting.md) | removing a build directory reports any path it could not remove and fails the command. |
| Post build step records | [capabilities/post-build-step-records.md](capabilities/post-build-step-records.md) | post-build steps and post-build persistence failures are recorded in the run's event log without changing the build's exit status. |
| Boolean env overrides | [capabilities/boolean-env-overrides.md](capabilities/boolean-env-overrides.md) | every boolean `BAKAR_*` override accepts the same true and false spellings. |
| Remote build dispatch | [capabilities/remote-build-dispatch-3d6051.md](capabilities/remote-build-dispatch-3d6051.md) | the run-id lookup distinguishes a failed lookup from a missing run directory and finds qcom runs; every dispatch network operation is bounded; an interrupted dispatch reports the remote build's real fate. |
| Nfs safe lock clearing | [capabilities/nfs-safe-lock-clearing-24fe64.md](capabilities/nfs-safe-lock-clearing-24fe64.md) | the ownership marker is created without an empty window, and an existing marker that cannot be read is refused rather than overwritten. |
| Persistent hashserv | [capabilities/persistent-hashserv.md](capabilities/persistent-hashserv.md) | the doctor check and the status command probe and name the address the daemon is bound to. |
| Concern single ownership | [capabilities/concern-single-ownership.md](capabilities/concern-single-ownership.md) | per-workspace daemon state reading and lifecycle helpers have one implementation shared by hashserv and prserv. |
| Report command | [capabilities/report-command.md](capabilities/report-command.md) | a failed run reports its duration and a qcom run reports success. |
| Task timing baselines | [capabilities/task-timing-baselines.md](capabilities/task-timing-baselines.md) | an interrupted update never leaves a truncated store. |
| Config schema validation | [capabilities/config-schema-validation.md](capabilities/config-schema-validation.md) | a vendor entry whose family has no manifest model is refused at dispatch with exit 2. |
| User config file | [capabilities/user-config-file.md](capabilities/user-config-file.md) | unknown keys are tolerated with a warning naming them, and a known section given the wrong shape is rejected. |
| Exception handling correctness | [capabilities/exception-handling-correctness.md](capabilities/exception-handling-correctness.md) | a failed repository sync reports the failing command and its status instead of a traceback. |
| Cli payload stream contract | [capabilities/cli-payload-stream-contract.md](capabilities/cli-payload-stream-contract.md) | post-mortem commands keep human-facing text off the payload stream. |
| Graceful stop | [capabilities/graceful-stop.md](capabilities/graceful-stop.md) | a container-runtime stop failure is reported with the runtime's own error. |
| Streaming event reader | [capabilities/streaming-event-reader.md](capabilities/streaming-event-reader.md) | polling progress views read only the bytes appended since their previous poll. |
| Architecture governance | [capabilities/architecture-governance.md](capabilities/architecture-governance.md) | the core group sits in the layer order, and group membership and forbidden import edges are enforced by the test suite. |
| Command documentation coverage | [capabilities/command-documentation-coverage.md](capabilities/command-documentation-coverage.md) | every user-settable setting and override is documented, and documented examples and help text match current formats. |
| Patch target integrity | [capabilities/patch-target-integrity-ebd339.md](capabilities/patch-target-integrity-ebd339.md) | an extracted module never imports its origin to keep a test patch target alive. |
| Module boundary contract | [capabilities/module-boundary-contract-7dec82.md](capabilities/module-boundary-contract-7dec82.md) | the stop module keeps only the lock gate and stop orchestration, the build UI state is independent of its renderer, and the global CLI options live in one object. |
