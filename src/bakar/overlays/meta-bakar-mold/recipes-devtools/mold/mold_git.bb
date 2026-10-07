SUMMARY = "mold: a modern, high-speed drop-in replacement for ld.bfd/gold/lld (Rust rewrite)"
HOMEPAGE = "https://github.com/rui314/mold"
DESCRIPTION = "mold is a faster drop-in replacement for existing Unix linkers. \
This recipe builds the Rust rewrite from upstream main, which uses mimalloc \
only as Rust's global allocator and does not override libc's malloc."

# REFERENCE RECIPE, not selected by default. mold_3.0.0.bb is the recipe in use;
# this one is kept for building an unreleased upstream commit when a fix is
# needed before the next release. DEFAULT_PREFERENCE is negative so that
# bumping this recipe's PV past the released version can never make it win
# silently; opt in with PREFERRED_VERSION_mold-native = "2.42.1+git%" (use the
# PV below). Its crate list is mold-git-crates.inc, frozen on purpose: do not
# run `bitbake -c update_crates` on this recipe, because that task writes
# ${BPN}-crates.inc, which is the file mold_3.0.0.bb uses. Regenerate
# mold-git-crates.inc by hand from the Cargo.lock at the new SRCREV.
DEFAULT_PREFERENCE = "-1"

# The Rust rewrite carries no bundled C++ third-party trees, so the license
# file at the top of the checkout is the only one to checksum. The crate
# dependencies are permissively licensed (MIT / Apache-2.0 / BSD / CC0); this
# is a build-host tool that never reaches an image, so they are not enumerated.
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5=3fb62e3fb2aa1c0f7d16e43be0107e99"

# Upstream main, pinned by SRCREV_mold and moved only by editing it. The Rust
# rewrite is unreleased; the C++ releases (stable branch, v2.42.1) override
# libc's malloc through vendored mimalloc, which crashes under pseudo, so there
# is no C++ fallback recipe here.
#
# mold's cli crate takes mimalloc from a git dependency
# (rui314/mimalloc_rust), so it is fetched here rather than from crates.io.
# cargo_common_do_patch_paths turns the name/destsuffix pair into a
# [patch] path entry; libmimalloc-sys resolves through mimalloc's own
# relative path once Cargo.lock's git sources are stripped. gitsm pulls the
# microsoft/mimalloc submodules libmimalloc-sys compiles.
#
# The mold entry sets subdir=${BP} so the checkout lands exactly where S
# points on every release. S is ${WORKDIR}/${BP} on scarthgap and
# ${UNPACKDIR}/${BP} on wrynose, but only wrynose's bitbake.conf also sets
# BB_GIT_DEFAULT_DESTSUFFIX = "${BP}"; scarthgap's git fetcher still defaults
# to git/, which leaves S empty and fails do_populate_lic on LIC_FILES_CHKSUM.
# A bare S = "${WORKDIR}/git" is not an alternative: it is a fatal QA error on
# wrynose, where S lives under UNPACKDIR. destsuffix=${BP} is not either: with
# name= set, cargo_common_do_patch_paths would also write a [patch] entry that
# points the mold repo at its own checkout. subdir places the checkout the same
# way and leaves that function's name+destsuffix condition untouched.
SRC_URI = "\
    gitsm://github.com/rui314/mold.git;protocol=https;nobranch=1;name=mold;subdir=${BP} \
    gitsm://github.com/rui314/mimalloc_rust.git;protocol=https;nobranch=1;name=mimalloc;destsuffix=mimalloc_rust \
"
SRCREV_mold = "a9c709b8a437c1e1627b065b771e32ce331ba363"
SRCREV_mimalloc = "cc58b72775b8b5bc89ee37752af61fcb4f7c2909"
SRCREV_FORMAT = "mold_mimalloc"

PV = "2.42.1+git"

inherit cargo cargo-update-recipe-crates

require mold-git-crates.inc

# mold at this commit needs rustc >= 1.95.0: it uses hint::cold_path and the
# atomic update method, both stable from 1.95.0. Built outside Yocto, the stock
# 1.95.0 toolchain compiles it and stock 1.94.1 stops with E0658. The tree
# selects its toolchain through RUSTVERSION (a "1.94.1%" style wildcard), so
# below 1.95.0 this one native tool opts in to the two features from the
# command line with RUSTC_BOOTSTRAP instead of patching mold's sources; from
# 1.95.0 on neither is set and the stock compiler builds it. Below 1.94.0
# (scarthgap's rust is 1.92.0) fmt::from_fn is still unstable too, so a third
# feature, debug_closure_helpers, is added there.
python () {
    rustversion = (d.getVar("RUSTVERSION") or "").rstrip("%")
    if rustversion and bb.utils.vercmp_string(rustversion, "1.95.0") < 0:
        features = ["cold_path", "atomic_try_update"]
        # fmt::from_fn is stable from 1.94.0; scarthgap's 1.92.0 still gates it.
        if bb.utils.vercmp_string(rustversion, "1.94.0") < 0:
            features.append("debug_closure_helpers")
        d.setVar("RUSTC_BOOTSTRAP", "1")
        d.setVarFlag("RUSTC_BOOTSTRAP", "export", "1")
        d.appendVar("RUSTFLAGS", " -Zcrate-attr=feature(%s)" % ",".join(features))
}

# mold.bbclass discovers its -B wrapper directory from an ld.mold symlink next
# to the mold binary.
do_install:append () {
    ln -sf mold ${D}${bindir}/ld.mold
}

BBCLASSEXTEND = "native"
