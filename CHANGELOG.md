# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/jetm/bspctl/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/jetm/bspctl/releases/tag/v0.0.1
