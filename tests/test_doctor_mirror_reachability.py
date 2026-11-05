"""Mirror reachability doctor checks: sstate-mirror-reachable and dl-mirror-reachable."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bakar import diagnostics
from bakar.config import BuildConfig
from bakar.diagnostics import (
    CHECK_GROUPS,
    Severity,
    Status,
    check_dl_mirror_reachable,
    check_sstate_mirror_reachable,
    run_all,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_CASES: tuple[tuple[Callable[[BuildConfig], diagnostics.CheckResult], str, str, str], ...] = (
    (check_sstate_mirror_reachable, "sstate-mirror-reachable", "sstate_mirror_url", "sstate-mirror.md"),
    (check_dl_mirror_reachable, "dl-mirror-reachable", "dl_mirror_url", "dl-mirror.md"),
)


def _cfg(**over: object) -> BuildConfig:
    base: dict[str, object] = {
        "workspace": Path("/tmp"),
        "bsp_family": "nxp",
        "machine": "m",
        "distro": "d",
        "image": "i",
        "manifest": "x.xml",
        "repo_url": "https://example.com",
        "repo_branch": "main",
        "kas_container_image": "img:latest",
    }
    base.update(over)
    return BuildConfig(**base)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize(("check", "name", "field", "doc"), _CASES)
def test_reachable_mirror_passes_naming_the_url(
    monkeypatch: pytest.MonkeyPatch, check: Callable[..., object], name: str, field: str, doc: str
) -> None:
    seen: list[tuple[str, float]] = []
    monkeypatch.setattr(
        diagnostics.probes, "http_reachable", lambda url, timeout=3.0: seen.append((url, timeout)) or True
    )
    result = check(_cfg(**{field: "http://mirror.example/x"}))
    assert result.name == name
    assert result.status is Status.PASS
    assert "http://mirror.example/x" in result.message
    assert seen == [("http://mirror.example/x", 3.0)]


@pytest.mark.unit
@pytest.mark.parametrize(("check", "name", "field", "doc"), _CASES)
def test_unreachable_mirror_warns_with_doc_hint(
    monkeypatch: pytest.MonkeyPatch, check: Callable[..., object], name: str, field: str, doc: str
) -> None:
    monkeypatch.setattr(diagnostics.probes, "http_reachable", lambda url, timeout=3.0: False)
    result = check(_cfg(**{field: "http://mirror.example/x"}))
    assert result.status is Status.FAIL
    assert result.severity is Severity.WARN
    assert "http://mirror.example/x" in result.message
    assert result.fix_hint == f"docs/features/{doc}#what-your-host-needs"


@pytest.mark.unit
def test_dl_mirror_warning_mentions_upstream_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics.probes, "http_reachable", lambda url, timeout=3.0: False)
    result = check_dl_mirror_reachable(_cfg(dl_mirror_url="http://m/"))
    assert "upstream" in result.message


@pytest.mark.unit
@pytest.mark.parametrize(("check", "name", "field", "doc"), _CASES)
def test_unset_url_skips_without_probing(
    monkeypatch: pytest.MonkeyPatch, check: Callable[..., object], name: str, field: str, doc: str
) -> None:
    def boom(*_a: object, **_k: object) -> bool:
        raise AssertionError("probe must not run with no URL configured")

    monkeypatch.setattr(diagnostics.probes, "http_reachable", boom)
    for empty in ("", None):
        result = check(_cfg(**{field: empty}))
        assert result.name == name
        assert result.status is Status.SKIP


@pytest.mark.unit
def test_raising_probe_in_run_all_reports_registered_names(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> bool:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(diagnostics.probes, "http_reachable", boom)
    cfg = _cfg(sstate_mirror_url="http://s/", dl_mirror_url="http://d/")
    results = run_all(cfg, only={"sstate-mirror-reachable", "dl-mirror-reachable"})
    assert sorted(r.name for r in results) == ["dl-mirror-reachable", "sstate-mirror-reachable"]
    assert all(r.status is Status.FAIL and r.severity is Severity.WARN for r in results)


@pytest.mark.unit
def test_both_checks_are_in_caches_and_storage_group() -> None:
    groups = dict(CHECK_GROUPS)
    assert "sstate-mirror-reachable" in groups["Caches & storage"]
    assert "dl-mirror-reachable" in groups["Caches & storage"]


@pytest.mark.unit
@pytest.mark.parametrize(("check", "name", "field", "doc"), _CASES)
@pytest.mark.parametrize("reachable", [True, False])
def test_mirror_userinfo_never_reaches_the_message(  # noqa: PLR0913 - stacked parametrize axes
    monkeypatch: pytest.MonkeyPatch,
    check: Callable[..., object],
    name: str,
    field: str,
    doc: str,
    reachable: bool,
) -> None:
    monkeypatch.setattr(diagnostics.probes, "http_reachable", lambda url, timeout=3.0: reachable)
    result = check(_cfg(**{field: "https://user:s3cret@mirror.example/x"}))
    assert "s3cret" not in result.message
    assert "user:" not in result.message
    assert "mirror.example/x" in result.message


@pytest.mark.unit
@pytest.mark.parametrize(("check", "name", "field", "doc"), _CASES)
def test_file_mirror_is_not_probed_and_not_a_warning(
    monkeypatch: pytest.MonkeyPatch, check: Callable[..., object], name: str, field: str, doc: str
) -> None:
    def boom(*_a: object, **_k: object) -> bool:
        raise AssertionError("a file:// mirror has no server to probe")

    monkeypatch.setattr(diagnostics.probes, "http_reachable", boom)
    result = check(_cfg(**{field: "file:///mnt/nfs/sstate"}))
    assert result.status is Status.PASS
    assert "not probed" in result.message
