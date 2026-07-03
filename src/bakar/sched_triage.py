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
from statistics import mean, median
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
# journalctl -o short-unix prefixes each line with an epoch (10+ digits . micros).
_TS_RE = re.compile(r"(?P<ts>\d{9,}\.\d+)")


@dataclass
class AllocStats:
    """Routing decisions parsed from ``dist-alloc`` lines."""

    total: int = 0
    # An alloc where a strictly-less-loaded candidate existed but was not chosen.
    misroutes: int = 0
    # A misroute where the skipped, less-loaded candidate had zero jobs (idle).
    idle_skips: int = 0
    per_node_chosen: Counter = field(default_factory=Counter)
    truncated: int = 0  # alloc lines whose candidate list was cut short (load==0 break)
    total_by_bucket: Counter = field(default_factory=Counter)  # non-truncated allocs by concurrent in-flight
    misroutes_by_bucket: Counter = field(default_factory=Counter)
    idle_skips_by_bucket: Counter = field(default_factory=Counter)

    @property
    def misroute_pct(self) -> float:
        return 100.0 * self.misroutes / self.total if self.total else 0.0


@dataclass
class SaturationStats:
    """Cluster in-flight utilisation parsed from ``dist-status poll`` lines."""

    samples: int = 0
    ceiling: int = 0  # total cores across servers (last poll)
    admission_ceiling: int = 0  # summed cores_plus_slack (c+1+c//8), last poll
    mean_inflight: float = 0.0
    mean_util_pct: float = 0.0
    idle_pct: float = 0.0  # share of polls with 0 in-progress
    under_eighth_pct: float = 0.0  # share of polls below 1/8 of ceiling
    near_sat_pct: float = 0.0  # share of polls at >= 7/8 of ceiling


@dataclass
class PollSample:
    """One ``dist-status poll`` with its epoch and per-server occupancy."""

    ts: float
    inflight: int
    per_server_jobs: dict[str, int]
    per_server_cores: dict[str, int]


@dataclass
class SupplyBucket:
    """Utilisation of the polls that share a do_compile supply level."""

    polls: int = 0
    mean_util_pct: float = 0.0
    per_node_ratio: dict[str, float] = field(default_factory=dict)  # addr -> mean jobs/cores


@dataclass
class WeightedUtil:
    """Time-weighted utilisation with the cadence stats that qualify it."""

    mean_util_pct: float = 0.0
    median_cadence_s: float = 0.0
    max_gap_s: float = 0.0


def _load_bucket(concurrent: int) -> str:
    """Bucket an alloc by the concurrent in-flight jobs at decision time.

    ``concurrent`` is the sum of the candidate servers' pre-assignment job
    counts. Only high-bucket misroutes are actionable - a wrong choice when
    both nodes are loaded costs real queueing; low-bucket misroutes are
    dominated by don't-care ties.
    """
    if concurrent < 16:
        return "low"
    if concurrent < 48:
        return "mid"
    return "high"


def parse_dist_alloc(lines: Iterable[str]) -> AllocStats:
    """Aggregate routing quality from the scheduler's ``dist-alloc`` lines.

    A *misroute* is an allocation whose chosen server was not the least-loaded
    candidate; an *idle skip* is a misroute where the cheaper candidate had zero
    jobs. Lines without a parseable chosen server are skipped.

    A line whose candidate list is shorter than the widest list seen in the run
    was *truncated* by the scheduler's ``load == 0`` early break (main.rs): it
    omits the unscanned servers, so it cannot express a misroute and would bias
    the rate downward. Truncated lines are counted in ``truncated`` and still
    attributed in ``per_node_chosen``, but excluded from ``total`` and the
    misroute math. Non-truncated allocs are bucketed by the concurrent in-flight
    jobs at decision time so the load-dependent rate is visible (only the high
    bucket is actionable). A single streaming pass records each alloc, then the
    widest candidate count classifies truncation once it is known.
    """
    stats = AllocStats()
    parsed: list[tuple[int, int, str, bool, bool]] = []
    max_cands = 0
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
        max_cands = max(max_cands, len(cands))
        concurrent = sum(jobs for _load, jobs in cands.values())
        chosen_load = cands[chosen_addr][0]
        cheaper = [jobs for addr, (load, jobs) in cands.items() if addr != chosen_addr and load < chosen_load]
        parsed.append((len(cands), concurrent, chosen_addr, bool(cheaper), any(j == 0 for j in cheaper)))
    for n_cands, concurrent, chosen_addr, is_misroute, is_idle_skip in parsed:
        stats.per_node_chosen[chosen_addr] += 1
        if n_cands < max_cands:
            stats.truncated += 1
            continue
        stats.total += 1
        bucket = _load_bucket(concurrent)
        stats.total_by_bucket[bucket] += 1
        if is_misroute:
            stats.misroutes += 1
            stats.misroutes_by_bucket[bucket] += 1
            if is_idle_skip:
                stats.idle_skips += 1
                stats.idle_skips_by_bucket[bucket] += 1
    return stats


def parse_dist_status(lines: Iterable[str]) -> SaturationStats:
    """Aggregate cluster utilisation from the scheduler's ``dist-status poll`` lines.

    ``ceiling`` is the summed core count across servers on the most recent poll;
    ``mean_util_pct`` is computed against each poll's own core ceiling so a
    server joining or leaving mid-build does not skew the series. ``near_sat``
    is measured against the *admission* ceiling instead - ``cores_plus_slack =
    c + 1 + c//8`` per server, the scheduler's own accept cutoff (main.rs
    ``load_weight``) - so it reflects how often the scheduler was close to
    refusing work, not merely how busy the cores were. For two 32-core servers
    the admission ceiling is 74, not the raw 64.
    """
    inflights: list[int] = []
    utils: list[float] = []
    last_ceiling = 0
    last_admission = 0
    idle = under8 = near = 0
    for line in lines:
        m = _STATUS_RE.search(line)
        if m is None:
            continue
        inflight = int(m.group("inflight"))
        server_cores = [int(s.group("cores")) for s in _STATUS_SERVER_RE.finditer(m.group("servers"))]
        ceiling = sum(server_cores)
        if ceiling <= 0:
            continue
        admission = sum(c + 1 + c // 8 for c in server_cores)
        inflights.append(inflight)
        last_ceiling = ceiling
        last_admission = admission
        util = inflight / ceiling
        utils.append(util)
        if inflight == 0:
            idle += 1
        if inflight < ceiling / 8:
            under8 += 1
        if inflight >= admission * 7 / 8:
            near += 1
    n = len(inflights)
    if n == 0:
        return SaturationStats()
    return SaturationStats(
        samples=n,
        ceiling=last_ceiling,
        admission_ceiling=last_admission,
        mean_inflight=mean(inflights),
        mean_util_pct=100.0 * mean(utils),
        idle_pct=100.0 * idle / n,
        under_eighth_pct=100.0 * under8 / n,
        near_sat_pct=100.0 * near / n,
    )


def parse_dist_status_series(lines: Iterable[str]) -> list[PollSample]:
    """Parse each ``dist-status poll`` line into a timestamped :class:`PollSample`.

    Wants the journal in ``-o short-unix`` format so each line carries a leading
    epoch; a line without one gets ts 0.0 (it still parses, but cannot be joined
    to the task timeline).
    """
    samples: list[PollSample] = []
    for line in lines:
        m = _STATUS_RE.search(line)
        if m is None:
            continue
        tm = _TS_RE.search(line)
        ts = float(tm.group("ts")) if tm else 0.0
        jobs: dict[str, int] = {}
        cores: dict[str, int] = {}
        for s in _STATUS_SERVER_RE.finditer(m.group("servers")):
            jobs[s.group("addr")] = int(s.group("jobs"))
            cores[s.group("addr")] = int(s.group("cores"))
        samples.append(
            PollSample(ts=ts, inflight=int(m.group("inflight")), per_server_jobs=jobs, per_server_cores=cores)
        )
    return samples


def conditioned_util(series: list[PollSample], compile_intervals: list[tuple[float, float]]) -> dict[str, SupplyBucket]:
    """Bucket poll utilisation by how many do_compile tasks were live at each poll.

    ``compile_intervals`` is the ``(started, completed)`` epoch span of every
    do_compile task (the caller filters the bitbake task timeline). A poll's
    supply level is the count of intervals containing its ts: 0 -> ``idle``,
    1-7 -> ``low``, >= 8 -> ``high``. This separates "the cluster was idle
    because no compiles existed" from "compiles existed but a node starved" -
    the per-node jobs/cores ratio in the ``high`` bucket is the feed-bottleneck
    signal a flat whole-build util average hides.
    """
    buckets: dict[str, SupplyBucket] = {name: SupplyBucket() for name in ("idle", "low", "high")}
    util_sums: dict[str, float] = dict.fromkeys(buckets, 0.0)
    ratio_sums: dict[str, dict[str, float]] = {name: {} for name in buckets}
    for poll in series:
        live = sum(1 for start, end in compile_intervals if start <= poll.ts <= end)
        name = "idle" if live == 0 else "low" if live < 8 else "high"
        bucket = buckets[name]
        bucket.polls += 1
        total_cores = sum(poll.per_server_cores.values())
        if total_cores > 0:
            util_sums[name] += poll.inflight / total_cores
        for addr, cores in poll.per_server_cores.items():
            if cores > 0:
                ratio_sums[name][addr] = ratio_sums[name].get(addr, 0.0) + poll.per_server_jobs.get(addr, 0) / cores
    for name, bucket in buckets.items():
        if bucket.polls:
            bucket.mean_util_pct = 100.0 * util_sums[name] / bucket.polls
            bucket.per_node_ratio = {addr: total / bucket.polls for addr, total in ratio_sums[name].items()}
    return buckets


def time_weighted_util(series: list[PollSample], max_gap_multiple: float = 5.0) -> WeightedUtil:
    """Time-weight poll utilisation so an irregular, request-driven cadence does not bias the mean.

    dist-status polls are emitted only when something queries the scheduler, so
    the cadence is uneven; an equal-weight mean over-counts bursts of frequent
    polls. Weight each poll by the gap to the next poll - the span its util
    represents - capping any gap at ``max_gap_multiple`` times the median gap so
    a monitor outage does not swamp the average. The last poll is weighted by
    the median gap. ``max_gap_s`` (uncapped) flags any window where an "idle"
    reading is really "unobserved".
    """
    ordered = sorted(series, key=lambda p: p.ts)
    if not ordered:
        return WeightedUtil()
    utils = [(p.inflight / c if (c := sum(p.per_server_cores.values())) > 0 else 0.0) for p in ordered]
    gaps = [ordered[i + 1].ts - ordered[i].ts for i in range(len(ordered) - 1)]
    if not gaps:
        return WeightedUtil(mean_util_pct=100.0 * utils[0])
    med = median(gaps)
    cap = max_gap_multiple * med
    weights = [min(g, cap) for g in gaps] + [med]
    total_w = sum(weights)
    weighted = sum(u * w for u, w in zip(utils, weights, strict=True)) / total_w if total_w else 0.0
    return WeightedUtil(mean_util_pct=100.0 * weighted, median_cadence_s=med, max_gap_s=max(gaps))


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
