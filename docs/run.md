# bakar run

Boot an avocado-os image in QEMU from the build directory.

Requires a completed build with stone provisioning. Only supported for meta-avocado builds (`avocado-qemux86-64`, `avocado-qemuarm64`).

## Synopsis

```text
bakar run KAS_YAML [OPTIONS]
```

## Arguments

| Argument | Description |
|----------|-------------|
| `KAS_YAML` | kas YAML identifying the target machine. Colon-separated overlays are accepted (e.g. `kas/machine/qemux86-64.yml:kas/target/qemu-provision.yml`); overlays are ignored for machine resolution |

## Options

| Flag | Description |
|------|-------------|
| `--swtpm` / `--no-swtpm` | Run with software TPM via swtpm daemon (default: enabled) |
| `--workspace`, `-w` | Workspace root override |

## Examples

```bash
# Build first (with stone provisioning overlay)
bakar build kas/machine/qemux86-64.yml:kas/target/qemu-provision.yml

# Boot the image
bakar run kas/machine/qemux86-64.yml

# Boot without software TPM
bakar run kas/machine/qemux86-64.yml --no-swtpm

# Boot arm64
bakar build kas/machine/qemuarm64.yml:kas/target/qemu-provision.yml
bakar run kas/machine/qemuarm64.yml
```

## Notes

- `bakar run` fails with exit 2 on non-meta-avocado kas YAMLs.
- Requires `qemu-system-x86_64` (or `qemu-system-aarch64`) and optionally `swtpm` on the host.
- Overlays in the `KAS_YAML` argument are stripped before machine resolution; they can be included for command-line consistency with `bakar build`.

## See also

- [build.md](build.md) - build the image before running
- [shell.md](shell.md) - interactive shell inside the kas environment
