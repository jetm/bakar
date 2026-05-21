# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
[0.1.0]: https://github.com/jetm/bspctl/compare/v0.0.2...v0.1.0
[0.0.2]: https://github.com/jetm/bspctl/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/jetm/bspctl/releases/tag/v0.0.1
