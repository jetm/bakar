# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.11.0] - 2026-06-03

### Added

- Added `bakar bitbake <recipe> [-c <task>] [-k]` command to build individual recipes with task targeting, run logging, and live knotty progress display (the same progress bar as `bakar build`). The `devshell` task opens an interactive terminal session and `listtasks` pretty-prints the available task names.
- Added `bakar clean-recipe <recipe>` alias for running `bitbake -c cleansstate` on a single recipe.
- Added `bakar graph <recipe>` command to analyze a recipe's BitBake dependency graph. Reports package count, direct dependencies, transitive dependency (blast radius), longest build chain, cycle detection, and most depended-on recipes. Supports `--format {text,dot,json}`, `--depth N`, and an optional buildhistory runtime fan-in section when buildhistory data is present.
- Added `bakar mirror <git-url>` command to seed an offline `PREMIRROR` from a git URL. Clones bare-and-mirrored, then produces a `git2_<netloc><path>.tar.gz` tarball with `--owner oe:0 --group oe:0` and an mtime fixed to the committer date, making the output byte-stable across re-runs of the same revision. Output goes to `--output-dir`, the configured `DL_DIR` when inside a workspace, or the current directory.

### Fixed

- Fixed `bakar graph` always reporting zero packages, no longest chain, and no critical recipes. Container startup log lines emitted by kas were prepended to the captured `task-depends.dot` content, causing the DOT parser to fail silently and return an empty graph; the log preamble is now stripped before parsing.
- Fixed `bakar graph` exiting with success and printing empty graph data when artifact retrieval (`cat ${TOPDIR}/task-depends.dot`) failed due to a wrong TOPDIR, missing artifact, or permission error. The command now checks retrieval exit codes and rejects an empty or malformed TOPDIR, exiting non-zero with the captured error message.
- Fixed `bakar graph --format dot` running the full analysis pipeline (including buildhistory reads and pn-buildlist retrieval) before discarding all results to echo only the raw dot text; the dot path now returns immediately after the dot content is captured.

## [0.10.0] - 2026-06-03

### Added

- Added `bakar show` command that prints the fully-resolved build picture (Config, Overlays, Layers, Sources, and the exact `kas-container` command that would run) without invoking a container. Works on un-built workspaces; supports `--json` and `--format md` for Markdown output.
- Added `bakar getvar` command that resolves BitBake variable values inside `kas-container`. Supports global lookup, recipe-scoped lookup (`--recipe`/`-r`), unexpanded values (`--unexpanded`/`-u`), and assignment history showing the full include-chain of file:line assignments (`--history`).
- Added `bakar inspect` command that produces a per-recipe report combining Identity, Paths, Sources, Inherits, Packages, and Dependencies sections from two container calls (`bitbake-layers show-recipes` and `bitbake -e`).
- Added `bakar diffsigs` command that diagnoses why a recipe task missed sstate cache by running `bitbake -S printdiff` followed by `bitbake-diffsigs`, then rendering structured output: root cause first, an indented rebuild chain with depth count and cross-recipe boundary annotation, and a compact added/removed dependency diff instead of full lists. Use `--raw` to bypass parsed output and receive the full unprocessed capture.
- Added `bakar layers inspect` sub-command that reports per-layer priority, series compatibility, version, and provides information by combining local `layer.conf` parsing with container-backed `bitbake-layers show-layers`. Accepts `--json`.
- Added `bakar layers status` sub-command that fetches the effective `MACHINE`, `DISTRO`, `DISTRO_CODENAME`, thread counts, mirror URLs, and hashserv URL for the current project in a single container call. Accepts `--json`.

### Changed

- `bakar layers` is now a sub-app; running it bare continues to print the existing git short-hash and branch listing unchanged.
- All container-dispatching commands (`show`, `getvar`, `inspect`, `diffsigs`, and existing commands such as `doctor`, `shell`, `diff`) now accept a kas YAML path via the `-f`/`--manifest` flag in addition to a positional argument, routing it correctly through the kas dispatch path instead of treating it as an XML manifest.
- `bakar inspect` now extracts `WORKDIR`, `S`, `B`, `D`, and `T` paths from the `bitbake -e` environment dump instead of a separate `bitbake-getvar` call, reducing the number of container invocations from three to two per run.
- `bakar diffsigs` structured output now includes chain depth ("N levels deep"), a cross-recipe boundary note when the root cause originates in a different recipe, a cause count when multiple dependency changes are found, and a count summary on the dependency diff ("N added, N removed").

## [0.9.0] - 2026-06-03

### Added

- `bakar report` now displays an sstate cache summary (hit/miss counts and percentages) when `--show-sstate` is passed or `layers.show_sstate_summary` is set to `true` via `bakar settings set`
- `bakar report` now displays a buildhistory section (image size, top packages by size, package count, dirty layers) when a `buildhistory` directory is present in the workspace — no flag required, the data's presence is the gate
- `bakar build` now prints the sstate cache summary after a successful build when `layers.show_sstate_summary` is enabled, mirroring the existing `show_hashes` / `--show-layers` behavior
- Added `layers.show_sstate_summary` setting, persistable via `bakar settings set layers.show_sstate_summary true`
- `bakar report` now finds test runs under `build-*/build/runs` (e.g. meta-avocado and custom build-dir BYO workspaces), preventing silent fallback to a stale generic run

### Fixed

- `bakar build --show-layers` (and `show_hashes` config) now correctly prints the layer hash table for bitbake-setup (bbsetup) workspaces; it previously printed nothing
- `bakar build --show-layers` on a fresh BYO build now shows the correct layer table instead of printing nothing (or showing a stale previous build's state), because the table is now printed after `kas` writes `bblayers.conf`
- `bakar build --show-layers --dry-run` on bbsetup workspaces now prints the layer table instead of silently skipping it due to an early exit
- Layer hash table entries containing git short hashes that match scientific-notation patterns (e.g. `25850e97`) are no longer incorrectly highlighted in cyan by Rich
- Enabling the `hashequiv` overlay no longer arms a connection to the public Yocto hash-equivalence server by default; builds in environments without access to that server no longer hang at startup. Set `BB_HASHSERVE_UPSTREAM` in the environment to opt in
- Layer hashes are now correctly resolved for bitbake-setup workspaces (the `layers/<repo>` layout); previously the hash table came back empty for bbsetup builds
- Malformed or unreadable `config-upstream.json` no longer causes the bbsetup workspace detector to propagate an error; it now correctly returns "not a bbsetup workspace"
- A malformed or incomplete `error-report.json` (missing keys or wrong-typed fields) now correctly falls through to the live-parse path in `bakar triage` instead of propagating an exception

## [0.8.0] - 2026-06-03

### Added

- Added `--sstate-mirror` flag to `bakar build` that configures an HTTP sstate/downloads mirror URL and automatically enables the shared-cache overlay (`SSTATE_MIRRORS`, `BB_HASHSERVE_UPSTREAM`) without requiring manual kas YAML edits.
- Added `sstate_mirror_url` field to the `[build]` section of `config.toml`, equivalent to passing `--sstate-mirror` on every build invocation.
- Added a `build_revision` field to report output: a short stable identity hash derived from the current layer checkout SHAs, visible in both human-readable and `--json` report output.
- Added warning and error tallies printed at the end of every build (e.g. "3 warnings, 1 error"), so build noisiness is visible without grepping `kas.log`.
- Added extended triage suggestions for compiler OOM kills (`cc1plus` killed by OOM killer), GitHub API rate limits (HTTP 429), DNS/network failures (with PREMIRROR recommendation), and mirror connection refusals.
- `bakar triage` now discovers run directories across all BSP families and BYO/generic workspaces, not only `nxp/` and `ti/` subdirectories.

### Changed

- `bakar triage` now reads a pre-computed `error-report.json` artifact written at build-failure time instead of re-parsing the full `kas.log` on every invocation, making triage significantly faster for completed failed runs. Run directories produced before this change continue to work via the existing log-parse fallback.
- `bakar clean-cache` sstate pruning now uses a two-phase move-then-delete strategy: stale files are staged inside a `.bakar-gc-<pid>` directory within the sstate root before deletion. This prevents half-deleted state under concurrent builds and leaves a recoverable staging directory if the process is interrupted.
- Timestamped phase headers (step start, success, and failure) are now written to `console.log`, making it possible to identify build step boundaries without cross-referencing `events.jsonl`.

## [0.7.0] - 2026-06-02

### Added

- Added `[build] ccache_shared` and `[build] ccache_dir` settings to share one ccache across all workspaces (cross-BSP hits, less disk) instead of the per-workspace default.
- `clean-cache` now also prunes the ccache via `ccache --evict-older-than`, with `--ccache-dir` and `--sstate`/`--ccache` scoping flags.

### Changed

- Renamed the `clean-sstate` command to `clean-cache`; it now prunes both the sstate cache and the ccache by age. Update any scripts that call `bakar clean-sstate`.

## [0.6.0] - 2026-06-01

### Added

- Added `bakar init` command for interactive workspace creation, guiding new users through BSP family selection and workspace configuration with family-specific prompts for NXP, TI, bbsetup, and generic targets.
- Added non-interactive flags (`--family`, `--workspace`, `--manifest`, `--machine`, `--distro`, `--image`, `--kas-yaml`, `--no-sync`) to `bakar init` for scripted or CI-driven workspace creation without a TTY.
- Added workspace-scoped defaults via `.bakar.toml`: settings written by `bakar init` (machine, distro, image, manifest) are now persisted per-workspace and applied automatically without requiring flags at every invocation.
- Added a new configuration resolution tier so the full precedence chain is: CLI flag > `BAKAR_*` env var > workspace `.bakar.toml` > `~/.config/bakar/config.toml` > built-in default.
- Added documentation for `bakar init`, workspace `.bakar.toml` defaults schema, and the updated five-tier precedence table in the configuration reference.

### Fixed

- Fixed `bakar init` cancelling a prompt (Ctrl-C or Escape) causing an uncaught `TypeError` and leaving a partial workspace directory on disk; prompts now abort cleanly.
- Fixed machine values written by `bakar init` into `[defaults.generic]` being silently ignored when resolving configuration for generic workspaces.
- Fixed a Python exception-handling bug where catching multiple exception types without parentheses silently caught only `ValueError` instead of both `ValueError` and `OSError`.
- Fixed `bakar init` not appearing in the CLI help output (`--help`) or quick-navigation table because its module was never imported at startup.

## [0.5.0] - 2026-05-29

### Breaking

- Renamed the tool from `bspctl` to `bakar`. The console script is now
  `bakar` (the old `bspctl` command is removed with no alias) and the
  importable package is now `bakar`.
- Renamed all `BSPCTL_*` environment variables to `BAKAR_*`. The old
  names are no longer read - there is no fallback.
- Renamed the workspace marker `.bspctl.toml` to `.bakar.toml` and the
  state directory `.bspctl/` to `.bakar/`. Existing workspaces must
  rename these manually before running `bakar`.

### Added
- Added `bakar layers` command to inspect which git revisions back each synced layer without running a full build.
- Added `bakar for-all <cmd>` command to run a shell command across every cloned source repository, exporting `BAKAR_REPO_NAME`, `BAKAR_REPO_PATH`, and `BAKAR_REPO_COMMIT` per invocation; exits non-zero if any repo fails while still visiting all repos.
- Added `bakar settings` subcommand (`list`, `get`, `set`, `unset`) for managing `~/.config/bakar/config.toml` without hand-editing; unknown keys and type mismatches are rejected with a non-zero exit.
- Added `bakar diff <old> <new>` to compare layer SHAs between two NXP/TI manifest XMLs or delegate to `kas diff` for BYO/bbsetup configs.
- Added `bakar prefetch` to run `bitbake --runall=fetch` through the existing kas environment, enabling offline source population without a full build.
- Added `bakar dump` to print or stream the fully resolved kas YAML (after include expansion and overlay merging) to stdout or a file.
- Added `bakar lock` to pin floating layer SHAs: wraps `repo manifest -r` for NXP workspaces and `kas lock` for BYO/bbsetup/TI.
- Added `bakar report [run-id]` to display a post-build summary (image size, duration, layer state) in human-readable or `--json` form, resolving the latest run when no ID is given.
- Added `bakar clean-sstate` for age-based sstate-cache pruning (default 30 days, dry-run by default; `--yes` deletes). Automatically detects `noatime` mounts and falls back to mtime-based pruning with a warning.
- Added `bakar hashserv` subcommand (`start`, `stop`, `status`) for explicit lifecycle control of a persistent per-workspace `bitbake-hashserv` daemon; `bakar build` auto-starts the daemon when `build.hashserv = true` is set in config.
- Added seven new `[build]` config keys accessible via `bakar settings`: `dl_dir`, `sstate_dir`, `sstate_mirrors`, `scheduler`, `pressure_max_cpu`, `pressure_max_io`, and `pressure_max_memory`. Integer keys are stored as integers (not strings) in config.toml.
- Added `--psi-calibrate` flag to `bakar doctor` to sample CPU/IO/memory pressure at 0.5 s intervals and recommend `pressure_max_*` thresholds at peak + 20% headroom.
- Added new `bakar doctor` pre-flight checks: PSI kernel support, git global identity (honoring `includeIf` conditionals), kas YAML syntax, workspace filesystem hardlink safety, Docker version (≥ 20.10) and storage driver (`overlay2`), ccache fill level, and persistent hashserv reachability.
- Added a `bakar-tuning-hashequiv.yml` opt-in overlay enabling OEEquivHash with a local `BB_HASHSERVE`; when `build.hashserv = true` is configured, bakar automatically appends this overlay (deduplicating if the user also passes it explicitly).
- Added per-command documentation under `docs/` and a navigation index at `docs/index.md`; README condensed to a quickstart with a commands table.

### Changed
- Raised minimum Python version to 3.14; Python 3.12 and 3.13 are no longer supported.
- Log output from the Rich console now goes to stderr, keeping stdout clean for commands like `gen-kas` that emit machine-readable text.
- `bakar doctor` git identity check now runs from the workspace directory so `includeIf "gitdir:..."` conditionals are honoured; a false BLOCK no longer fires for developers using per-project git identities.
- `BB_DISKMON_DIRS` in all tuning overlays updated from deprecated `ABORT` keyword to `HALT` (required for Yocto scarthgap and later).
- All tuning overlays now set `BB_HASHSERVE_UPSTREAM = ""` to prevent silent build hangs when the container cannot reach the public Yocto hash equivalence server.
- `bakar clean --all` now gracefully stops the persistent hashserv daemon before wiping the workspace, preventing SQLite WAL corruption.
- Settings config file is now written atomically (temp file + replace) to prevent truncation on crash.
- `for-all` now catches `OSError` from subprocess invocation so a removed or inaccessible repo directory counts as a failure and the loop continues to remaining repos.

### Fixed
- Fixed `bakar report` crashing with `UnboundLocalError` on NXP/TI workspaces due to the `family` variable being read before assignment.
- Fixed `bakar diff` silently treating BYO kas configs as empty NXP manifests; dispatch now keys on file type (`.xml` → structural diff, everything else → `kas diff`).
- Fixed `bakar doctor` kas-yaml-syntax check incorrectly returning FAIL for a valid YAML when the remote branch was rebased; the check now returns SKIP so the subsequent sync step can repair git state.
- Fixed `bakar doctor` kas-yaml-syntax error message showing an irrelevant INFO log line instead of the actual ERROR message.
- Fixed exception handling in the disk-usage sampler that was silently swallowing programmer errors; narrowed to `(SubprocessError, OSError, ValueError)` and logs only the first failure instead of flooding the run log.
- Fixed `triage` not finding run directories for meta-avocado workspaces (builds land in `build-<stem>/build/runs/`).

## [0.4.0] - 2026-05-26

### Added

- Added support for **bitbake-setup workspaces** as a new BSP family (`bbsetup`). `bakar` now auto-detects a bitbake-setup workspace from the current
directory, translates its `config-upstream.json` and `sources-fixed-revisions.json` into a `kas-bbsetup.yml`, and routes `gen-kas`, `build`, `doctor`, and
`triage` subcommands accordingly.
- `bakar gen-kas` regenerates `kas-bbsetup.yml` from a bitbake-setup workspace's resolved configuration, pinning each layer repository to its fixed-revision
SHA.
- `bakar build` now runs the kas pipeline on bitbake-setup workspaces using the generic tuning overlay without requiring a manifest file or YAML argument.
- `bakar doctor` now runs dedicated pre-flight checks for bitbake-setup workspaces, verifying that the workspace is initialized and that sources are present.
- `bakar sync` on a bitbake-setup workspace now fails fast with guidance to use `bitbake-setup init` instead of silently attempting an unsupported sync.
- Generated and committed kas YAML files now declare configuration format version 21 (up from 3), compatible with kas 4.x and newer.

### Fixed

- Fixed a build failure that could occur when kas attempted to verify a pinned commit's reachability against a branch that had moved forward; commit-pinned
repos in the `bbsetup` kas translation now emit only the SHA, omitting the branch anchor.

## [0.3.0] - 2026-05-25

### Added

- Persistent user configuration via `~/.config/bakar/config.toml` — set default machine, distro, image, manifest, repo URL, and container image without exporting environment variables on every shell session. An absent file falls back to built-in defaults and is never auto-created.
- `examples/config.toml` reference file with every available key commented out and annotated; copying it to `~/.config/bakar/config.toml` is inert until a key is uncommented.
- `--show-layers` flag and `show_hashes` config key to print each layer's git short hash and branch after a build, mirroring what bitbake logs. The layers table now also includes the bitbake version.
- Layer hash collection now supports generic kas YAML builds that use `${TOPDIR}`-relative layer paths (e.g. `${TOPDIR}/../layers/<repo>`), in addition to the existing NXP/TI `/sources/`-based layout.
- `doctor` config key to suppress the pre-flight doctor check without passing `--skip-doctor` on every invocation.

### Changed

- Configuration resolution now follows a four-tier precedence chain: CLI flag → environment variable → `config.toml` → built-in default. Setting `container_image` in `config.toml` activates container mode (same behaviour as `KAS_CONTAINER_IMAGE`) and prints a notice so the switch is not silent.
- The container-bitbake doctor check now bind-mounts the workspace bitbake directory into the container when available, producing a real version string instead of an opaque "inspection failed" / SKIP result. When `which bitbake` returns a not-found message the check now reports "not in container PATH (workspace-sourced)" rather than a generic failure.
- A config file parse error now exits with code 2 and prints the file path, making misconfigured `config.toml` files immediately identifiable.

## [0.2.1] - 2026-05-23

### Fixed
- `BB_NUMBER_THREADS` and `PARALLEL_MAKE` defaulted to 16 regardless of the host's CPU count. `_build_env()` now sets `NPROC` to `os.cpu_count()` before invoking kas, so the tuning overlay picks up the actual core count. Set `NPROC` explicitly to override.
- `bakar doctor` now reports the effective `NPROC` value at pre-flight time via the new `nproc` INFO check.

## [0.2.0]

### Fixed
- Overlay YAMLs were missing from the published wheel: `overlays/` at repo root is not picked up by `uv_build`. Moved to `src/bakar/overlays/` so the files are included as package data. Every prior release was broken - `bakar build` raised `FileNotFoundError` on the first run.
- `_overlay_dir()` walked `__file__` three levels up to the repo root, producing a non-existent path under `site-packages/`. Replaced with `importlib.resources.files("bakar") / "overlays"`.

## [0.1.0] - 2026-05-22

### Added
- `bakar build --host` and `bakar shell --host` flags bypass `kas-container` and run plain `kas`/`kas shell` directly on the host - no Docker required.
- Auto-detection: when `KAS_CONTAINER_IMAGE` is absent from the environment, host mode activates automatically. Set the variable to opt into container builds.
- Example kas YAML (`examples/kas-qemux86-64-wrynose.yml`) for a local, network-free wrynose (Yocto master) minimal build on qemux86-64 using repos from `~/repos/personal/yocto/`.

### Changed
- Releases are now driven by `scripts/release.sh`, which enforces an atomic bump+push (preconditions and validation gates run first, then `bump-my-version` and `git push --follow-tags` execute back-to-back with no opportunity to interleave commits).

## [0.0.3] - 2026-05-22

### Added
- First release published to PyPI. Install with `uv tool install bakar` or `pip install bakar`.
- Python 3.14 added to the supported version matrix (3.11–3.14).
- GitHub Actions CI workflow: test matrix across Python 3.11–3.14, ruff lint, ty type-check.
- Automated PyPI publishing via OIDC Trusted Publisher on version-tag push, with a GitHub release
  created from the matching CHANGELOG section.
- `RELEASING.md`: step-by-step release guide covering PyPI Trusted Publisher setup and the
  `bump-my-version` workflow.

### Changed
- Documentation leads with kas wrapper identity (four general capabilities); NXP/TI vendor
  manifest support is now presented as a secondary layer built on top.

## [0.0.2] - 2026-05-21

## [0.0.1] - 2026-05-21

### Added
- Initial public release.
- NXP i.MX BSP support via Google `repo` + `var-setup-release.sh` + `kas-container`.
- TI Sitara BSP support via `varigit/oe-layersetup` + `kas-container`.
- Generic BYO kas YAML support for any non-NXP/TI build.
- Pre-flight `bakar doctor` checks with BLOCK/WARN/INFO severity.
- Structured per-run observability under `<bsp_root>/build/runs/<ts>/` (events.jsonl, console.log, kas.log, env.txt, time.log, du.tsv).
- `bakar triage` post-mortem with keyed failure-pattern suggestions.
- Vendor config layer at `~/.config/bakar/vendors.toml` for custom board families.

[Unreleased]: https://github.com/jetm/bakar/compare/v0.11.0...HEAD
[0.11.0]: https://github.com/jetm/bakar/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/jetm/bakar/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/jetm/bakar/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/jetm/bakar/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/jetm/bakar/compare/v0.4.0...v0.7.0
[0.4.0]: https://github.com/jetm/bakar/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jetm/bakar/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/jetm/bakar/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jetm/bakar/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jetm/bakar/compare/v0.0.3...v0.1.0
[0.0.3]: https://github.com/jetm/bakar/compare/v0.0.2...v0.0.3
[0.0.2]: https://github.com/jetm/bakar/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/jetm/bakar/releases/tag/v0.0.1
