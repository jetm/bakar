SUMMARY = "mold: a modern, high-speed drop-in replacement for ld.bfd/gold/lld (Rust rewrite)"
HOMEPAGE = "https://github.com/rui314/mold"
DESCRIPTION = "mold is a faster drop-in replacement for existing Unix linkers. \
This recipe builds the Rust rewrite from upstream main, which uses mimalloc \
only as Rust's global allocator and does not override libc's malloc."

# The Rust rewrite carries no bundled C++ third-party trees, so the license
# file at the top of the checkout is the only one to checksum. The crate
# dependencies are permissively licensed (MIT / Apache-2.0 / BSD / CC0); this
# is a build-host tool that never reaches an image, so they are not enumerated.
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5=3fb62e3fb2aa1c0f7d16e43be0107e99"

# Upstream main, pinned by SRCREV. The Rust rewrite is unreleased; the stable
# branch (and the v2.42.1 tag the C++ recipe builds) still overrides libc's
# malloc through vendored mimalloc, which crashes under pseudo.
#
# mold's cli crate takes mimalloc from a git dependency
# (rui314/mimalloc_rust), so it is fetched here rather than from crates.io.
# cargo_common_do_patch_paths turns the name/destsuffix pair into a
# [patch] path entry; libmimalloc-sys resolves through mimalloc's own
# relative path once Cargo.lock's git sources are stripped. gitsm pulls the
# microsoft/mimalloc submodules libmimalloc-sys compiles.
SRC_URI = "\
    gitsm://github.com/rui314/mold.git;protocol=https;nobranch=1;name=mold \
    gitsm://github.com/rui314/mimalloc_rust.git;protocol=https;nobranch=1;name=mimalloc;destsuffix=mimalloc_rust \
"
SRCREV_mold = "a9c709b8a437c1e1627b065b771e32ce331ba363"
SRCREV_mimalloc = "cc58b72775b8b5bc89ee37752af61fcb4f7c2909"
SRCREV_FORMAT = "mold_mimalloc"

# "+git" sorts above the C++ recipe's 2.42.1, so this one wins by default.
PV = "2.42.1+git"

inherit cargo cargo-update-recipe-crates

require mold-crates.inc

# mold main tracks current stable Rust and uses two std APIs that rustc 1.94.1,
# the tree's toolchain, still gates (E0658: hint::cold_path and the atomic
# update method). RUSTC_BOOTSTRAP lets this one native tool opt in to them from
# the command line instead of patching mold's sources. Drop both lines once the
# tree's rust no longer gates them.
export RUSTC_BOOTSTRAP = "1"
RUSTFLAGS += "-Zcrate-attr=feature(cold_path,atomic_try_update)"

# The C++ recipe's CMake install staged an ld.mold symlink next to mold;
# mold.bbclass discovers the wrapper directory from it.
do_install:append () {
    ln -sf mold ${D}${bindir}/ld.mold
}

BBCLASSEXTEND = "native"
