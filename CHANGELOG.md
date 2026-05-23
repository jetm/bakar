# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-05-22

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
