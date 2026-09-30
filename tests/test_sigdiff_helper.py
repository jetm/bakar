"""Tests for the standalone sigdiff_helper (runs under bitbake's library)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bakar import sigdiff_helper as h

pytestmark = pytest.mark.unit

H1 = "a" * 40
H2 = "b" * 40
H3 = "c" * 40


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    return path


def _roots(tmp_path: Path) -> dict:
    return {
        "stamps": [str(tmp_path / "stamps")],
        "ledger": str(tmp_path / "ledger"),
        "sstate": [str(tmp_path / "sstate")],
    }


def _stub(calls: list):
    def compare(a, b, recursecb=None, color=False, collapsed=False):
        calls.append((a, b))
        return [f"cmp {Path(a).name} {Path(b).name}"]

    return compare


def test_validation() -> None:
    assert h.valid_pn("gcc-cross-x86_64+1.2")
    assert not h.valid_pn("..")
    assert not h.valid_pn("a..b")
    assert not h.valid_pn("-x")
    assert not h.valid_pn("a/b")
    assert h.valid_task("do_compile")
    assert not h.valid_task("compile")
    assert not h.valid_task("do_x/../y")
    assert h.valid_hash(H1)
    assert not h.valid_hash("A" * 40)
    assert not h.valid_hash("a" * 39)


def test_split_key() -> None:
    assert h.split_key("zlib:do_compile") == ("zlib", "do_compile")
    assert h.split_key("mc:foo:zlib:do_compile") == ("zlib", "do_compile")
    assert h.split_key("nocolon") is None


def test_lookup_order_stamps_first(tmp_path: Path) -> None:
    stamp = _touch(tmp_path / "stamps" / "arch" / "zlib" / f"1.0-r0.do_compile.sigdata.{H1}")
    _touch(tmp_path / "ledger" / "zlib" / f"do_compile.{H1}.sigdata")
    lk = h.SigLookup(_roots(tmp_path), _stub([]))
    assert lk.find("zlib", "do_compile", H1) == str(stamp)


def test_lookup_ledger_then_sstate(tmp_path: Path) -> None:
    ledger = _touch(tmp_path / "ledger" / "zlib" / f"do_compile.{H1}.sigdata")
    sstate = _touch(
        tmp_path / "sstate" / H2[0:2] / H2[2:4] / f"sstate:zlib:core2:1.0:r0:x:14:{H2}_compile.tar.zst.siginfo"
    )
    lk = h.SigLookup(_roots(tmp_path), _stub([]))
    assert lk.find("zlib", "do_compile", H1) == str(ledger)
    assert lk.find("zlib", "do_compile", H2) == str(sstate)
    assert lk.find("zlib", "do_compile", H3) is None


def test_invalid_values_not_found(tmp_path: Path) -> None:
    _touch(tmp_path / "ledger" / "zlib" / f"do_compile.{H1}.sigdata")
    lk = h.SigLookup(_roots(tmp_path), _stub([]))
    assert lk.find("../zlib", "do_compile", H1) is None
    assert lk.find("zlib", "compile", H1) is None
    assert lk.find("zlib", "do_compile", "*") is None


def test_traversal_key_opens_nothing_outside_roots(tmp_path: Path) -> None:
    (tmp_path / "root").mkdir()
    _touch(tmp_path / "ledger" / "zlib" / f"do_compile.{H1}.sigdata")
    # A file that a traversal key would reach if pn were not validated.
    _touch(tmp_path / "root" / f"do_x.{H1}.sigdata")
    calls: list = []
    lk = h.SigLookup(_roots(tmp_path), _stub(calls))
    out = lk.recurse("../../root:do_x", H1, H2)
    assert out == [f"Unable to find matching sigdata for ../../root:do_x with hashes {H1} or {H2}"]
    assert calls == []


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    outside = _touch(tmp_path / "outside" / "real.sigdata")
    ledger_pn = tmp_path / "ledger" / "zlib"
    ledger_pn.mkdir(parents=True)
    (ledger_pn / f"do_compile.{H1}.sigdata").symlink_to(outside)
    lk = h.SigLookup(_roots(tmp_path), _stub([]))
    assert lk.find("zlib", "do_compile", H1) is None


def test_confinement_is_per_component(tmp_path: Path) -> None:
    sibling = _touch(tmp_path / "ledger-evil" / "x")
    lk = h.SigLookup(_roots(tmp_path), _stub([]))
    assert not lk.confined(str(sibling))
    assert not lk.confined(str(tmp_path / "ledger" / ".." / "ledger-evil" / "x"))
    assert lk.confined(str(tmp_path / "ledger" / "a"))


def test_not_found_lines(tmp_path: Path) -> None:
    _touch(tmp_path / "ledger" / "zlib" / f"do_compile.{H1}.sigdata")
    lk = h.SigLookup(_roots(tmp_path), _stub([]))
    assert lk.recurse("zlib:do_compile", H2, H3) == [
        f"Unable to find matching sigdata for zlib:do_compile with hashes {H2} or {H3}"
    ]
    assert lk.recurse("zlib:do_compile", H2, H1) == [
        f"Unable to find matching sigdata for zlib:do_compile with hash {H2}"
    ]
    assert lk.recurse("zlib:do_compile", H1, H2) == [
        f"Unable to find matching sigdata for zlib:do_compile with hash {H2}"
    ]


def test_recursion_indents_and_memoizes(tmp_path: Path) -> None:
    _touch(tmp_path / "ledger" / "zlib" / f"do_compile.{H1}.sigdata")
    _touch(tmp_path / "ledger" / "zlib" / f"do_compile.{H2}.sigdata")
    calls: list = []
    lk = h.SigLookup(_roots(tmp_path), _stub(calls))
    first = lk.recurse("zlib:do_compile", H1, H2)
    second = lk.recurse("zlib:do_compile", H1, H2)
    assert first == second
    assert first == [f"    cmp do_compile.{H1}.sigdata do_compile.{H2}.sigdata"]
    assert len(calls) == 1
    lk.recurse("mc:x:zlib:do_compile", H1, H2)
    assert len(calls) == 2


def test_run_request_confines_inputs_and_collects_errors(tmp_path: Path) -> None:
    old = _touch(tmp_path / "ledger" / "zlib" / f"do_compile.{H1}.sigdata")
    new = _touch(tmp_path / "ledger" / "zlib" / f"do_compile.{H2}.sigdata")
    outside = _touch(tmp_path / "elsewhere" / "f")
    calls: list = []
    req = {
        "roots": _roots(tmp_path),
        "comparisons": [
            {"recipe": "zlib", "task": "do_compile", "old": str(old), "new": str(new)},
            {"recipe": "evil", "task": "do_compile", "old": str(outside), "new": str(new)},
        ],
    }
    resp = h.run_request(req, _stub(calls))
    assert [r["recipe"] for r in resp["results"]] == ["zlib"]
    assert resp["errors"] == [{"recipe": "evil", "task": "do_compile", "reason": "path outside request roots"}]
    assert len(calls) == 1


def test_run_request_compare_exception_is_error(tmp_path: Path) -> None:
    old = _touch(tmp_path / "ledger" / "a")
    new = _touch(tmp_path / "ledger" / "b")

    def boom(*a, **k):
        raise OSError("nope")

    resp = h.run_request(
        {"roots": _roots(tmp_path), "comparisons": [{"recipe": "r", "task": "t", "old": str(old), "new": str(new)}]},
        boom,
    )
    assert resp["results"] == []
    assert resp["errors"][0]["reason"] == "OSError: nope"


def test_run_request_malformed() -> None:
    with pytest.raises(ValueError):
        h.run_request([], _stub([]))
    with pytest.raises(ValueError):
        h.run_request({"roots": {}}, _stub([]))


def test_main_unreadable_request_exits_2(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, h.__file__],
        input="not json",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 2
    assert "unreadable request" in proc.stderr


def test_main_without_bb_exits_2() -> None:
    proc = subprocess.run(
        [sys.executable, "-I", h.__file__],
        input=json.dumps({"roots": {}, "comparisons": []}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 2
    assert "bb.siggen" in proc.stderr


def _sigdata(runtaskhashes: dict, taskhash: str, varval: str) -> dict:
    return {
        "task": "do_compile",
        "basehash_ignore_vars": {"_set_object": []},
        "taskhash_ignore_tasks": {"_set_object": []},
        "taskdeps": ["FOO"],
        "basehash": "0" * 40,
        "gendeps": {"FOO": {"_set_object": []}},
        "varvals": {"do_compile": "echo ${FOO}", "FOO": varval},
        "runtaskdeps": sorted(runtaskhashes),
        "file_checksum_values": [],
        "runtaskhashes": runtaskhashes,
        "taskhash": taskhash,
        "unihash": taskhash,
    }


def _write_zstd_json(path: Path, data: dict) -> None:
    from compression import zstd

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zstd.compress(json.dumps(data).encode()))


@pytest.mark.skipif(not os.environ.get("BAKAR_TEST_BITBAKE_LIB"), reason="BAKAR_TEST_BITBAKE_LIB unset")
def test_end_to_end_against_real_bitbake(tmp_path: Path) -> None:
    dep1, dep2 = "1" * 40, "2" * 40
    top1, top2 = "3" * 40, "4" * 40
    ledger = tmp_path / "ledger"
    _write_zstd_json(ledger / "dep" / f"do_compile.{dep1}.sigdata", _sigdata({}, dep1, "one"))
    _write_zstd_json(ledger / "dep" / f"do_compile.{dep2}.sigdata", _sigdata({}, dep2, "two"))
    old = ledger / "top" / f"do_compile.{top1}.sigdata"
    new = ledger / "top" / f"do_compile.{top2}.sigdata"
    _write_zstd_json(old, _sigdata({"dep:do_compile": dep1}, top1, "same"))
    _write_zstd_json(new, _sigdata({"dep:do_compile": dep2}, top2, "same"))
    g1 = ledger / "lone" / f"do_compile.{'7' * 40}.sigdata"
    g2 = ledger / "lone" / f"do_compile.{'8' * 40}.sigdata"
    _write_zstd_json(g1, _sigdata({"gone:do_compile": "5" * 40}, "7" * 40, "same"))
    _write_zstd_json(g2, _sigdata({"gone:do_compile": "6" * 40}, "8" * 40, "same"))
    req = {
        "roots": {"stamps": [str(tmp_path / "stamps")], "ledger": str(ledger), "sstate": []},
        "comparisons": [
            {"recipe": "top", "task": "do_compile", "old": str(old), "new": str(new)},
            {"recipe": "lone", "task": "do_compile", "old": str(g1), "new": str(g2)},
        ],
    }
    env = dict(os.environ, PYTHONPATH=os.environ["BAKAR_TEST_BITBAKE_LIB"])
    out_file = tmp_path / "out.json"
    err_file = tmp_path / "err.txt"
    with out_file.open("w") as out, err_file.open("w") as err:
        proc = subprocess.run(
            [sys.executable, h.__file__],
            input=json.dumps(req),
            stdout=out,
            stderr=err,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
    assert proc.returncode == 0, err_file.read_text()
    resp = json.loads(out_file.read_text())
    assert resp["errors"] == []
    text = "\n".join(resp["results"][0]["lines"])
    assert "dep:do_compile" in text
    assert "    " in text and "FOO" in text
    lone = "\n".join(resp["results"][1]["lines"])
    assert f"Unable to find matching sigdata for gone:do_compile with hashes {'5' * 40} or {'6' * 40}" in lone


def _symlinked_siginfo(tmp_path: Path, target_dir: Path) -> tuple[Path, Path]:
    """A sstate siginfo symlink into target_dir, plus a regular ledger file to compare it with."""
    target = _touch(target_dir / f"sstate:zlib:x:{H1}_populate_sysroot.tar.zst.siginfo")
    link = tmp_path / "sstate" / "universal" / "aa" / "aa" / target.name
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    new = _touch(tmp_path / "ledger" / "zlib" / f"do_populate_sysroot.{H2}.sigdata")
    return link, new


def _one_comparison(roots: dict, old: Path, new: Path) -> dict:
    req = {"roots": roots, "comparisons": [{"recipe": "zlib", "task": "do_x", "old": str(old), "new": str(new)}]}
    return h.run_request(req, _stub([]))


def test_symlink_into_also_allowed_dir_is_accepted(tmp_path: Path) -> None:
    seed = tmp_path / "seeds" / "scarthgap"
    link, new = _symlinked_siginfo(tmp_path, seed)
    roots = {**_roots(tmp_path), "also_allowed": [str(seed)]}
    resp = _one_comparison(roots, link, new)
    assert resp["errors"] == []
    assert [r["recipe"] for r in resp["results"]] == ["zlib"]


def test_symlink_into_unlisted_dir_is_rejected(tmp_path: Path) -> None:
    seed = tmp_path / "seeds" / "scarthgap"
    link, new = _symlinked_siginfo(tmp_path, seed)
    for roots in (_roots(tmp_path), {**_roots(tmp_path), "also_allowed": [str(tmp_path / "other")]}):
        resp = _one_comparison(roots, link, new)
        assert resp["results"] == []
        assert resp["errors"][0]["reason"] == "path outside request roots"


def test_also_allowed_traversal_is_rejected(tmp_path: Path) -> None:
    seed = tmp_path / "seeds" / "scarthgap"
    seed.mkdir(parents=True)
    _touch(tmp_path / "elsewhere" / "secret")
    new = _touch(tmp_path / "ledger" / "zlib" / f"do_x.{H2}.sigdata")
    roots = {**_roots(tmp_path), "also_allowed": [str(seed)]}
    escapes = (
        seed / ".." / ".." / ".." / "elsewhere" / "secret",
        tmp_path / "sstate" / "universal" / ".." / ".." / "elsewhere" / "secret",
    )
    for escape in escapes:
        resp = _one_comparison(roots, escape, new)
        assert resp["results"] == []
        assert resp["errors"][0]["reason"] == "path outside request roots", escape


def test_also_allowed_is_not_searched_by_hash(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    _touch(seed / "ab" / "cd" / f"sstate:zlib:x:{H1}_x.tar.zst.siginfo")
    lk = h.SigLookup({**_roots(tmp_path), "also_allowed": [str(seed)]}, _stub([]))
    assert lk.find("zlib", "do_x", H1) is None
