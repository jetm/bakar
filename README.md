[![CI](https://github.com/jetm/bakar/actions/workflows/ci.yml/badge.svg)](https://github.com/jetm/bakar/actions/workflows/ci.yml)

# bakar

kas-based BSP build orchestrator for Yocto. Wraps `kas-container` with manifest-driven sync, pre-flight checks, structured telemetry, and post-mortem tooling. Works with NXP i.MX (repo XML), TI Sitara (oe-layertool), bitbake-setup workspaces, and any bring-your-own kas YAML.

## Install

```bash
uv tool install git+https://github.com/jetm/bakar.git
```

## Quickstart

```bash
# NXP i.MX manifest-driven build
bakar build -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart

# Bring-your-own kas YAML
bakar build my-project.yml

# Post-mortem a failed build
bakar triage
```

## Documentation

Full command reference, workflow guides, and configuration: **[docs/index.md](docs/index.md)**
