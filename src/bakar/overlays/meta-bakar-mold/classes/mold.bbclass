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
# Linker discovery is via a -B<wrapper-dir> under RECIPE_SYSROOT_NATIVE (D2), not
# a target-sysroot drop: a -native recipe cannot own a ${TARGET_SYS} tooldir
# without breaking native sstate sharing across arches. -B prefixes are searched
# ahead of the standard tooldir, sidestepping the collect2 --sysroot lookup
# failure (mold #564). The wrapper dir holds a timing wrapper installed as BOTH
# ld.mold AND ld.bfd (D6) so the mold and bfd measurement arms are symmetric.
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

    # Reached only for a recipe that will link through the -B wrappers. Pull the
    # mold provider into THIS recipe only (the native/cross classes returned
    # above, so no cycle) and stage the timing wrappers into the -B dir.
    d.appendVar('DEPENDS', ' mold-native')
    d.appendVarFlag('do_prepare_recipe_sysroot', 'postfuncs', ' mold_stage_wrappers')
}

# Stage the timing wrapper as BOTH ld.mold and ld.bfd into the -B wrapper dir
# under RECIPE_SYSROOT_NATIVE (D6), never the target sysroot (D2). Runs as a
# do_prepare_recipe_sysroot postfunc so the wrappers land after the real
# ld.mold (from mold-native) is staged into the native sysroot.
mold_stage_wrappers () {
    install -d "${MOLD_WRAPPER_DIR}"
    install -m 0755 "${MOLD_WRAPPER_SRC}" "${MOLD_WRAPPER_DIR}/ld.mold"
    install -m 0755 "${MOLD_WRAPPER_SRC}" "${MOLD_WRAPPER_DIR}/ld.bfd"
}
