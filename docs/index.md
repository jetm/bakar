# bakar documentation

## Quick navigation

| Command | Doc | One-liner |
|---------|-----|-----------|
| `setup` | [setup.md](setup.md) | Prepare the host once: profile, remediate doctor host checks, persist `[host]` config |
| `init` | [init.md](init.md) | Interactive wizard: scaffold a new workspace and write `.bakar.toml` |
| `build` | [build.md](build.md) | Full pipeline: doctor, sync, gen-kas, kas-container build |
| `sync` | [sync.md](sync.md) | Sync sources without building |
| `gen-kas` | [gen-kas.md](gen-kas.md) | Regenerate kas YAML from manifest |
| `bitbake` | [bitbake.md](bitbake.md) | Run a single recipe or task through bitbake, with run logging |
| `clean-recipe` | [bitbake.md](bitbake.md) | Clean one recipe's sstate (`bitbake -c cleansstate`) |
| `rebuild` | [bitbake.md](bitbake.md) | Rebuild one recipe from scratch (`cleansstate` then build) |
| `shell` | [shell.md](shell.md) | Interactive kas-container shell or one-shot command |
| `run` | [run.md](run.md) | Boot avocado-os image in QEMU (meta-avocado only) |
| `stop` | [stop.md](stop.md) | Gracefully halt a running build (SIGINT, then escalate) |
| `clean` | [clean.md](clean.md) | Remove the build directory |
| `clean-cache` | [clean-cache.md](clean-cache.md) | Prune stale sstate and ccache entries by age |
| `doctor` | [doctor.md](doctor.md) | Run pre-flight checks |
| `triage` | [triage.md](triage.md) | Post-mortem a failed build |
| `report` | [report.md](report.md) | Summarize a completed build run |
| `insights` | [insights.md](insights.md) | Per-recipe/per-task analytics: sstate, timing, pressure, disk |
| `log` | [log.md](log.md) | Tail a run log live |
| `monitor` | [monitor.md](monitor.md) | One-view live watch: cluster load, dist stats, task progress |
| `layers` | [layers.md](layers.md) | Print layer git hashes, branches, priority, and build status |
| `show` | [show.md](show.md) | Print resolved build picture: config, overlays, layers, sources, command |
| `getvar` | [getvar.md](getvar.md) | Resolve a bitbake variable and show where it was set |
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
| `dump` | [dump.md](dump.md) | Inspect the resolved kas YAML |
| `hashserv` | [hashserv.md](hashserv.md) | Manage the persistent bitbake-hashserv daemon |
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
- Only want to sync sources: [sync.md](sync.md)
- Only want to regenerate the kas YAML: [gen-kas.md](gen-kas.md)

**Build failed:**
- Stop a running build cleanly so it stays resumable: [stop.md](stop.md)
- Find what went wrong: [triage.md](triage.md)
- Watch a running build (tail one log): [log.md](log.md)
- Watch a running build (cluster + dist + task progress in one view): [monitor.md](monitor.md)
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
- What a variable resolves to and where it was set: [getvar.md](getvar.md)
- What a recipe pulls in (packages, deps, paths): [inspect.md](inspect.md)
- Per-layer priority, compat, and provided recipes: [layers.md](layers.md) (`layers inspect`)
- Project-level MACHINE, DISTRO, thread/mirror config: [layers.md](layers.md) (`layers status`)
- Why a task missed sstate and rebuilt: [diffsigs.md](diffsigs.md)
- A recipe's dependency graph (blast radius, longest chain, cycles): [graph.md](graph.md)

**Reproducibility and snapshots:**
- Pin current SHAs: [lock.md](lock.md)
- See what changed between manifest versions: [diff.md](diff.md)
- Check how far the workspace has drifted from pinned SHAs: [drift.md](drift.md)
- Generate release notes between two pinned states: [changelog.md](changelog.md)
- Pre-fetch sources for an offline build: [prefetch.md](prefetch.md)
- Seed a premirror tarball from a git URL (host-side): [mirror.md](mirror.md)
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
- Run a build on an idle remote node (mirror the tree, build over ssh): [build.md](build.md) (`--on <host>`)

---

## Command groups

### Build pipeline

```text
bakar doctor    - pre-flight (runs automatically before build)
bakar sync      - fetch/update sources
bakar gen-kas   - translate manifest → kas YAML
bakar build     - all of the above, then kas-container build
```

Related: [build.md](build.md), [sync.md](sync.md), [gen-kas.md](gen-kas.md), [doctor.md](doctor.md)

### Inspection

```text
bakar show              - resolved config, overlays, layers, sources (local, no container)
bakar getvar <VAR>      - variable resolution and provenance via bitbake-getvar / bitbake -e
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
bakar log       - tail a live build log
bakar monitor   - one-view live watch: cluster load, dist stats, task progress
bakar triage    - surface the failing recipe/task from bitbake-events.json (--run/--preset/--release select the run dir)
bakar report    - summarize a completed run (timing, image size, layers)
bakar insights  - per-recipe/per-task analytics: sstate, timing, pressure, disk
bakar layers    - print layer git hashes without running anything
```

Related: [log.md](log.md), [monitor.md](monitor.md), [triage.md](triage.md), [report.md](report.md), [insights.md](insights.md), [layers.md](layers.md)

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
bakar shell     - interactive or one-shot kas-container shell
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
bakar bitbake-override  - swap BSP-bundled bitbake for upstream
bakar run               - boot avocado-os QEMU image (meta-avocado only)
bakar stress-parse      - stress-test bitbake parser fork race
```

Related: [clean.md](clean.md), [clean-cache.md](clean-cache.md), [hashserv.md](hashserv.md), [bitbake-override.md](bitbake-override.md), [run.md](run.md), [stress-parse.md](stress-parse.md)
