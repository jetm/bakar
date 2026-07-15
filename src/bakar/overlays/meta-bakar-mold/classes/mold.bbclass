# mold.bbclass - make mold the linker for selected target-userspace recipes.
#
# Inherited globally via `INHERIT += "mold"` (the bakar mold tuning overlay does
# this). Modeled on meta-bakar-sccache's allow-list anon-python gate: the flag is
# carried in an indirection variable (MOLD_LDFLAGS) appended to TARGET_LDFLAGS,
# and the per-recipe gate blanks that variable for recipes that must NOT link
# with mold. Blanking - not re-pinning bfd - keeps a stray -fuse-ld=mold out of
# excluded recipes' logs and avoids depending on gcc's last-`-fuse-ld`-wins (D4).
#
# Scope is target-only (D5): the flag is appended to TARGET_LDFLAGS, never
# BUILD_LDFLAGS. Native links run the build-host/buildtools gcc whose version is
# unaudited; a <12.1 gcc hard-fails on -fuse-ld=mold, so the native compiler is
# left untouched.
#
# Linker discovery uses a -B<wrapper-dir> under RECIPE_SYSROOT_NATIVE (D2), not a
# target-sysroot drop: a -native recipe cannot own a ${TARGET_SYS} tooldir
# without breaking native sstate sharing across arches. -B prefixes are searched
# ahead of the standard tooldir, sidestepping the collect2 --sysroot lookup
# failure (mold #564). But libtool strips -B from its generated link line, so -B
# alone leaves autotools recipes unable to find ld.mold; the same wrapper dir is
# also exported via COMPILER_PATH (an env var libtool cannot strip) so discovery
# survives libtool. Both point at the same dir; -B wins for cmake/meson/make,
# COMPILER_PATH covers libtool.
#
# The -B dir holds exactly ONE timing wrapper, chosen by mode (revised D6): the
# mold arm (list/global) stages ld.mold, the baseline arm stages ld.bfd. Staging
# BOTH was wrong - in mold mode a toolchain recipe that permanently forces
# -fuse-ld=bfd (libgcc, gcc-runtime) would hit our ld.bfd, and a bare-name PATH
# scan there resolves the host x86 ld.bfd in tmp/hosttools first, failing the
# cross link. The wrapper's REAL_LINKER is baked to the absolute arch-correct
# linker at stage time (cross ld.bfd for the baseline arm, native ld.mold for the
# mold arm) so it never depends on PATH order.
#
# MOLD_MODE selects the policy (default "list"):
#   list     - allow-list: blank MOLD_LDFLAGS unless PN in MOLD_INCLUDED_PN.
#   global   - deny-list:  blank MOLD_LDFLAGS when PN in MOLD_EXCLUDED_PN.
#   baseline - measurement: set MOLD_LDFLAGS to the bfd arm over MOLD_INCLUDED_PN.

# Default policy: allow-list. Flipping to "global" is a one-variable change.
MOLD_MODE ??= "list"

# Per-recipe opt-out escape hatch (mirrors sccache's SCCACHE_DISABLE).
MOLD_DISABLE ??= ""

# Allow-list seeded with the Phase-1b heavy target plus the Phase-2 browser
# heavies, so they are proven under mold before the global flip.
MOLD_INCLUDED_PN ?= "llvm librsvg chromium-ozone-wayland chromium-x11 qtwebengine wpewebkit"

# Deny-list carries glibc so the kernel/glibc NEVER-mold contract (req 4) does
# NOT rely on gcc flag ordering (A7); kernel/firmware self-exclude by clearing
# LDFLAGS or forcing ld.bfd, so they need no entry here.
MOLD_EXCLUDED_PN ?= "glibc"

# Neutral -B wrapper directory, in the native sysroot the driver runs from. The
# target --sysroot only scopes libs/startfiles, never the linker-program lookup.
MOLD_WRAPPER_DIR ?= "${RECIPE_SYSROOT_NATIVE}/mold-ld-wrappers"

# Locate this class's own layer so the timing wrapper file can be staged from it.
MOLD_CLASSDIR := "${@os.path.dirname(bb.utils.which(d.getVar('BBPATH'), 'classes/mold.bbclass') or '')}"
MOLD_WRAPPER_SRC ?= "${MOLD_CLASSDIR}/../files/ld-timing-wrapper.sh"

# The linker flag, carried indirectly so the gate can blank it per-recipe.
# --build-id=sha256 (never uuid) keeps mold output deterministic so hashequiv /
# sstate stay valid within a mold lineage (A8). Target-only (D5): appended to
# TARGET_LDFLAGS, never BUILD_LDFLAGS.
MOLD_LDFLAGS ?= "-fuse-ld=mold -B${MOLD_WRAPPER_DIR} -Wl,--build-id=sha256"
TARGET_LDFLAGS:append = " ${MOLD_LDFLAGS}"

python () {
    # Never scope the flag or the mold-provider dependency onto native/cross/
    # crosssdk/nativesdk/allarch recipes: their CC is the build/host compiler
    # (D5), and a mold-native DEPENDS here would reach mold's own cmake-native /
    # ninja-native and form a parse-time cycle (D3). Skip the gate entirely.
    for cls in ('native', 'nativesdk', 'cross', 'crosssdk', 'cross-canadian', 'allarch'):
        if bb.data.inherits_class(cls, d):
            return

    if bb.utils.to_boolean(d.getVar('MOLD_DISABLE')):
        d.setVar('MOLD_LDFLAGS', '')
        return

    # Respect a recipe's own linker pin. A recipe that forces -fuse-ld=bfd/gold/
    # lld (glibc, libgcc, zlib, and the ld-is-lld/ptest-conditional set) has
    # deliberately opted into that linker; our -B<wrapper> injection breaks it -
    # the forced non-mold linker falls through -B to the host x86 ld.bfd
    # (unrecognised aarch64 emulation) instead of the cross linker. Never mold a
    # recipe that pins its own linker, in ANY mode. Checked before the baseline
    # branch sets our own -fuse-ld=bfd, so only the recipe's pin matches here (our
    # default MOLD_LDFLAGS at this point is -fuse-ld=mold). This generalises the
    # glibc deny-list entry to every self-pinning recipe without a hardcoded list
    # that drifts across oe-core versions.
    ldflags = d.getVar('LDFLAGS') or ''
    if any(pin in ldflags for pin in ('-fuse-ld=bfd', '-fuse-ld=gold', '-fuse-ld=lld')):
        d.setVar('MOLD_LDFLAGS', '')
        return

    pn = d.getVar('PN')
    mode = (d.getVar('MOLD_MODE') or 'list').strip()
    included = (d.getVar('MOLD_INCLUDED_PN') or '').split()
    excluded = (d.getVar('MOLD_EXCLUDED_PN') or '').split()

    if mode == 'global':
        # Deny-list: blank the var for excluded recipes (never re-pin bfd, D4).
        if pn in excluded:
            d.setVar('MOLD_LDFLAGS', '')
            return
    elif mode == 'baseline':
        # Measurement arm: bfd over the SAME allow-list, through the same -B
        # wrapper dir, so the A/B differs only in the linker (D7).
        if pn not in included:
            d.setVar('MOLD_LDFLAGS', '')
            return
        d.setVar('MOLD_LDFLAGS',
                 '-fuse-ld=bfd -B%s -Wl,--build-id=sha256' % d.getVar('MOLD_WRAPPER_DIR'))
    else:
        # list (default): allow-list. Blank the var for non-members.
        if pn not in included:
            d.setVar('MOLD_LDFLAGS', '')
            return

    # Reached only for a recipe that links through the -B wrapper. The baseline
    # arm links with the cross bfd; only the mold arm needs the mold binary, so
    # pull mold-native into THIS recipe for the mold arm alone (the native/cross
    # classes returned above, so no D3 cycle). Stage the arm-appropriate wrapper.
    arm = 'bfd' if mode == 'baseline' else 'mold'
    d.setVar('MOLD_WRAP_ARM', arm)
    if arm == 'mold':
        d.appendVar('DEPENDS', ' mold-native')

    # Export COMPILER_PATH so gcc still finds the wrapper when libtool strips the
    # -B<wrapper> flag from its generated link line. libtool filters command-line
    # flags but inherits the environment, and COMPILER_PATH is gcc's env-var
    # subprogram search path (tried after GCC_EXEC_PREFIX); -B alone does not
    # survive libtool, so autotools recipes could not find ld.mold. The wrapper
    # dir holds only our one arm wrapper, so no other subprogram is shadowed.
    wrapdir = d.getVar('MOLD_WRAPPER_DIR')
    existing = d.getVar('COMPILER_PATH') or ''
    d.setVar('COMPILER_PATH', wrapdir + (':' + existing if existing else ''))
    d.setVarFlag('COMPILER_PATH', 'export', '1')

    d.appendVarFlag('do_prepare_recipe_sysroot', 'postfuncs', ' mold_stage_wrappers')
}

# Stage the arm-appropriate timing wrapper into the -B wrapper dir under
# RECIPE_SYSROOT_NATIVE (D2), never the target sysroot. Runs as a
# do_prepare_recipe_sysroot postfunc so it lands after the real linkers (the
# cross bfd from binutils-cross, ld.mold from mold-native) are staged. Bake the
# wrapper's REAL_LINKER to the absolute arch-correct linker so its own lookup
# never falls back to the bare-name PATH scan (which finds host ld.bfd first).
mold_stage_wrappers () {
    install -d "${MOLD_WRAPPER_DIR}"
    if [ "${MOLD_WRAP_ARM}" = "bfd" ]; then
        install -m 0755 "${MOLD_WRAPPER_SRC}" "${MOLD_WRAPPER_DIR}/ld.bfd"
        sed -i 's|^REAL_LINKER=""|REAL_LINKER="${STAGING_BINDIR_TOOLCHAIN}/${TARGET_PREFIX}ld.bfd"|' "${MOLD_WRAPPER_DIR}/ld.bfd"
    else
        install -m 0755 "${MOLD_WRAPPER_SRC}" "${MOLD_WRAPPER_DIR}/ld.mold"
        sed -i 's|^REAL_LINKER=""|REAL_LINKER="${STAGING_BINDIR_NATIVE}/ld.mold"|' "${MOLD_WRAPPER_DIR}/ld.mold"
    fi
}
