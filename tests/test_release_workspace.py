"""Tests for bakar.release_workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from bakar.release_workspace import (
    ReleaseRoutingError,
    check_template,
    is_valid_delivered_name,
    map_release,
    matches_release_workspace,
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


@pytest.mark.parametrize(
    "template",
    ["build-{stem:>8}", "build-{stem!r}", "build-{stem.x}", "build-{stem[0]}", "build-{}", "build-{stem", "{machine}"],
)
def test_check_template_refuses_what_the_runtime_cannot_render(template: str) -> None:
    with pytest.raises(ReleaseRoutingError, match=r"template|may only use|format spec"):
        check_template("build.worktree_build_dir", template, frozenset({"stem", "worktree"}))


def test_check_template_accepts_plain_placeholders_and_escaped_braces() -> None:
    check_template("t", "b-{stem}-{worktree}", frozenset({"stem", "worktree"}))
    check_template("t", "literal-only", frozenset({"stem"}))
    check_template("t", "b{{x}}-{stem}", frozenset({"stem"}))


@pytest.mark.parametrize("template", ["build-{stem:>8}", "build-{stem", "{machine}"])
def test_runtime_and_loader_agree_on_a_refused_build_dir_template(template: str) -> None:
    with pytest.raises(ReleaseRoutingError):
        worktree_build_dir_name("m", "wt", template)


@pytest.mark.parametrize("template", ["../{worktree}", "/tmp/{stem}", "builds/{stem}-{worktree}", "{stem}/{worktree}"])
def test_build_dir_must_render_one_directory_name(template: str) -> None:
    with pytest.raises(ReleaseRoutingError, match="single directory name"):
        worktree_build_dir_name("m", "wt", template)


@pytest.mark.parametrize("worktree", ["", ".", ".."])
def test_build_dir_refuses_an_empty_or_dot_rendering(worktree: str) -> None:
    with pytest.raises(ReleaseRoutingError, match="single directory name"):
        worktree_build_dir_name("m", worktree, "{worktree}")


def test_map_release_results_are_absolute() -> None:
    assert map_release("wrynose", "/w/peridio-{release}", None) == Path("/w/peridio-wrynose")
    assert map_release("wrynose", None, {"wrynose": "/w/x"}) == Path("/w/x")


@pytest.mark.parametrize("releases", [{"wrynose": ""}, {"wrynose": "relative/ws"}])
def test_map_release_refuses_an_empty_or_relative_entry(releases: dict[str, str]) -> None:
    with pytest.raises(ReleaseRoutingError, match="absolute"):
        map_release("wrynose", None, releases)


def test_map_release_refuses_a_relative_template_and_a_format_spec() -> None:
    with pytest.raises(ReleaseRoutingError, match="absolute"):
        map_release("wrynose", "ws-{release}", None)
    with pytest.raises(ReleaseRoutingError, match="format spec"):
        map_release("wrynose", "/w/{release!r}", None)


@pytest.mark.parametrize("branch", ["feature/wrynose-avocado", "../x-avocado"])
def test_release_key_must_be_a_directory_name(branch: str) -> None:
    with pytest.raises(ReleaseRoutingError, match="directory name"):
        release_key_from_merged(_merged(branch))


def test_matches_release_workspace_by_entry_template_and_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real-wrynose"
    real.mkdir()
    link = tmp_path / "peridio-wrynose"
    link.symlink_to(real)
    template = str(tmp_path / "peridio-{release}")
    assert matches_release_workspace(tmp_path / "peridio-scarthgap", template, None)
    assert matches_release_workspace(link, None, {"wrynose": str(real)})
    assert not matches_release_workspace(tmp_path / "peridio-wrynose" / "sub", template, None)
    assert not matches_release_workspace(tmp_path / "other", template, {"wrynose": str(real)})
    assert not matches_release_workspace(tmp_path / "peridio-wrynose", None, None)


@pytest.mark.parametrize("raw", ["a..b", "feat.lock", "x.build", "..b.lock"])
def test_sanitize_refuses_names_git_or_the_lock_files_cannot_hold(raw: str) -> None:
    with pytest.raises(ReleaseRoutingError):
        sanitize_delivered_name(raw)


@pytest.mark.parametrize("name", ["a..b", "feat.lock", "x.build", "ok.name-1", "wt1"])
def test_is_valid_delivered_name_matches_sanitize(name: str) -> None:
    refused = False
    try:
        sanitize_delivered_name(name)
    except ReleaseRoutingError:
        refused = True
    assert is_valid_delivered_name(name) is not refused


def test_a_delivered_name_is_a_valid_git_ref_component(tmp_path: Path) -> None:
    import subprocess

    for raw in ["feature/x y", "wt 1", "a.b", "UPPER_case-1"]:
        name = sanitize_delivered_name(raw)
        proc = subprocess.run(
            ["git", "check-ref-format", f"refs/bakar/{name}/0123456789ab"], capture_output=True, check=False
        )
        assert proc.returncode == 0, name
