"""Validation and write confinement for the feed mirror (``bakar.feed_mirror_paths``)."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from bakar.feed_mirror_paths import (
    UnsafePathError,
    confine_metadata_href,
    confine_package_href,
    resolve_destination,
    validate_repo_path,
    validate_source_url,
)

_SHA = "ab" + "0" * 62


# --- validate_source_url ---------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://repo.avocadolinux.org", "https://repo.avocadolinux.org"),
        ("https://repo.avocadolinux.org/", "https://repo.avocadolinux.org"),
        ("http://127.0.0.1:8080/mirror/", "http://127.0.0.1:8080/mirror"),
        ("HTTPS://Example.com/feeds", "HTTPS://Example.com/feeds"),
    ],
)
def test_source_url_http_and_https_accepted_with_trailing_slash_stripped(url: str, expected: str) -> None:
    assert validate_source_url(url) == expected


@pytest.mark.parametrize("url", ["file:///etc", "ftp://mirror.example.com/feed", "gopher://h/x", "/srv/feed"])
def test_source_url_other_scheme_rejected_naming_accepted_schemes(url: str) -> None:
    with pytest.raises(UnsafePathError) as exc:
        validate_source_url(url)
    message = str(exc.value)
    assert repr(url) in message
    assert "http://" in message
    assert "https://" in message


@pytest.mark.parametrize("url", ["https://user:pw@host/feed", "https://user@host/feed", "http://:pw@host:81/x"])
def test_source_url_credentials_rejected_without_echoing_secret(url: str) -> None:
    with pytest.raises(UnsafePathError, match="credentials") as exc:
        validate_source_url(url)
    assert "pw" not in str(exc.value)
    assert "host" in str(exc.value)


@pytest.mark.parametrize("url", ["https://host/feed?x=1", "https://host/feed?", "https://host/feed#frag"])
def test_source_url_query_or_fragment_rejected(url: str) -> None:
    with pytest.raises(UnsafePathError, match="query string or fragment") as exc:
        validate_source_url(url)
    assert repr(url) in str(exc.value)


@pytest.mark.parametrize("url", ["https://", "http:///feed", "", "https://ho st/", "https://host:notaport/"])
def test_source_url_without_usable_host_rejected(url: str) -> None:
    with pytest.raises(UnsafePathError) as exc:
        validate_source_url(url)
    assert repr(url) in str(exc.value)


# --- validate_repo_path ----------------------------------------------------


@pytest.mark.parametrize("path", ["sdk/all", "target/rzv2n-sr-som-ext", "target/cortexa55", "all"])
def test_repo_path_relative_posix_accepted(path: str) -> None:
    assert validate_repo_path(path, origin="operator") == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/sdk/all",
        "sdk\\all",
        "sdk/\x00all",
        "sdk//all",
        "sdk/all/",
        "./sdk/all",
        "sdk/./all",
        "../../etc",
        "sdk/../target/m1",
        "_pkgs",
        "_pkgs/ab",
        "snapshots/1/sdk/all",
    ],
)
@pytest.mark.parametrize("origin", ["operator", "source index"])
def test_repo_path_unsafe_rejected_naming_value_and_origin(path: str, origin: str) -> None:
    with pytest.raises(UnsafePathError) as exc:
        validate_repo_path(path, origin=origin)
    message = str(exc.value)
    assert repr(path) in message
    assert origin in message


def test_repo_path_reserved_name_allowed_below_first_segment() -> None:
    assert validate_repo_path("target/_pkgs", origin="operator") == "target/_pkgs"


# --- confine_metadata_href -------------------------------------------------


@pytest.mark.parametrize(
    ("href", "name"),
    [
        (f"repodata/{_SHA}-primary.xml.gz", f"{_SHA}-primary.xml.gz"),
        ("repodata/filelists.xml.zst", "filelists.xml.zst"),
    ],
)
def test_confine_metadata_href_accepts_one_file_under_repodata(href: str, name: str) -> None:
    assert confine_metadata_href(href) == name


@pytest.mark.parametrize(
    "href",
    [
        "repodata/repomd.xml",
        "repodata/repomd.xml.asc",
        "repodata/repomd.xml.key",
        "repodata/primary.xml.gz.part",
        "repodata/",
        "repodata/.",
        "repodata/..",
        "repodata/sub/primary.xml.gz",
        "repodata",
        "primary.xml.gz",
        "../repodata/primary.xml.gz",
        "/repodata/primary.xml.gz",
        "other/primary.xml.gz",
        "repodata\\primary.xml.gz",
        "repodata/prim\x00ary.xml.gz",
        "",
    ],
)
def test_confine_metadata_href_rejects(href: str) -> None:
    with pytest.raises(UnsafePathError) as exc:
        confine_metadata_href(href)
    assert repr(href) in str(exc.value)


# --- confine_package_href --------------------------------------------------


@pytest.mark.parametrize(
    ("repo", "href", "expected"),
    [
        ("sdk/all", f"../../_pkgs/ab/{_SHA}.rpm", f"_pkgs/ab/{_SHA}.rpm"),
        (
            "target/cortexa55",
            "Packages/a/a52dec-0.7.4-r0.0.cortexa55.rpm",
            "target/cortexa55/Packages/a/a52dec-0.7.4-r0.0.cortexa55.rpm",
        ),
        ("target/m1", "./x.rpm", "target/m1/x.rpm"),
        ("all", f"../_pkgs/ab/{_SHA}.rpm", f"_pkgs/ab/{_SHA}.rpm"),
    ],
)
def test_confine_package_href_worked_examples(repo: str, href: str, expected: str) -> None:
    assert confine_package_href(href, repo=repo) == PurePosixPath(expected)


@pytest.mark.parametrize(
    ("repo", "href"),
    [
        ("sdk/all", "../../../../etc/passwd.rpm"),
        ("sdk/all", "../../../x.rpm"),
        ("target/m1", "../qemux86-64/repodata/x.rpm"),
        ("target/m1", "../qemux86-64/Packages/x.rpm"),
        ("target/m1", "../m1-ext/x.rpm"),
        ("target/m1", "repodata/x.rpm"),
        ("target/m1", "Packages/../repodata/x.rpm"),
        ("sdk/all", "../../_pkgs/repodata/x.rpm"),
        ("sdk/all", "../../_pkgs.rpm"),
        ("sdk/all", "../../snapshots/1/sdk/all/x.rpm"),
        ("target/m1", "snapshots/1/x.rpm"),
        ("sdk/all", "../x.rpm"),
    ],
)
def test_confine_package_href_rejects_landing_outside_pool_or_own_repo(repo: str, href: str) -> None:
    with pytest.raises(UnsafePathError) as exc:
        confine_package_href(href, repo=repo)
    assert repr(href) in str(exc.value)


@pytest.mark.parametrize(
    "href",
    [
        "",
        "/etc/passwd.rpm",
        "https://evil.example/x.rpm",
        "file:///etc/x.rpm",
        "//evil.example/x.rpm",
        "Packages\\x.rpm",
        "Packages/x\x00.rpm",
        "Packages//x.rpm",
        "Packages/x.srpm.txt",
        "Packages/x",
        "Packages/",
    ],
)
def test_confine_package_href_rejects_malformed(href: str) -> None:
    with pytest.raises(UnsafePathError) as exc:
        confine_package_href(href, repo="target/m1")
    assert repr(href) in str(exc.value)


def test_confine_package_href_rejects_xml_base() -> None:
    with pytest.raises(UnsafePathError, match="xml:base") as exc:
        confine_package_href("Packages/x.rpm", repo="target/m1", base="https://elsewhere.example/")
    assert "'https://elsewhere.example/'" in str(exc.value)


def test_confine_package_href_rejects_empty_xml_base() -> None:
    with pytest.raises(UnsafePathError, match="xml:base"):
        confine_package_href("Packages/x.rpm", repo="target/m1", base="")


@pytest.mark.parametrize("repo", ["../..", "_pkgs", "/abs"])
def test_confine_package_href_rejects_unsafe_repo(repo: str) -> None:
    with pytest.raises(UnsafePathError) as exc:
        confine_package_href("x.rpm", repo=repo)
    assert repr(repo) in str(exc.value)


# --- resolve_destination ---------------------------------------------------


def test_confine_resolve_destination_inside_channel(tmp_path: Path) -> None:
    channel = tmp_path / "2024" / "edge"
    channel.mkdir(parents=True)
    dest = resolve_destination(channel, PurePosixPath(f"_pkgs/ab/{_SHA}.rpm"))
    assert dest == channel.resolve() / "_pkgs" / "ab" / f"{_SHA}.rpm"


def test_confine_resolve_destination_through_symlinked_feed_root(tmp_path: Path) -> None:
    real_root = tmp_path / "disk" / "feed"
    (real_root / "2024" / "edge").mkdir(parents=True)
    link_root = tmp_path / "feed"
    link_root.symlink_to(real_root, target_is_directory=True)
    channel = link_root / "2024" / "edge"

    dest = resolve_destination(channel, PurePosixPath("sdk/all/Packages/x.rpm"))

    assert dest == real_root / "2024" / "edge" / "sdk" / "all" / "Packages" / "x.rpm"


def test_confine_resolve_destination_rejects_symlink_escaping_channel(tmp_path: Path) -> None:
    channel = tmp_path / "2024" / "edge"
    channel.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (channel / "_pkgs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafePathError, match="outside"):
        resolve_destination(channel, PurePosixPath("_pkgs/ab/x.rpm"))


def test_confine_resolve_destination_rejects_dotdot_escape(tmp_path: Path) -> None:
    channel = tmp_path / "2024" / "edge"
    channel.mkdir(parents=True)
    with pytest.raises(UnsafePathError) as exc:
        resolve_destination(channel, PurePosixPath("../other/x.rpm"))
    assert "'../other/x.rpm'" in str(exc.value)


@pytest.mark.parametrize("relative", ["/etc/passwd", "."])
def test_confine_resolve_destination_rejects_absolute_or_channel_itself(tmp_path: Path, relative: str) -> None:
    with pytest.raises(UnsafePathError):
        resolve_destination(tmp_path, PurePosixPath(relative))
