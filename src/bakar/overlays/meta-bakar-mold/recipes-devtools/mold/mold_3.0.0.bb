SUMMARY = "mold: a modern, high-speed drop-in replacement for ld.bfd/gold/lld"
HOMEPAGE = "https://github.com/rui314/mold"
DESCRIPTION = "mold 3.0 is the Rust rewrite of mold and a drop-in replacement \
for 2.42.1. It uses mimalloc only as Rust's global allocator and does not \
override libc's malloc."

# mold_git.bb is kept beside this recipe as a reference for building an
# unreleased upstream commit. This released recipe is the default: it has the
# higher version, and mold_git.bb sets a negative DEFAULT_PREFERENCE.
#
# The checkout carries no bundled third-party trees, so the license file at the
# top of the source is the only one to checksum. The crate dependencies are
# permissively licensed (MIT / Apache-2.0 / BSD / CC0); this is a build-host
# tool that never reaches an image, so they are not enumerated.
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5=3fb62e3fb2aa1c0f7d16e43be0107e99"

# mold is fetched from git, pinned by SRCREV_mold to the commit the v${PV} tag
# points at. The GitHub archive tarball is not an option: oe-core's src-uri-bad
# check rejects "unstable" github.com/.../archive/ URLs, and it is a warning on
# scarthgap but an error on wrynose.
#
# The git fetcher unpacks to git/ by default on scarthgap while S is
# ${WORKDIR}/${BP}, so the mold entry sets subdir=${BP} to land where S points
# on every release (wrynose's bitbake.conf defaults the destination to ${BP}
# itself). destsuffix=${BP} would place it identically, but with name= set it
# also makes cargo_common_do_patch_paths write a [patch] entry pointing the mold
# repo at its own checkout; subdir avoids that. There is no .gitmodules in mold
# itself, so plain git is enough for this entry.
#
# mold's cli crate takes mimalloc from a git dependency
# (rui314/mimalloc_rust, pinned by rev in cli/Cargo.toml), so it is fetched here
# rather than from crates.io. cargo_common_do_patch_paths turns the
# name/destsuffix pair into a [patch] path entry; libmimalloc-sys resolves
# through mimalloc's own relative path once Cargo.lock's git sources are
# stripped. gitsm pulls the microsoft/mimalloc submodules libmimalloc-sys
# compiles. SRCREV_mimalloc must equal the rev in cli/Cargo.toml and Cargo.lock.
SRC_URI = "\
    git://github.com/rui314/mold.git;protocol=https;nobranch=1;name=mold;subdir=${BP} \
    gitsm://github.com/rui314/mimalloc_rust.git;protocol=https;nobranch=1;name=mimalloc;destsuffix=mimalloc_rust \
"
SRCREV_mold = "8de38c35a2df16a25f7ff87ac3ad07156a925beb"
SRCREV_mimalloc = "3979460494f1cd1e7f936cb8e10f41e927c9f698"
SRCREV_FORMAT = "mold_mimalloc"

inherit cargo cargo-update-recipe-crates

require mold-crates.inc

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

# mold.bbclass discovers its -B wrapper directory from an ld.mold symlink next
# to the mold binary.
do_install:append () {
    ln -sf mold ${D}${bindir}/ld.mold
}

BBCLASSEXTEND = "native"
