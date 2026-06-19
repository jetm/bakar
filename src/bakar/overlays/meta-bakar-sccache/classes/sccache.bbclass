#
# sccache.bbclass - route eligible target compiles through sccache-dist.
#
# Modeled on oe-core's meta/classes/ccache.bbclass: set CCACHE per-recipe
# through an anonymous python gate so only compatible recipes route through the
# launcher, and honor the same per-recipe CCACHE_DISABLE escape hatch. Enable
# with `INHERIT += "sccache"` (the bakar sccache tuning overlay does this and
# also `INHERIT:remove = "ccache"`, since the two launchers are mutually
# exclusive).
#
# sccache-dist differs from ccache in three ways that this class must handle,
# none of which ccache.bbclass needs to:
#
#  1. It ships jobs to a remote build-server, so it must stay OFF the build/host
#     compiler. OE prepends ${CCACHE} to BUILD_CC/BUILD_CXX (gcc-native.bbclass)
#     as well as the target CC. A leaked sccache there ships host-tool compiles
#     (e.g. linux-libc-headers' fixdep, built with HOSTCC="${BUILD_CC}") to the
#     build-server, where they need network the install task lacks and hit the
#     unpackageable host `as`. Force BUILD_CC/BUILD_CXX back to the
#     gcc-native.bbclass definitions minus ${CCACHE}; :forcevariable beats the
#     class assignment regardless of inherit order.
#  2. It cannot package a host gcc whose assembler is PATH-relative (Arch's host
#     gcc reports a bare `as`), so native/cross/SDK toolchain recipes - whose CC
#     IS the host compiler - must compile locally. The class gate excludes them.
#  3. Wrapping even the target compiler breaks the kernel's kconfig compiler
#     detection (`sccache aarch64-...-gcc: unknown C compiler`), and the kernel's
#     config step has no network for its host kconfig tools. The kernel is
#     excluded entirely; its do_compile stays local for now.

# Per-recipe opt-out, mirrors CCACHE_DISABLE.
SCCACHE_DISABLE ??= ""

# Recipe classes that compile with the host/build compiler (the unpackageable
# `as` case) or that break under a wrapped compiler (kernel). Excluded from
# distribution; they fall back to the local compiler.
SCCACHE_EXCLUDED_CLASSES ?= "native cross crosssdk nativesdk cross-canadian kernel"

python () {
    if (bb.utils.to_boolean(d.getVar('SCCACHE_DISABLE')) or
            bb.utils.to_boolean(d.getVar('CCACHE_DISABLE'))):
        return
    for cls in d.getVar('SCCACHE_EXCLUDED_CLASSES').split():
        if bb.data.inherits_class(cls, d):
            return
    d.setVar('CCACHE', 'sccache ')
}

# Keep the build/host compiler local even inside eligible target recipes
# (reason 1 above). Definitions copied from gcc-native.bbclass minus ${CCACHE}.
BUILD_CC:forcevariable = "${BUILD_PREFIX}gcc ${BUILD_CC_ARCH}"
BUILD_CXX:forcevariable = "${BUILD_PREFIX}g++ ${BUILD_CC_ARCH}"

# cmake.bbclass's oecmake_map_compiler splits the compiler launcher out of CC,
# but only recognizes the literal "ccache" - with CC="sccache <gcc>" it makes
# sccache itself the compiler, so cmake's compiler check runs `sccache <flags>`
# and dies "unexpected argument '-m'". Re-derive the OECMAKE compiler/launcher
# split with a helper that recognizes sccache too. cmake.bbclass uses ?= for
# these, so this plain assignment wins; its :allarch = "" override still wins for
# allarch recipes (which do not compile). The NATIVE_* launchers read BUILD_CC,
# already stripped above, so they need no override.
def sccache_map_compiler(varname, d):
    args = (d.getVar(varname) or "").split()
    if args and args[0] in ('ccache', 'sccache'):
        return args[1], args[0]
    return (args[0] if args else ''), ''

OECMAKE_C_COMPILER = "${@sccache_map_compiler('CC', d)[0]}"
OECMAKE_C_COMPILER_LAUNCHER = "${@sccache_map_compiler('CC', d)[1]}"
OECMAKE_CXX_COMPILER = "${@sccache_map_compiler('CXX', d)[0]}"
OECMAKE_CXX_COMPILER_LAUNCHER = "${@sccache_map_compiler('CXX', d)[1]}"

# Put sccache on bitbake's task PATH. OE restricts each task's PATH to sysroot
# bins plus the HOSTTOOLS allowlist (tmp/hosttools/); the host /usr/bin/sccache
# is invisible to recipes unless allowlisted.
HOSTTOOLS += "sccache"

# Let the compiler reach the scheduler. bitbake runs each task in a fresh
# network namespace (loopback down) via unshare(CLONE_NEWNET) unless the task
# sets [network] = "1" - only do_fetch opts in by default. The sccache client
# ships jobs from every task that runs the compiler: do_configure (compiler
# tests), do_compile, do_install (some recipes link with the target gcc at
# install, e.g. glibc's format.lds), and the ptest.bbclass mirrors of all three
# (do_compile_ptest_base builds the test binaries). A [network] flag on a task a
# recipe does not define is harmless.
do_configure[network] = "1"
do_compile[network] = "1"
do_install[network] = "1"
do_configure_ptest_base[network] = "1"
do_compile_ptest_base[network] = "1"
do_install_ptest_base[network] = "1"

# Point the in-build sccache client at the configured scheduler. Empty when
# unset, which leaves the client on its own config / local-cache mode. bakar
# exports BAKAR_SCCACHE_SCHEDULER_URL and the tuning overlay whitelists it
# through kas's BB_ENV_PASSTHROUGH_ADDITIONS.
export SCCACHE_DIST_SCHEDULER_URL = "${@os.environ.get('BAKAR_SCCACHE_SCHEDULER_URL', '')}"
