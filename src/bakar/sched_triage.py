"""Parse sccache-dist scheduler + client logs into a triage report.

The cluster's idleness and misrouting are diagnosable from two log streams the
R0 instrumentation (sccache fork) already emits:

- the **scheduler journal** (``journalctl -u sccache-scheduler``): one
  ``dist-status poll`` line per poll (cluster in-progress vs capacity =
  saturation) and one ``dist-alloc`` line per job (chosen server + every
  candidate's load = misroute signal).
- the **client log** (``SCCACHE_ERROR_LOG``): one ``dist-job done`` line per
  distributed compile (per-phase ms timers), ``Compiling locally (not
  eligible ...)`` for configure conftests, ``falling back to local: <reason>``
  for gate-full/error fallbacks, and - under ``sccache::compiler=debug`` - the
  remote compiler's own stderr, including rustc ``error[E0xxx]`` diagnostics
  that pin why a Rust compile failed remotely.

All parsers are pure (take an iterable of lines, return a dataclass) so they
unit-test without a live cluster. No ``bb`` or sccache import.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from statistics import mean
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

# --- scheduler journal ------------------------------------------------------

# dist-alloc: job 87379 -> ServerId(10.42.0.2:10501) (now 5/32 jobs, load 0.156);
#   candidates [(ServerId(192.168.8.172:10501), 0.15625, 5, 32), (ServerId(10.42.0.2:10501), 0.125, 4, 32)]
_ALLOC_CHOSEN_RE = re.compile(
    r"dist-alloc: job \d+ -> ServerId\((?P<addr>[\d.]+:\d+)\) \(now \d+/\d+ jobs, load (?P<load>[\d.]+)\)"
)
_ALLOC_CAND_RE = re.compile(r"ServerId\((?P<addr>[\d.]+:\d+)\), (?P<load>[\d.]+), (?P<jobs>\d+), (?P<cores>\d+)")

# dist-status poll: 11 in-progress jobs; servers [("192.168.8.172:10501", 5, 32), ("10.42.0.2:10501", 6, 32)]
_STATUS_RE = re.compile(r"dist-status poll: (?P<inflight>\d+) in-progress jobs; servers \[(?P<servers>.*)\]")
_STATUS_SERVER_RE = re.compile(r'"(?P<addr>[\d.]+:\d+)", (?P<jobs>\d+), (?P<cores>\d+)')


@dataclass
class AllocStats:
    """Routing decisions parsed from ``dist-alloc`` lines."""

    total: int = 0
    # An alloc where a strictly-less-loaded candidate existed but was not chosen.
    misroutes: int = 0
    # A misroute where the skipped, less-loaded candidate had zero jobs (idle).
    idle_skips: int = 0
    per_node_chosen: Counter = field(default_factory=Counter)

    @property
    def misroute_pct(self) -> float:
        return 100.0 * self.misroutes / self.total if self.total else 0.0


@dataclass
class SaturationStats:
    """Cluster in-flight utilisation parsed from ``dist-status poll`` lines."""

    samples: int = 0
    ceiling: int = 0  # total cores across servers (last poll)
    mean_inflight: float = 0.0
    mean_util_pct: float = 0.0
    idle_pct: float = 0.0  # share of polls with 0 in-progress
    under_eighth_pct: float = 0.0  # share of polls below 1/8 of ceiling
    near_sat_pct: float = 0.0  # share of polls at >= 7/8 of ceiling


def parse_dist_alloc(lines: Iterable[str]) -> AllocStats:
    """Aggregate routing quality from the scheduler's ``dist-alloc`` lines.

    A *misroute* is an allocation whose chosen server was not the least-loaded
    candidate; an *idle skip* is a misroute where the cheaper candidate had zero
    jobs. Lines without a parseable chosen server are skipped.
    """
    stats = AllocStats()
    for line in lines:
        m = _ALLOC_CHOSEN_RE.search(line)
        if m is None:
            continue
        chosen_addr = m.group("addr")
        # Candidate loads are the PRE-assignment snapshot; the load in the
        # "-> ServerId(x) (now j/c jobs, load L)" clause is POST-assignment (the
        # just-placed job already counted). Comparing against the post-load would
        # flag the chosen server's own lower pre-load as "cheaper" and mark every
        # alloc a misroute, so the baseline must be the chosen server's PRE-load
        # from the candidate list.
        cands = {c.group("addr"): (float(c.group("load")), int(c.group("jobs"))) for c in _ALLOC_CAND_RE.finditer(line)}
        if chosen_addr not in cands:
            continue
        stats.total += 1
        stats.per_node_chosen[chosen_addr] += 1
        chosen_load = cands[chosen_addr][0]
        cheaper = [
            (addr, load, jobs) for addr, (load, jobs) in cands.items() if addr != chosen_addr and load < chosen_load
        ]
        if cheaper:
            stats.misroutes += 1
            if any(jobs == 0 for _addr, _load, jobs in cheaper):
                stats.idle_skips += 1
    return stats


def parse_dist_status(lines: Iterable[str]) -> SaturationStats:
    """Aggregate cluster utilisation from the scheduler's ``dist-status poll`` lines.

    ``ceiling`` is the summed core count across servers on the most recent poll;
    the percentages are computed against each poll's own ceiling so a server
    joining or leaving mid-build does not skew the series.
    """
    inflights: list[int] = []
    utils: list[float] = []
    last_ceiling = 0
    idle = under8 = near = 0
    for line in lines:
        m = _STATUS_RE.search(line)
        if m is None:
            continue
        inflight = int(m.group("inflight"))
        ceiling = sum(int(s.group("cores")) for s in _STATUS_SERVER_RE.finditer(m.group("servers")))
        if ceiling <= 0:
            continue
        inflights.append(inflight)
        last_ceiling = ceiling
        util = inflight / ceiling
        utils.append(util)
        if inflight == 0:
            idle += 1
        if inflight < ceiling / 8:
            under8 += 1
        if inflight >= ceiling * 7 / 8:
            near += 1
    n = len(inflights)
    if n == 0:
        return SaturationStats()
    return SaturationStats(
        samples=n,
        ceiling=last_ceiling,
        mean_inflight=mean(inflights),
        mean_util_pct=100.0 * mean(utils),
        idle_pct=100.0 * idle / n,
        under_eighth_pct=100.0 * under8 / n,
        near_sat_pct=100.0 * near / n,
    )


# --- client log -------------------------------------------------------------

# [m_spacing.o]: dist-job done on 192.168.8.172:10501 in 5841ms
#   (put_tc 1335ms, alloc 200ms, submit 0ms, run+fetch 4305ms, in_flight 85)
# Once the fork's W2 timer ships, a leading "preprocess <N>ms" field prefixes the
# breakdown: (preprocess N ms, put_tc N ms, ...) - it runs first chronologically
# and is not part of the "in <total>ms" round-trip. The prefix is optional so the
# parser matches both the pre- and post-timer log formats.
_JOB_RE = re.compile(
    r"dist-job done on (?P<addr>[\d.]+:\d+) in (?P<total>\d+)ms "
    r"\((?:preprocess (?P<preprocess>\d+)ms, )?put_tc (?P<put_tc>\d+)ms, alloc (?P<alloc>\d+)ms, "
    r"submit (?P<submit>\d+)ms, run\+fetch (?P<run_fetch>\d+)ms, in_flight (?P<in_flight>\d+)\)"
)
_NOT_ELIGIBLE_RE = re.compile(r"Compiling locally \(not eligible")
_FALLBACK_RE = re.compile(r"falling back to local: (?P<reason>.+?)\s*$")
# rustc JSON diagnostic: ..."code":{"code":"E0433"...
_RUST_ERR_RE = re.compile(r'"code":"(?P<code>E\d{3,4})"')


@dataclass
class ClientStats:
    """Per-compile timers + fallbacks parsed from the client ``SCCACHE_ERROR_LOG``."""

    jobs: int = 0
    per_node_jobs: Counter = field(default_factory=Counter)
    mean_total_ms: float = 0.0
    mean_put_tc_ms: float = 0.0
    mean_run_fetch_ms: float = 0.0
    mean_preprocess_ms: float | None = None  # None until the fork W2 timer ships
    not_eligible: int = 0  # configure conftests kept local (expected)
    fallback_reasons: Counter = field(default_factory=Counter)
    rust_error_codes: Counter = field(default_factory=Counter)


def _norm_reason(reason: str) -> str:
    """Collapse a fallback reason to a stable bucket (drop counts/addresses)."""
    r = reason.strip()
    if "Insufficient capacity" in r:
        return "gate-full (insufficient cluster capacity)"
    r = re.sub(r"\d+", "N", r)
    return r[:80]


def parse_client_log(lines: Iterable[str]) -> ClientStats:
    """Aggregate distributed-compile timers, local fallbacks, and remote rust errors.

    ``not_eligible`` counts configure conftests sccache intentionally keeps local
    (not a failure). ``fallback_reasons`` buckets the gate-full / error fallbacks.
    ``rust_error_codes`` counts rustc ``E0xxx`` diagnostics surfaced by the
    ``sccache::compiler=debug`` remote-stderr dump - the W3 rust-distribution
    failure signal.
    """
    stats = ClientStats()
    totals: list[int] = []
    put_tcs: list[int] = []
    run_fetches: list[int] = []
    preprocs: list[int] = []
    for line in lines:
        m = _JOB_RE.search(line)
        if m is not None:
            stats.jobs += 1
            stats.per_node_jobs[m.group("addr")] += 1
            totals.append(int(m.group("total")))
            put_tcs.append(int(m.group("put_tc")))
            run_fetches.append(int(m.group("run_fetch")))
            if m.group("preprocess") is not None:
                preprocs.append(int(m.group("preprocess")))
            continue
        if _NOT_ELIGIBLE_RE.search(line):
            stats.not_eligible += 1
            continue
        fb = _FALLBACK_RE.search(line)
        if fb is not None:
            stats.fallback_reasons[_norm_reason(fb.group("reason"))] += 1
            continue
        re_err = _RUST_ERR_RE.search(line)
        if re_err is not None:
            stats.rust_error_codes[re_err.group("code")] += 1
    if totals:
        stats.mean_total_ms = mean(totals)
        stats.mean_put_tc_ms = mean(put_tcs)
        stats.mean_run_fetch_ms = mean(run_fetches)
    if preprocs:
        stats.mean_preprocess_ms = mean(preprocs)
    return stats
