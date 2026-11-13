"""Pin comparison between the kas-declared repos and a host description."""

from __future__ import annotations

from bakar.feed_describe import LayerCommit
from bakar.feed_pins import Refusal, compare_pins, describe_refusal

A = "a" * 40
B = "b" * 40
C = "c" * 40


def _obs(*pairs: tuple[str, str | None]) -> tuple[LayerCommit, ...]:
    return tuple(LayerCommit(repo=r, commit=c) for r, c in pairs)


def _verdicts(report) -> dict[str, str]:
    return {v.repo: v.verdict for v in report.verdicts}


def test_full_commit_matches() -> None:
    report = compare_pins({"poky": {"commit": A}}, _obs(("poky", A)))
    assert _verdicts(report) == {"poky": "match"}
    assert report.refusals == ()


def test_seven_char_prefix_matches() -> None:
    report = compare_pins({"poky": {"commit": A[:7]}}, _obs(("poky", A)))
    assert _verdicts(report) == {"poky": "match"}


def test_twelve_char_prefix_matches() -> None:
    report = compare_pins({"poky": {"commit": B[:12]}}, _obs(("poky", B)))
    assert _verdicts(report) == {"poky": "match"}


def test_non_prefix_differs() -> None:
    report = compare_pins({"poky": {"commit": "abcdef1"}}, _obs(("poky", B)))
    assert _verdicts(report) == {"poky": "differs"}
    assert report.refusals == (Refusal(repo="poky", declared="abcdef1", observed=B, verdict="differs"),)


def test_short_value_under_seven_chars_is_not_a_prefix_match() -> None:
    report = compare_pins({"poky": {"commit": A[:6]}}, _obs(("poky", A)))
    assert _verdicts(report) == {"poky": "differs"}


def test_prefix_is_matched_from_the_start_not_the_end() -> None:
    observed = "0123456789abcdef0123456789abcdef01234567"
    assert _verdicts(compare_pins({"poky": {"commit": observed[:9]}}, _obs(("poky", observed)))) == {"poky": "match"}
    assert _verdicts(compare_pins({"poky": {"commit": observed[-9:]}}, _obs(("poky", observed)))) == {"poky": "differs"}


def test_different_full_commit_differs() -> None:
    report = compare_pins({"poky": {"commit": A}}, _obs(("poky", B)))
    assert _verdicts(report) == {"poky": "differs"}
    assert report.refusals[0].observed == B


def test_missing_when_absent_from_description() -> None:
    report = compare_pins({"poky": {"commit": A}}, _obs(("other", B)))
    assert _verdicts(report) == {"poky": "missing"}
    assert report.refusals == (Refusal(repo="poky", declared=A, observed=None, verdict="missing"),)


def test_missing_when_observed_commit_is_null() -> None:
    report = compare_pins({"poky": {"commit": A}}, _obs(("poky", None)))
    assert _verdicts(report) == {"poky": "missing"}
    assert report.refusals[0].observed is None


def test_extra_observed_repo_is_ignored() -> None:
    report = compare_pins({"poky": {"commit": A}}, _obs(("poky", A), ("extra", B)))
    assert _verdicts(report) == {"poky": "match"}
    assert report.refusals == ()
    assert report.observed == {"poky": A, "extra": B}


def test_observed_keeps_nulls() -> None:
    report = compare_pins({}, _obs(("poky", None), ("x", A)))
    assert report.observed == {"poky": None, "x": A}


def test_meta_avocado_is_delivered_not_refused() -> None:
    declared = {"meta-avocado": {"commit": A}}
    report = compare_pins(declared, _obs(("meta-avocado", B)))
    assert report.delivered == B
    assert report.refusals == ()
    assert "meta-avocado" not in _verdicts(report)


def test_meta_avocado_missing_is_not_refused() -> None:
    report = compare_pins({"meta-avocado": {"commit": A}}, ())
    assert report.delivered is None
    assert report.refusals == ()
    assert report.verdicts == ()


def test_delivered_without_declaration() -> None:
    report = compare_pins({}, _obs(("meta-avocado", C)))
    assert report.delivered == C


def test_declared_without_commit_is_ignored() -> None:
    declared = {"a": {"branch": "main"}, "b": {"commit": None}, "c": {"commit": ""}}
    report = compare_pins(declared, ())
    assert report.verdicts == ()
    assert report.refusals == ()


def test_non_dict_declared_values_are_ignored() -> None:
    report = compare_pins({"a": None, "b": "deadbeef", "c": ["x"]}, ())
    assert report.verdicts == ()


def test_basename_of_path_matches_when_key_absent() -> None:
    declared = {"meta-vendor": {"commit": A, "path": "layers/meta-vendor-real"}}
    report = compare_pins(declared, _obs(("meta-vendor-real", A)))
    assert _verdicts(report) == {"meta-vendor": "match"}


def test_basename_of_path_reports_differs() -> None:
    declared = {"k": {"commit": A, "path": "sources/layer-x/"}}
    report = compare_pins(declared, _obs(("layer-x", B)))
    assert _verdicts(report) == {"k": "differs"}
    assert report.refusals[0].observed == B


def test_key_wins_over_path_basename() -> None:
    declared = {"k": {"commit": A, "path": "p/other"}}
    report = compare_pins(declared, _obs(("k", A), ("other", B)))
    assert _verdicts(report) == {"k": "match"}


def test_uppercase_hex_compares_equal() -> None:
    report = compare_pins({"poky": {"commit": A.upper()}}, _obs(("poky", A)))
    assert _verdicts(report) == {"poky": "match"}
    report = compare_pins({"poky": {"commit": A[:9].upper()}}, _obs(("poky", A)))
    assert _verdicts(report) == {"poky": "match"}


def test_verdicts_sorted_by_name() -> None:
    declared = {"zeta": {"commit": A}, "alpha": {"commit": B}, "mid": {"commit": C}}
    report = compare_pins(declared, _obs(("zeta", A), ("alpha", B), ("mid", C)))
    assert [v.repo for v in report.verdicts] == ["alpha", "mid", "zeta"]
    assert report.verdicts[0].declared == B
    assert report.verdicts[0].observed == B


def test_refusal_text_names_repo_pin_and_commit() -> None:
    declared = {"poky": {"commit": A}, "oe": {"commit": B}}
    report = compare_pins(declared, _obs(("poky", C)))
    text = describe_refusal(report)
    assert "\n" not in text
    assert f"poky declared {A} but the host built {C}" in text
    assert f"oe declared {B} but the host reported no evidence" in text
    assert text.index("oe ") < text.index("poky ")


def test_refusal_text_empty_without_refusals() -> None:
    report = compare_pins({"poky": {"commit": A}}, _obs(("poky", A)))
    assert describe_refusal(report) == ""
