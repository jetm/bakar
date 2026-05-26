# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-05-26

### Added

- Added support for **bitbake-setup workspaces** as a new BSP family (`bbsetup`). `bspctl` now auto-detects a bitbake-setup workspace from the current
directory, translates its `config-upstream.json` and `sources-fixed-revisions.json` into a `kas-bbsetup.yml`, and routes `gen-kas`, `build`, `doctor`, and
`triage` subcommands accordingly.
- `bspctl gen-kas` regenerates `kas-bbsetup.yml` from a bitbake-setup workspace's resolved configuration, pinning each layer repository to its fixed-revision
SHA.
- `bspctl build` now runs the kas pipeline on bitbake-setup workspaces using the generic tuning overlay without requiring a manifest file or YAML argument.
- `bspctl doctor` now runs dedicated pre-flight checks for bitbake-setup workspaces, verifying that the workspace is initialized and that sources are present.
- `bspctl sync` on a bitbake-setup workspace now fails fast with guidance to use `bitbake-setup init` instead of silently attempting an unsupported sync.
- Generated and committed kas YAML files now declare configuration format version 21 (up from 3), compatible with kas 4.x and newer.

### Fixed

- Fixed a build failure that could occur when kas attempted to verify a pinned commit's reachability against a branch that had moved forward; commit-pinned
repos in the `bbsetup` kas translation now emit only the SHA, omitting the branch anchor.

## [0.3.0] - 2026-05-25

### Added

- Persistent user configuration via `~/.config/bspctl/config.toml` — set default machine, distro, image, manifest, repo URL, and container image without exporting environment variables on every shell session. An absent file falls back to built-in defaults and is never auto-created.
- `examples/config.toml` reference file with every available key commented out and annotated; copying it to `~/.config/bspctl/config.toml` is inert until a key is uncommented.
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
- `bspctl doctor` now reports the effective `NPROC` value at pre-flight time via the new `nproc` INFO check.

## [0.2.0]

### Fixed
- Overlay YAMLs were missing from the published wheel: `overlays/` at repo root is not picked up by `uv_build`. Moved to `src/bspctl/overlays/` so the files are included as package data. Every prior release was broken - `bspctl build` raised `FileNotFoundError` on the first run.
- `_overlay_dir()` walked `__file__` three levels up to the repo root, producing a non-existent path under `site-packages/`. Replaced with `importlib.resources.files("bspctl") / "overlays"`.

## [0.1.0] - 2026-05-22

### Added
- `bspctl build --host` and `bspctl shell --host` flags bypass `kas-container` and run plain `kas`/`kas shell` directly on the host - no Docker required.
- Auto-detection: when `KAS_CONTAINER_IMAGE` is absent from the environment, host mode activates automatically. Set the variable to opt into container builds.
- Example kas YAML (`examples/kas-qemux86-64-wrynose.yml`) for a local, network-free wrynose (Yocto master) minimal build on qemux86-64 using repos from `~/repos/personal/yocto/`.

### Changed
- Releases are now driven by `scripts/release.sh`, which enforces an atomic bump+push (preconditions and validation gates run first, then `bump-my-version` and `git push --follow-tags` execute back-to-back with no opportunity to interleave commits).

## [0.0.3] - 2026-05-22

### Added
- First release published to PyPI. Install with `uv tool install bspctl` or `pip install bspctl`.
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
- Pre-flight `bspctl doctor` checks with BLOCK/WARN/INFO severity.
- Structured per-run observability under `<bsp_root>/build/runs/<ts>/` (events.jsonl, console.log, kas.log, env.txt, time.log, du.tsv).
- `bspctl triage` post-mortem with keyed failure-pattern suggestions.
- Vendor config layer at `~/.config/bspctl/vendors.toml` for custom board families.

[Unreleased]: https://github.com/jetm/bspctl/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jetm/bspctl/compare/v0.0.3...v0.1.0
[0.0.3]: https://github.com/jetm/bspctl/compare/v0.0.2...v0.0.3
[0.0.2]: https://github.com/jetm/bspctl/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/jetm/bspctl/releases/tag/v0.0.1
