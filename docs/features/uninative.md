# Uninative

Uninative builds the `-native` tools against the host-provided uninative tarball, so sstate is portable across host distributions. It is opt-in because enabling it changes sstate signatures. The `uninative` key is documented in [config-reference.md](../config-reference.md).

## What your host needs

1. Build in host mode on an Arch-family host. The feature takes effect only there; elsewhere it adds nothing.
2. Install the `yocto-uninative-tarball` package so that `/usr/share/yocto-uninative/uninative.inc` exists. This is a hard requirement. For example: `sudo pacman -S yocto-uninative-tarball`, or build it from your AUR helper.
3. Run `bakar doctor` and read the `uninative-fragment` check. It is a hard requirement and blocks when the fragment is absent or malformed. For a malformed fragment, reinstall or rebuild `yocto-uninative-tarball`, or turn the feature off.
4. Expect new sstate signatures. Enabling it changes `NATIVELSBSTRING` to `universal`, so sstate built without uninative is not reused, and the reverse.

## Enable

Interactive: run `bakar init` and select the uninative feature.

Non-interactive: `bakar init --feature uninative --set build.uninative=true`

The feature writes one key, and `--set` accepts only this one for it: `build.uninative` (default `true`).

## Turn off

Run `bakar init --disable uninative`. It unsets `build.uninative`, which then falls back to its default of off. It does not uninstall the `yocto-uninative-tarball` package, and sstate signatures change back.

## See also

- [config-reference.md](../config-reference.md)
- [doctor.md](../doctor.md)
- `bakar doctor`
