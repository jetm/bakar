"""Tests for bakar.release_workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar.release_workspace import (
    ReleaseRoutingError,
    is_valid_delivered_name,
    map_release,
    parse_repo_policy,
    release_key_from_merged,
    sanitize_delivered_name,
    worktree_build_dir_name,
)


@pytest.mark.parametrize(("value", "want"), [(None, "kas"), ("", "kas"), ("kas", "kas"), ("reuse", "reuse")])
def test_parse_repo_policy_known_values(value: str | None, want: str) -> None:
    assert parse_repo_policy(value) == want


@pytest.mark.parametrize("value", ["resue", "REUSE", "none"])
def test_parse_repo_policy_refuses_unknown_values(value: str) -> None:
    with pytest.raises(ReleaseRoutingError, match=repr(value)):
        parse_repo_policy(value)


def _merged(branch: str | None = None, *, series: object = None, **extra: str) -> dict:
    entry: dict = dict(extra)
    if branch is not None:
        entry["branch"] = branch
    cfg: dict = {"repos": {"openembedded-core": entry}}
    if series is not None:
        cfg["env"] = {"AVOCADO_YOCTO_SERIES": series}
    return cfg


class TestReleaseKeyFromMerged:
    def test_wrynose_without_series(self) -> None:
        assert release_key_from_merged(_merged("wrynose-avocado")) == "wrynose"

    def test_wrynose_with_matching_series(self) -> None:
        cfg = _merged("wrynose-avocado", series="wrynose")
        assert release_key_from_merged(cfg) == "wrynose"

    def test_scarthgap_without_series(self) -> None:
        assert release_key_from_merged(_merged("scarthgap-avocado")) == "scarthgap"

    def test_empty_series_is_ignored(self) -> None:
        cfg = _merged("wrynose-avocado", series="")
        assert release_key_from_merged(cfg) == "wrynose"

    def test_mismatch_names_both_values(self) -> None:
        cfg = _merged("wrynose-avocado", series="scarthgap")
        with pytest.raises(ReleaseRoutingError) as exc:
            release_key_from_merged(cfg)
        assert "wrynose" in str(exc.value)
        assert "scarthgap" in str(exc.value)

    def test_commit_only_pin_raises(self) -> None:
        cfg = {"repos": {"openembedded-core": {"commit": "abc"}}}
        with pytest.raises(ReleaseRoutingError, match="openembedded-core"):
            release_key_from_merged(cfg)

    def test_master_branch_raises(self) -> None:
        with pytest.raises(ReleaseRoutingError, match=r"openembedded-core.*master"):
            release_key_from_merged(_merged("master"))

    def test_bare_suffix_raises(self) -> None:
        with pytest.raises(ReleaseRoutingError, match="openembedded-core"):
            release_key_from_merged(_merged("-avocado"))

    @pytest.mark.parametrize(
        "cfg",
        [
            {},
            {"repos": None},
            {"repos": {}},
            {"repos": {"openembedded-core": None}},
            {"repos": {"openembedded-core": {"branch": ""}}},
            {"repos": {"openembedded-core": {"branch": 3}}},
        ],
    )
    def test_missing_entry_or_branch_raises(self, cfg: dict) -> None:
        with pytest.raises(ReleaseRoutingError, match="openembedded-core"):
            release_key_from_merged(cfg)


class TestMapRelease:
    def test_template(self) -> None:
        assert map_release("wrynose", "/ws/peridio-{release}", None) == Path("/ws/peridio-wrynose")

    def test_override_wins_over_template(self) -> None:
        got = map_release("wrynose", "/ws/peridio-{release}", {"wrynose": "/opt/ws-w"})
        assert got == Path("/opt/ws-w")

    def test_override_for_other_release_falls_back_to_template(self) -> None:
        got = map_release("scarthgap", "/ws/peridio-{release}", {"wrynose": "/opt/ws-w"})
        assert got == Path("/ws/peridio-scarthgap")

    def test_override_without_template(self) -> None:
        assert map_release("wrynose", None, {"wrynose": "/opt/ws-w"}) == Path("/opt/ws-w")

    def test_nothing_configured(self) -> None:
        assert map_release("wrynose", None, None) is None
        assert map_release("wrynose", None, {"scarthgap": "/x"}) is None

    def test_tilde_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", "/home/u")
        assert map_release("wrynose", "~/peridio-{release}", None) == Path("/home/u/peridio-wrynose")
        assert map_release("wrynose", None, {"wrynose": "~/w"}) == Path("/home/u/w")


class TestWorktreeBuildDirName:
    def test_default(self) -> None:
        got = worktree_build_dir_name("raspberrypi5", "rpi5-lgpio-feed", None)
        assert got == "build-raspberrypi5-rpi5-lgpio-feed"

    def test_shared_template(self) -> None:
        got = worktree_build_dir_name("raspberrypi5", "rpi5-lgpio-feed", "build-{stem}")
        assert got == "build-raspberrypi5"

    def test_custom_template(self) -> None:
        got = worktree_build_dir_name("raspberrypi5", "wt", "build-x-{worktree}")
        assert got == "build-x-wt"

    def test_main_checkout_ignores_template(self) -> None:
        assert worktree_build_dir_name("raspberrypi5", None, None) == ("build-raspberrypi5")
        assert worktree_build_dir_name("raspberrypi5", None, "build-{machine}") == ("build-raspberrypi5")

    def test_unknown_placeholder_raises_with_default_source(self) -> None:
        with pytest.raises(ReleaseRoutingError) as exc:
            worktree_build_dir_name("s", "w", "build-{machine}")
        assert "build.worktree_build_dir" in str(exc.value)
        assert "{machine}" in str(exc.value)

    def test_unknown_placeholder_raises_with_given_source(self) -> None:
        with pytest.raises(ReleaseRoutingError, match="BAKAR_WORKTREE_BUILD_DIR"):
            worktree_build_dir_name("s", "w", "build-{machine}", source="BAKAR_WORKTREE_BUILD_DIR")


class TestDeliveredName:
    def test_sanitize_slash(self) -> None:
        got = sanitize_delivered_name("seeed-j5012-carrier-bsp/aquantia-wol-hooks")
        assert got == "seeed-j5012-carrier-bsp-aquantia-wol-hooks"

    def test_sanitize_strips_edges(self) -> None:
        assert sanitize_delivered_name("..-feat x-.") == "feat-x"

    def test_sanitize_keeps_valid(self) -> None:
        assert sanitize_delivered_name("rpi5_lgpio.feed-1") == "rpi5_lgpio.feed-1"

    @pytest.mark.parametrize("raw", ["", "///", "..", "-.-"])
    def test_sanitize_empty_raises(self, raw: str) -> None:
        with pytest.raises(ReleaseRoutingError):
            sanitize_delivered_name(raw)

    @pytest.mark.parametrize("name", ["a", "rpi5-lgpio-feed", "A.b_c-9"])
    def test_valid(self, name: str) -> None:
        assert is_valid_delivered_name(name)

    @pytest.mark.parametrize("name", ["", "a/b", "a b", "a;rm", "x\n"])
    def test_invalid(self, name: str) -> None:
        assert not is_valid_delivered_name(name)
