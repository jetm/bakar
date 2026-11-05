# PSI autocalibration

PSI autocalibration tunes bitbake's pressure thresholds from the pressure your host measures, using the kernel's pressure stall information. Thresholds are written after the next successful build.

## What your host needs

1. The kernel must expose PSI (hard requirement, probe `psi-kernel`, which reads `/proc/pressure/cpu`). Build the kernel with `CONFIG_PSI=y`, or boot with `psi=1` on the kernel command line.
2. Verify with `bakar doctor`; the `psi_support` check reads the same file and reports PSI unavailable when it is absent.

## Enable

Interactive: run `bakar init` and select the psi feature.

Non-interactive: `bakar init --feature psi --set build.psi_autocalibrate=true`

`build.psi_autocalibrate` is the only key this feature writes, so it is the only key `--set` accepts for it.

## Turn off

Run `bakar init --disable psi`. It unsets `build.psi_autocalibrate` in your user config. It does not change the kernel PSI setting.

## See also

- `bakar doctor`
- [Compiler cache](ccache.md)
