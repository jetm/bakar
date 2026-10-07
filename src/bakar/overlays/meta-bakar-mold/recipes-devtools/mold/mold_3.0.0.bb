SUMMARY = "mold: a modern, high-speed drop-in replacement for ld.bfd/gold/lld"
DESCRIPTION = "mold is a faster drop-in replacement for the existing Unix \
               linkers. Version 3 is a rewrite in Rust of the original C++ \
               implementation and supports ELF targets."
HOMEPAGE = "https://github.com/rui314/mold"
BUGTRACKER = "https://github.com/rui314/mold/issues"
SECTION = "devel"

# This recipe tracks the one proposed to meta-openembedded
# (meta-oe/recipes-devtools/mold). It differs only where scarthgap and wrynose
# need it: ;subdir=${BP} on the mold checkout, nobranch=1 in place of
# branch=main;tag=v${PV} (scarthgap's bitbake rejects a tag next to SRCREV), and
# the python block below that gates rust older than 1.95. SRCREV is the commit
# the v${PV} tag points at.
#
# mold_git.bb is kept beside this recipe as a reference for building an
# unreleased upstream commit. This released recipe is the default: it has the
# higher version, and mold_git.bb sets a negative DEFAULT_PREFERENCE.
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5=3fb62e3fb2aa1c0f7d16e43be0107e99"

DEPENDS = "zstd"
DEPENDS:append:class-target = " libstd-rs"

# The git fetcher unpacks to git/ by default on scarthgap while S is
# ${WORKDIR}/${BP}, so the mold entry sets subdir=${BP} to land where S points
# on every release (wrynose's bitbake.conf defaults the destination to ${BP}
# itself). destsuffix=${BP} would place it identically, but with name= set it
# also makes cargo_common_do_patch_paths write a [patch] entry pointing the mold
# repo at its own checkout; this entry stays unnamed and uses subdir instead.
SRC_URI = "\
    git://github.com/rui314/mold.git;protocol=https;nobranch=1;subdir=${BP} \
    gitsm://github.com/rui314/mimalloc_rust.git;protocol=https;nobranch=1;name=mimalloc;destsuffix=mimalloc_rust;type=git-dependency \
"
SRCREV = "8de38c35a2df16a25f7ff87ac3ad07156a925beb"
SRCREV_mimalloc = "3979460494f1cd1e7f936cb8e10f41e927c9f698"
SRCREV_FORMAT = "default_mimalloc"

inherit cargo cargo-update-recipe-crates pkgconfig

require ${BPN}-crates.inc

# mold 3.0.0 declares rust-version 1.95 in Cargo.toml. Older toolchains are
# handled here for this one native tool, without patching mold's sources: below
# 1.95.0 cargo is told to ignore the declared version and RUSTC_BOOTSTRAP opts
# in, from the command line, to the library features that were still unstable
# before the release that stabilized them: hint::cold_path (1.95.0) and
# fmt::from_fn, behind debug_closure_helpers (1.94.0; scarthgap's rust is
# 1.92.0). From 1.95.0 on none of this is set and the stock compiler builds it.
python () {
    rustversion = (d.getVar("RUSTVERSION") or "").rstrip("%")
    if rustversion and bb.utils.vercmp_string(rustversion, "1.95.0") < 0:
        features = ["cold_path"]
        if bb.utils.vercmp_string(rustversion, "1.94.0") < 0:
            features.append("debug_closure_helpers")
        d.appendVar("CARGO_BUILD_FLAGS", " --ignore-rust-version")
        d.setVar("RUSTC_BOOTSTRAP", "1")
        d.setVarFlag("RUSTC_BOOTSTRAP", "export", "1")
        d.appendVar("RUSTFLAGS", " -Zcrate-attr=feature(%s)" % ",".join(features))
}

# mold finds mold-wrapper.so (used by "mold -run") in $MOLD_LIBDIR/mold,
# a path baked in at build time.
export MOLD_LIBDIR = "${libdir}"

# cargo.bbclass does not install shared objects, so the wrapper, the man page
# and the ld aliases that upstream's install-mold.sh would create are
# installed here.
do_install:append () {
    install -d ${D}${libdir}/mold
    install -m 0755 ${B}/target/${CARGO_TARGET_SUBDIR}/mold-wrapper.so ${D}${libdir}/mold/

    install -d ${D}${mandir}/man1
    install -m 0644 ${S}/docs/mold.1 ${D}${mandir}/man1/
    ln -sf mold.1 ${D}${mandir}/man1/ld.mold.1

    ln -sf mold ${D}${bindir}/ld.mold
    install -d ${D}${libexecdir}/mold
    ln -sf ${@os.path.relpath(d.getVar('bindir'), d.getVar('libexecdir') + '/mold')}/mold ${D}${libexecdir}/mold/ld
}

BBCLASSEXTEND = "native"
