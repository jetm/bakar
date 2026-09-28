"""Self-test for the synthetic upstream feed in ``tests._feed_upstream``.

Every mirror test trusts this fixture to publish a self-consistent feed and to
inject exactly the faults it is asked for, so the fixture's own claims are
checked here over real HTTP, the way a mirror would read them.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

import pytest
from defusedxml import ElementTree as ET  # noqa: N817 - ET for ElementTree is the established convention

from tests._feed_upstream import COMMON_NS, REPO_NS, FeedSpec, build_feed, serve_feed

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests._feed_upstream import FeedServer, UpstreamFeed

pytest_plugins = ["tests._feed_upstream"]
pytestmark = pytest.mark.unit

_REPO = {"repo": REPO_NS}
_COMMON = {"c": COMMON_NS}

# Every URL these tests request names the loopback server this same test
# started (``server.url``, an ephemeral 127.0.0.1 port) - never a value that
# crosses a trust boundary. A plain opener, rather than the module-level
# ``urlopen`` function, is what bakar.feed_mirror itself uses against a real
# untrusted source; mirroring that shape here keeps this file's fetch calls
# consistent with the code it is testing.
_opener = urllib.request.build_opener()


def _get(url: str) -> bytes:
    with _opener.open(url, timeout=5) as response:
        return response.read()


def _status(url: str) -> int:
    try:
        with _opener.open(url, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _child(element: ET.Element, tag: str, ns: dict[str, str]) -> ET.Element:
    found = element.find(tag, ns)
    assert found is not None, f"missing <{tag}>"
    return found


def _text(element: ET.Element, tag: str) -> str:
    """Return the text of repomd child ``tag``, asserting it is present."""
    text = _child(element, tag, _REPO).text
    assert text is not None, f"empty <{tag}>"
    return text


def _attr(element: ET.Element, tag: str, name: str, ns: dict[str, str]) -> str:
    value = _child(element, tag, ns).get(name)
    assert value is not None, f"<{tag}> has no {name}"
    return value


def _repo_url(server: FeedServer, repo: str) -> str:
    return f"{server.url}{server.feed.url_path(repo)}/"


def _primary(server: FeedServer, repo: str) -> tuple[ET.Element, ET.Element, bytes]:
    """Fetch ``repo``'s repomd.xml and primary; return (repomd root, primary <data>, compressed primary)."""
    repomd = ET.fromstring(_get(server.url + server.feed.repomd_path(repo)))
    data = next(d for d in repomd.findall("repo:data", _REPO) if d.get("type") == "primary")
    href = _attr(data, "repo:location", "href", _REPO)
    return repomd, data, _get(_repo_url(server, repo) + href)


def _listed(compressed: bytes) -> list[tuple[str, str, int, str]]:
    """Return (checksum type, checksum, size, href) for every package in a primary."""
    root = ET.fromstring(gzip.decompress(compressed))
    out = []
    for pkg in root.findall("c:package", _COMMON):
        checksum = _child(pkg, "c:checksum", _COMMON)
        assert checksum.text is not None
        out.append(
            (
                _attr(pkg, "c:checksum", "type", _COMMON),
                checksum.text,
                int(_attr(pkg, "c:size", "package", _COMMON)),
                _attr(pkg, "c:location", "href", _COMMON),
            )
        )
    return out


def _verify_repo(server: FeedServer, repo: str) -> list[tuple[str, str, int, str]]:
    """Assert ``repo``'s primary and every package it lists match their declarations."""
    _, data, compressed = _primary(server, repo)
    body = gzip.decompress(compressed)
    assert hashlib.sha256(compressed).hexdigest() == _text(data, "repo:checksum")
    assert hashlib.sha256(body).hexdigest() == _text(data, "repo:open-checksum")
    assert int(_text(data, "repo:size")) == len(compressed)
    assert int(_text(data, "repo:open-size")) == len(body)
    listed = _listed(compressed)
    for ctype, checksum, size, href in listed:
        assert ctype == "sha256"
        payload = _get(urllib.parse.urljoin(_repo_url(server, repo), href))
        assert hashlib.sha256(payload).hexdigest() == checksum
        assert len(payload) == size
    return listed


@pytest.fixture
def feed(tmp_path: Path) -> UpstreamFeed:
    return build_feed(tmp_path, {"sdk/all": 3, "target/m1": 5})


def test_built_repos_verify_over_http(feed: UpstreamFeed, feed_server: Callable[[UpstreamFeed], FeedServer]) -> None:
    server = feed_server(feed)
    assert server.url.startswith("http://127.0.0.1:")
    assert int(server.url.rsplit(":", 1)[1]) > 0

    sdk = _verify_repo(server, "sdk/all")
    target = _verify_repo(server, "target/m1")

    assert len(sdk) == 3
    assert len(target) == 5
    for _, sha, _, href in sdk:
        assert href == f"../../_pkgs/{sha[:2]}/{sha}.rpm"
        assert (feed.channel_dir / "_pkgs" / sha[:2] / f"{sha}.rpm").is_file()


def test_repomd_names_every_metadata_type_by_digest(feed: UpstreamFeed) -> None:
    repodata = feed.channel_dir / "sdk/all/repodata"
    repomd = ET.fromstring((repodata / "repomd.xml").read_bytes())

    assert _text(repomd, "repo:revision") == feed.revision("sdk/all")
    kinds = {}
    for data in repomd.findall("repo:data", _REPO):
        href = _attr(data, "repo:location", "href", _REPO)
        digest = hashlib.sha256((repodata / href.removeprefix("repodata/")).read_bytes()).hexdigest()
        assert href == f"repodata/{digest}-{data.get('type')}.xml.gz"
        assert _text(data, "repo:checksum") == digest
        assert _text(data, "repo:timestamp").isdigit()
        kinds[data.get("type")] = href
    assert set(kinds) == {"primary", "filelists", "other"}


def test_withheld_path_returns_404(feed: UpstreamFeed, feed_server: Callable[[UpstreamFeed], FeedServer]) -> None:
    server = feed_server(feed)
    path = feed.package_path(feed.packages("sdk/all")[0])
    feed.withhold(path)

    assert (feed.root / path.lstrip("/")).is_file()
    assert _status(server.url + path) == 404


def test_fail_once_path_fails_then_succeeds(
    feed: UpstreamFeed, feed_server: Callable[[UpstreamFeed], FeedServer]
) -> None:
    server = feed_server(feed)
    package = feed.packages("target/m1")[1]
    path = feed.package_path(package)
    feed.fail_once(path)

    assert _status(server.url + path) == 503
    assert hashlib.sha256(_get(server.url + path)).hexdigest() == package.sha256


def test_fail_always_and_corrupt_apply_and_clear_while_running(
    feed: UpstreamFeed, feed_server: Callable[[UpstreamFeed], FeedServer]
) -> None:
    server = feed_server(feed)
    first, second = feed.packages("sdk/all")[:2]
    failing, corrupted = feed.package_path(first), feed.package_path(second)
    feed.fail_always(failing)
    feed.corrupt(corrupted)

    assert [_status(server.url + failing) for _ in range(3)] == [503, 503, 503]
    bad = _get(server.url + corrupted)
    assert len(bad) == second.size
    assert hashlib.sha256(bad).hexdigest() != second.sha256

    feed.clear_faults()

    assert hashlib.sha256(_get(server.url + failing)).hexdigest() == first.sha256
    assert hashlib.sha256(_get(server.url + corrupted)).hexdigest() == second.sha256


def test_requests_are_recorded_in_order(feed: UpstreamFeed, feed_server: Callable[[UpstreamFeed], FeedServer]) -> None:
    server = feed_server(feed)
    paths = [
        feed.repomd_path("target/m1"),
        feed.package_path(feed.packages("sdk/all")[2]),
        "/2024/edge/does-not-exist",
        feed.repomd_path("sdk/all"),
    ]
    for path in paths:
        _status(server.url + path)

    assert server.requests == paths
    assert server.package_requests == [paths[1]]


def test_shared_payload_listed_once_in_pool_by_every_named_repo(
    tmp_path: Path, feed_server: Callable[[UpstreamFeed], FeedServer]
) -> None:
    feed = build_feed(tmp_path, FeedSpec(repos={"sdk/all": 1, "target/m1": 2}, shared=("sdk/all", "target/m1")))
    server = feed_server(feed)

    sdk = {checksum for _, checksum, _, _ in _verify_repo(server, "sdk/all")}
    target = {checksum for _, checksum, _, _ in _verify_repo(server, "target/m1")}

    common = sdk & target
    assert len(common) == 1
    sha = common.pop()
    assert list((feed.channel_dir / "_pkgs").rglob(f"{sha}.rpm")) == [feed.channel_dir / f"_pkgs/{sha[:2]}/{sha}.rpm"]


def test_update_changes_revision_and_primary_still_verifies(
    feed: UpstreamFeed, feed_server: Callable[[UpstreamFeed], FeedServer]
) -> None:
    server = feed_server(feed)
    repomd = feed.repomd_path("target/m1")
    before_rev = _text(ET.fromstring(_get(server.url + repomd)), "repo:revision")
    before = {checksum for _, checksum, _, _ in _verify_repo(server, "target/m1")}
    replaced = feed.packages("target/m1")[0]

    fresh = feed.update("target/m1", add=2, replace=replaced.href)

    after_rev = _text(ET.fromstring(_get(server.url + repomd)), "repo:revision")
    after = {checksum for _, checksum, _, _ in _verify_repo(server, "target/m1")}
    assert after_rev != before_rev
    assert len(after) == 7
    assert replaced.sha256 not in after
    assert after - before == {p.sha256 for p in fresh}
    assert len(fresh) == 3


def test_metadata_faults_stay_self_consistent_except_the_fault(
    feed: UpstreamFeed, feed_server: Callable[[UpstreamFeed], FeedServer]
) -> None:
    server = feed_server(feed)
    hostile = "../../../../etc/passwd.rpm"

    feed.replace_href("sdk/all", 0, hostile)
    feed.set_checksum_type("sdk/all", 1, "md5")
    feed.override_open_size("sdk/all", 10)

    _, data, compressed = _primary(server, "sdk/all")
    assert hashlib.sha256(compressed).hexdigest() == _text(data, "repo:checksum")
    assert int(_text(data, "repo:open-size")) == 10
    listed = _listed(compressed)
    assert listed[0][3] == hostile
    assert listed[1][0] == "md5"

    feed.override_open_size("sdk/all", None)
    _, data, compressed = _primary(server, "sdk/all")
    assert int(_text(data, "repo:open-size")) == len(gzip.decompress(compressed))


def test_optional_outputs(tmp_path: Path) -> None:
    feed = build_feed(
        tmp_path,
        FeedSpec(
            repos={"sdk/all": 1, "target/m1": 2},
            targets={"m1": ["sdk/all", "target/m1", "target/m1-ext"]},
            snapshot="20240101T000000Z",
            signed=("target/m1",),
        ),
    )
    channel = feed.channel_dir

    assert json.loads((channel / "targets.json").read_text()) == {"m1": ["sdk/all", "target/m1", "target/m1-ext"]}
    assert json.loads((channel / "target/m1/snapshots-latest.json").read_text())["id"] == "20240101T000000Z"
    snap_primary = next((channel / "snapshots/20240101T000000Z/target/m1/repodata").glob("*-primary.xml.gz"))
    for _, sha, _, href in _listed(snap_primary.read_bytes()):
        assert href == f"../../../../_pkgs/{sha[:2]}/{sha}.rpm"
    signature = channel / "target/m1/repodata/repomd.xml.asc"
    assert signature.is_file()
    assert not (channel / "sdk/all/repodata/repomd.xml.asc").exists()

    feed.set_signed("target/m1", signed=False)
    assert not signature.exists()


def test_context_manager_shuts_down_on_exit(feed: UpstreamFeed) -> None:
    with serve_feed(feed) as server:
        url = server.url + feed.repomd_path("sdk/all")
        assert _status(url) == 200

    with pytest.raises(urllib.error.URLError):
        _get(url)
