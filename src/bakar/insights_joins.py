"""Join executed tasks against buildstats records and task-graph nodes.

Split out of :mod:`bakar.insights_timing`, which orchestrates the timing
report and holds the pieces that don't belong to a single named cluster (see
that module's docstring for the report shape). This module owns the "did this
executed task reach a record" question for both sides that need answering it:

- :class:`GraphJoin` - did the task resolve to a node in the captured
  ``task-depends.dot`` graph, which feeds :mod:`bakar.insights_critical_path`.
- :class:`BuildstatsJoin` - did the task carry a matching buildstats record,
  which feeds the CPU floor in :mod:`bakar.insights_timing` and the
  per-task-type churn aggregation in :mod:`bakar.insights_churn`.

Both joins answer the same shape of question - what share of the executed set
reached a record - and refuse below the same :data:`JOIN_RATE_THRESHOLD`, so
:class:`_JoinGate`/:func:`_gate_join` hold the counting and verdict logic once,
shared by both :func:`_compute_graph_join` and :func:`_compute_join`.
:mod:`bakar.insights_churn` and :mod:`bakar.insights_critical_path` import the
matching/resolution helpers here (:func:`_match_record`, :func:`_index_records`,
:func:`_resolve_graph_node`, :func:`_capture_phrase`, :class:`_ParsedGraph`)
rather than re-implementing them, so this module has no dependency on either -
they depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bakar import graph_analyze, task_timings

if TYPE_CHECKING:
    from collections.abc import Callable, Container

    import networkx as nx

    from bakar.buildstats import BuildstatsRun, TaskStats

#: Share of executed tasks that must carry a buildstats record before any
#: CPU figure derived from that join may be published. The reference analyser
#: (chezmoi's ``yocto-bench-buildstats.py``) arrived at the same 95% after
#: publishing a path over a partially-joined graph: the result was confidently
#: wrong and read exactly like a correct one.
JOIN_RATE_THRESHOLD = 0.95

#: How many unjoined task keys a refusal names. Enough to recognise a pattern
#: (all setscene, all one recipe) without pasting the whole shortfall.
UNJOINED_SAMPLE = 5


@dataclass(frozen=True)
class _JoinResult:
    """Fields and rendering shared by :class:`GraphJoin` and :class:`BuildstatsJoin`.

    Both answer the same shape of question - what share of the executed set
    reached a record - and both refuse below the same :data:`JOIN_RATE_THRESHOLD`
    (via :class:`_JoinGate`). Holding ``rate`` and ``report_lines`` here once
    means a change to either lands on both joins at once, for the same reason
    :class:`_JoinGate` centralizes the counting and verdict logic: two copies
    could drift apart silently, and for a gate that means one of them quietly
    stops refusing.

    ``joined`` and ``executed`` are counted over the SAME set - every executed
    task raises ``executed``, and only a task that reached a record also raises
    ``joined``. Dropping unjoined tasks from both sides instead would pin the
    rate at 100% and pass the gate vacuously over precisely the partial-join
    case it exists to catch.

    ``available`` says the join was computed at all; it is false when the
    input was absent, empty, or its source raised. That is distinct from a
    computed-but-failing rate, which is ``available=True, gate_passed=False``.

    ``unjoined_sample`` is carried on the PASSING branch too: a residual that
    never grew past the gate is the one a reader can still act on, and a sample
    printed only once the gate has already refused surfaces the pattern one run
    too late.
    """

    available: bool = False
    gate_passed: bool = False
    executed: int = 0
    joined: int = 0
    unjoined_sample: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def rate(self) -> float:
        """Joined share of executed tasks, 0.0 when nothing executed.

        Zero executed tasks is a refusal, not a pass: a rate defined as 1.0 over
        an empty denominator would clear the gate on a run that measured nothing.
        """
        return (self.joined / self.executed) if self.executed else 0.0

    def report_lines(self) -> list[str]:
        """Render this section as plain text lines.

        The note carries the achieved rate on the PASSING branch as well as the
        refusing one: the figure a reader might act on is the one whose
        coverage they need, so naming the rate only when the section declines
        is exactly backwards.
        """
        lines = [f"  {self.note}"]
        lines.extend(f"  unjoined: {name}" for name in self.unjoined_sample)
        return lines


@dataclass(frozen=True)
class BuildstatsJoin(_JoinResult):
    """How much of this run's executed task set carries a buildstats record.

    Every CPU-derived figure in this report - the CPU floor, and therefore the
    concurrency floor built on it - is computed over the tasks that joined. A
    join covering 60% of the build yields a floor that is arithmetically fine
    and factually a floor for a different, smaller build, with nothing in its
    formatting to say so. So the rate is the gate, not a footnote.

    ``cpu_seconds`` is ``None`` whenever ``gate_passed`` is false. That is
    structural rather than stylistic: a downstream section cannot print a floor
    it was never handed the input for, so the "refused but printed anyway"
    failure has no expressible form here.

    The remaining fields, ``rate`` and ``report_lines`` are shared with
    :class:`GraphJoin` via :class:`_JoinResult` - see its docstring.
    """

    cpu_seconds: float | None = None
    note: str = "buildstats join unavailable: no buildstats source supplied"


@dataclass(frozen=True)
class GraphJoin(_JoinResult):
    """How much of this run's executed task set resolves to a task-graph node.

    The critical path is computed over the captured task graph and weighted by
    the executed tasks that resolve to its nodes, so a partially-joined graph
    yields a chain that is arithmetically fine and factually a chain through a
    different, smaller build. Measured on run ``20260910-173444`` a naive join
    landed at 66.3% while :class:`BuildstatsJoin` on the SAME run was 100.0% -
    the two joins measure different pairs of sides, so the buildstats gate
    cannot detect this failure at all and this one is separate rather than
    reused.

    ``available`` is false when no dependency source was supplied, when the
    source raised, or when the graph it returned is empty. An absent input is
    not a coverage failure and must not render as 0%, which is why the default
    note says "unavailable" rather than naming a rate -
    :class:`bakar.insights_timing.CpuFloor` makes the same distinction.

    ``unjoined_sample``, on the PASSING branch, is what the version-strip debt
    marker on :func:`_resolve_graph_node` asks a reader to watch: its upgrade
    trigger is "the sample is dominated by version-strip misses". The
    remaining fields, ``rate`` and ``report_lines`` are shared with
    :class:`BuildstatsJoin` via :class:`_JoinResult` - see its docstring.
    """

    note: str = "graph join unavailable: no dependency source supplied"


@dataclass(frozen=True)
class _ParsedGraph:
    """One parse of the captured dependency graph, shared by both consumers.

    The graph join and the critical path (:mod:`bakar.insights_critical_path`)
    read the same capture, and ``_dependency_source(run_dir, window)`` in
    :mod:`bakar.commands.insights` correlates the capture against the run's
    build window before it returns - so invoking it a second time is not free.
    Parsing once and handing this around is what keeps the report to one
    invocation without either consumer having to know the other exists.

    ``error`` carries the source's or the parser's failure text and is ``None``
    on success; ``graph`` is ``None`` exactly when ``error`` is set.
    """

    graph: nx.MultiDiGraph | None = None
    nodes: frozenset[str] = frozenset()
    error: str | None = None


def _parse_dependency_graph(dependency_source: Callable[[], tuple[str, str]]) -> _ParsedGraph:
    """Call the dependency source once and parse the graph it returns.

    ``dependency_source`` returns ``(dot_text, buildlist_text)`` - the same two
    artifacts ``bakar graph`` retrieves from a live ``bitbake -g`` run (see
    :mod:`bakar.commands.graph`). Parsing reuses
    :func:`bakar.graph_analyze.read_graph` instead of re-implementing DOT
    parsing, and node names reach both consumers verbatim as ``<pn>.<task>``.

    A raising source degrades to an ``error`` string, and so does a capture
    pydot could not parse - ``read_graph`` reports that as its second return
    value, which is what lets the consumers below say "unparseable" only when
    it is true and "empty" only when it is. This never raises back to
    :func:`bakar.insights_timing.timing_report`.
    """
    try:
        # buildlist_text (package_count etc.) isn't needed by either consumer.
        dot_text, _buildlist_text = dependency_source()
        graph, parsed_ok = graph_analyze.read_graph(dot_text)
    except Exception as exc:  # noqa: BLE001 - any dependency-source failure degrades gracefully
        return _ParsedGraph(error=f"dependency source failed ({exc})")
    if not parsed_ok:
        return _ParsedGraph(error="dependency graph could not be parsed")
    return _ParsedGraph(graph=graph, nodes=frozenset(graph.nodes))


#: One executed task's ``(recipe, task)`` identity, read from an artifact row
#: WITHOUT consulting its timestamps. This is the join's denominator, and it is
#: deliberately a different set from ``durations``: a row whose ``started`` or
#: ``completed`` is missing, unparseable or non-finite still records a task that
#: ran, and it must lower the join rate rather than vanish from both sides of it.
#:
#: What counts as executed is read from the row's ``outcome``, never from its
#: timestamps. A ``failed_silent`` row is the one exclusion: that outcome is a
#: setscene MISS, and :mod:`bakar.eventlog` records it from a ``TaskFailedSilent``
#: event that arrives with no preceding ``TaskStarted`` - the task never began,
#: so bitbake wrote no buildstats file for it and never will. Counting those
#: would put the gate below its threshold on any sstate-seeded build, which is
#: bakar's default regime, and a gate that always refuses reports nothing.
#: Every other outcome - including ``None``, a task that started and never
#: finished - counts: it ran, so a buildstats record is owed for it, and its
#: absence is real missing coverage rather than a taxonomy artifact.
#:
#: Two further degradations are chosen here rather than inherited:
#:
#: - A row carrying no ``task`` string yields no identity at all and is counted
#:   on NEITHER side. A row that cannot say what it is cannot say that it ran,
#:   so counting it would be inventing a denominator entry; when every row is
#:   like that the denominator is empty, which :func:`_compute_join` already
#:   refuses rather than treats as a pass.
#: - A row with a task but no usable ``recipe`` keeps identity ``("", task)``.
#:   That matches no buildstats record, so it counts in the denominator and not
#:   the numerator - the honest signal, since the tree may well hold the record
#:   and nothing here can say which recipe owns it.
_ExecutedTask = tuple[str, str]

#: The one ``outcome`` that means the task never began - see :data:`_ExecutedTask`.
_NOT_EXECUTED_OUTCOME = "failed_silent"


@dataclass(frozen=True)
class _JoinGate:
    """The join-rate verdict shared by :class:`GraphJoin` and :class:`BuildstatsJoin`.

    Both joins answer the same question over different resolvers - what share of
    the executed set reached a record - and both refuse below the same
    :data:`JOIN_RATE_THRESHOLD`. Holding one gate means a change to the
    threshold, to the empty-denominator refusal, or to the sample bound lands on
    both joins at once; two copies could drift apart silently, which for a gate
    means one of them quietly stops refusing.

    The gate owns the counting and the verdict only. Note wording stays with
    each caller: the refusals name different downstream sections ("no critical
    path", "no CPU-derived figure") and a generic sentence would cost the reader
    the one thing the note is for.

    ``matched`` collects the record keys the executed set reached. Only
    :func:`_compute_join` reads it, to sum CPU seconds over exactly those
    records; :func:`_compute_graph_join` has no per-node weight to pull from a
    node it merely resolved, so it ignores the field.
    """

    label: str
    executed: int
    joined: int
    unjoined_sample: list[str]
    matched: set[tuple[str, str]]
    passed: bool

    @property
    def is_empty(self) -> bool:
        """True when nothing executed, which is a refusal rather than a pass."""
        return not self.executed

    @property
    def empty_note(self) -> str:
        """The refusal note for an empty denominator."""
        return f"{self.label} refused: no executed tasks to join against"

    @property
    def rate_pct(self) -> float:
        """Joined share as a percentage, 0.0 over an empty denominator."""
        return (100.0 * self.joined / self.executed) if self.executed else 0.0

    @property
    def gate_pct(self) -> float:
        """The threshold this verdict was taken against, as a percentage."""
        return 100.0 * JOIN_RATE_THRESHOLD


def _gate_join(
    label: str,
    executed: list[_ExecutedTask],
    resolve: Callable[[str, str], tuple[str, str] | None],
) -> _JoinGate:
    """Resolve every executed task and take the join-rate verdict.

    ``resolve`` returns the record key a task reached, or ``None``. A task that
    reaches nothing is named in the bounded sample rather than only counted: a
    refusal has to be diagnosable, since it can fire for a bakar-side identity
    defect as readily as for a real coverage gap.

    Numerator and denominator are counted over the SAME set - every executed
    task raises ``executed``, and only a resolved one also raises ``joined``.
    Dropping unresolved tasks from both sides would pin the rate at 100% and
    pass vacuously over precisely the partial-join case the gate exists to
    catch.
    """
    matched: set[tuple[str, str]] = set()
    unjoined: list[str] = []
    joined = 0
    for recipe, task in executed:
        key = resolve(recipe, task)
        if key is None:
            unjoined.append(f"{recipe}:{task}")
        else:
            joined += 1
            matched.add(key)

    executed_count = len(executed)
    return _JoinGate(
        label=label,
        executed=executed_count,
        joined=joined,
        unjoined_sample=unjoined[:UNJOINED_SAMPLE],
        matched=matched,
        passed=bool(executed_count) and joined >= JOIN_RATE_THRESHOLD * executed_count,
    )


def _compute_graph_join(parsed: _ParsedGraph, executed: list[_ExecutedTask]) -> GraphJoin:
    """Join executed tasks against task-graph nodes and gate on the rate.

    ``executed`` is the timestamp-independent identity set
    (:data:`_ExecutedTask`), the same denominator :func:`_compute_join` uses -
    a task with no usable timestamp still ran, so it must lower this rate
    rather than vanish from both sides of it.

    Resolution is :func:`_resolve_graph_node`'s exact-then-setscene-fallback;
    the counting, the bounded sample and the verdict are :func:`_gate_join`'s,
    shared with :func:`_compute_join`.
    """
    if parsed.error is not None:
        return GraphJoin(note=f"graph join unavailable: {parsed.error}")
    if not parsed.nodes:
        # An unparseable capture never reaches here - _parse_dependency_graph
        # turns read_graph's parsed_ok=False into an error above - so this
        # branch is the genuinely empty graph and says so.
        return GraphJoin(note="graph join unavailable: empty dependency graph")

    gate = _gate_join(
        "graph join",
        executed,
        lambda recipe, task: _resolve_graph_node(parsed.nodes, recipe, task),
    )
    if gate.is_empty:
        return GraphJoin(available=True, note=gate.empty_note)

    if not gate.passed:
        # Two decimals, not one: at real build scale (thousands of executed
        # tasks) a refused rate can round to the same one-decimal figure as
        # the gate itself (94.97% -> "95.0%"), reading as "95.0% ... below
        # the 95.0% gate" - a display collision, not a wrong verdict, but one
        # a reader has no way to tell apart from a real contradiction.
        return GraphJoin(
            available=True,
            executed=gate.executed,
            joined=gate.joined,
            unjoined_sample=gate.unjoined_sample,
            note=(
                f"graph join refused: {gate.rate_pct:.2f}% of executed tasks resolved to a graph node, "
                f"below the {gate.gate_pct:.2f}% gate ({gate.executed - gate.joined} of {gate.executed} "
                f"executed tasks reach no node) - no critical path is reported for this run"
            ),
        )

    return GraphJoin(
        available=True,
        gate_passed=True,
        executed=gate.executed,
        joined=gate.joined,
        unjoined_sample=gate.unjoined_sample,
        note=(
            f"graph join {gate.rate_pct:.1f}% "
            f"({gate.joined} of {gate.executed} executed tasks resolved to a graph node)"
        ),
    )


def _join_key(recipe: str, task: str) -> tuple[str, str]:
    """Key both sides of the join on ``(PN, task)``, version stripped.

    The event log records a versioned PF (``busybox-1.36.1-r0``) and so does the
    buildstats recipe directory, but they are not guaranteed to agree on the
    revision suffix - a task restored from sstate and one rebuilt after a bump
    can disagree by ``-r0`` alone. Stripping the version the way
    :func:`bakar.task_timings.strip_recipe_version` already does for baseline
    keys lets those two spellings still meet.

    This key is a FALLBACK, never the primary one - see :func:`_match_record`.
    Aggregating records under it would merge ``busybox-1.36.1-r0`` and
    ``busybox-1.37-r0`` into one bucket, so a stale record for a PF this run
    never executed would be credited to the PF it did.
    """
    return (task_timings.strip_recipe_version(recipe), task)


_RecordKey = tuple[str, str]


def _index_records(
    tasks: list[TaskStats],
) -> tuple[dict[_RecordKey, list[TaskStats]], dict[_RecordKey, dict[_RecordKey, None]]]:
    """Index buildstats records by exact ``(PF, task)`` and by stripped key.

    The second index maps a stripped key to every exact key carrying it, which
    is what makes an ambiguous strip detectable rather than silently merged.

    Its buckets are dicts used as ORDERED SETS, not lists, and the distinction
    is measured rather than stylistic. A list bucket needs ``key not in bucket``
    to dedupe, which is a linear scan per record and quadratic per stripped key.
    On an ordinary capture that is invisible - each stripped key carries one or
    two exact keys - but a tree holding many versions of one recipe collapses
    them all into a single bucket: 3,000 versions of one recipe measured 37 ms
    of pure comparison after the filesystem walk had already finished. A dict
    keeps insertion order, so the ambiguity check below still sees candidates in
    the order they were read.
    """
    exact: dict[_RecordKey, list[TaskStats]] = {}
    stripped: dict[_RecordKey, dict[_RecordKey, None]] = {}
    for stat in tasks:
        key = (stat.recipe, stat.task)
        exact.setdefault(key, []).append(stat)
        stripped.setdefault(_join_key(stat.recipe, stat.task), {})[key] = None
    return exact, stripped


def _match_record(
    exact: dict[tuple[str, str], list[TaskStats]],
    stripped: dict[_RecordKey, dict[_RecordKey, None]],
    recipe: str,
    task: str,
) -> tuple[str, str] | None:
    """Resolve one executed task to the buildstats record it owns, or ``None``.

    Exact ``(PF, task)`` first, because that is the only match that proves the
    record belongs to the version this run executed. The version-stripped key is
    tried next and ONLY when it is unambiguous, which is what keeps the strip
    doing the job it was added for - the two sides disagreeing by a revision
    suffix - without letting it credit a version the run never built.

    An ambiguous stripped key resolves to ``None`` and therefore counts as
    unjoined. That lowers the join rate, which is the honest signal: the tree
    holds two versions of this recipe and nothing here can say which one ran.
    """
    key = (recipe, task)
    if key in exact:
        return key
    candidates = stripped.get(_join_key(recipe, task)) or {}
    return next(iter(candidates)) if len(candidates) == 1 else None


#: Suffix bitbake gives a task that RESTORES another task's output from sstate.
#: ``task-depends.dot`` carries none of these nodes - ``bitbake -g`` graphs the
#: work a build can do, never the restore variants that stand in for it - so a
#: restore only reaches the graph through the fallback in
#: :func:`_resolve_graph_node`.
_SETSCENE_SUFFIX = "_setscene"


def _resolve_graph_node(nodes: Container[str], recipe: str, task: str) -> tuple[str, str] | None:
    """Resolve one executed ``(PF, task)`` identity to a task-graph node.

    Returns ``(node, contributing_task)`` or ``None``. The second element is the
    name of the task that actually ran, which is what a caller needs to say
    where a node's weight came from: a node resolved through the setscene
    fallback is weighted by seconds ``do_populate_sysroot`` never spent, and
    bucketing that under the node's own name would report time against a task
    that did not run.

    Exact ``<PN>.<task>`` first, mirroring :func:`_match_record`'s discipline for
    the buildstats join. The exact match is the only one that proves the node
    belongs to the task that ran. Only when it misses, and only when the task is
    a setscene restore, is the suffix removed and the node it stands in for
    tried. Stripping first would credit an ordinary task to a node it does not
    own, and it would throw away that proof for nothing - measured on run
    20260910-173444 the ordering costs no join, while the fallback itself takes
    the rate from 66.3% to 99.2%.
    """
    # devtool-debt: identity goes through ``strip_recipe_version``, which strips
    # one trailing ``-<digits>[-r<n>]``, so a PV containing a hyphen
    # (``libedit-20251016-3.1-r0``) reduces to ``libedit-20251016`` and misses
    # the ``libedit`` node. Ceiling: the residual stays inside the graph gate's
    # 5% budget - measured 21 of 2566 identities (0.8%) on run 20260910-173444,
    # every one of them ``libedit`` or ``libedit-native``. Upgrade trigger: a
    # run refuses with its unjoined sample dominated by version-strip misses,
    # at which point resolve PN boundaries against the captured ``pn-buildlist``
    # rather than by suffix arithmetic (design D7).
    pn = task_timings.strip_recipe_version(recipe)
    exact = f"{pn}.{task}"
    if exact in nodes:
        return (exact, task)
    if task.endswith(_SETSCENE_SUFFIX):
        stood_in_for = f"{pn}.{task[: -len(_SETSCENE_SUFFIX)]}"
        if stood_in_for in nodes:
            return (stood_in_for, task)
    return None


def _capture_phrase(run: BuildstatsRun) -> str:
    """Name the capture directory the figures came from.

    Printed on the PASSING path as well as the refusing ones. Naming the source
    only when the section declines to publish is exactly backwards for auditing:
    the number a reader might act on is the one whose provenance they need.
    """
    return "capture directory unrecorded" if run.directory is None else f"from capture {run.directory}"


def _compute_join(
    buildstats_source: Callable[[], BuildstatsRun],
    executed: list[_ExecutedTask],
) -> BuildstatsJoin:
    """Join executed tasks against buildstats records and gate on the rate.

    ``executed`` is the identity set read from the artifact's task rows, NOT the
    duration list - see :data:`_ExecutedTask`. Taking the duration list instead
    made the denominator "tasks with a usable timestamp", so a task with a
    missing one left the numerator and the denominator together and the rate
    stayed at 100% over a build the records covered a fraction of.

    Follows :func:`bakar.insights_critical_path._compute_critical_path`'s
    precedent exactly: any failure - the callable raises, the tree is absent,
    the tree is empty, no capture correlates with this run - returns an
    explicit unavailable result with a note and never raises back to
    :func:`bakar.insights_timing.timing_report`.

    The four outcomes keep separate notes. They are what
    :mod:`bakar.buildstats` went out of its way to distinguish, and collapsing
    them here would put the distinction back in the bin it was lifted out of:
    a tree that was never found is a path problem, a tree that recorded nothing
    is a measurement, and a tree holding only some other build's captures is a
    provenance failure that a rate near 100% would otherwise hide.
    """
    try:
        run = buildstats_source()
    except Exception as exc:  # noqa: BLE001 - any buildstats-source failure degrades gracefully
        return BuildstatsJoin(note=f"buildstats join unavailable: source failed ({exc})")

    if run.outcome == "absent":
        return BuildstatsJoin(note=f"buildstats join unavailable: tree absent ({run.note})")
    if run.outcome == "uncorrelated":
        return BuildstatsJoin(note=f"buildstats join unavailable: no capture belongs to this run ({run.note})")
    if run.outcome != "parsed":
        return BuildstatsJoin(
            note=f"buildstats join unavailable: tree present but recorded nothing ({run.note})",
        )

    exact, stripped = _index_records(run.tasks)

    gate = _gate_join(
        "buildstats join",
        executed,
        lambda recipe, task: _match_record(exact, stripped, recipe, task),
    )
    if gate.is_empty:
        return BuildstatsJoin(available=True, note=gate.empty_note)

    if not gate.passed:
        return BuildstatsJoin(
            available=True,
            executed=gate.executed,
            joined=gate.joined,
            unjoined_sample=gate.unjoined_sample,
            note=(
                f"buildstats join refused: {gate.rate_pct:.1f}% of executed tasks joined, below the "
                f"{gate.gate_pct:.1f}% gate ({gate.executed - gate.joined} of {gate.executed} executed tasks "
                f"have no buildstats record) - no CPU-derived figure is reported for this run. "
                f"{_capture_phrase(run)}"
            ),
        )

    # Only records an executed task actually matched contribute, and matching is
    # by exact ``(PF, task)`` with an unambiguous version-stripped fallback (see
    # :func:`_match_record`). A buildstats tree carries rows this run never
    # executed - a stale capture, a second version of the same recipe, a sibling
    # machine's directory - and neither summing the tree wholesale nor
    # aggregating under the stripped key would keep those out of the total.
    return BuildstatsJoin(
        available=True,
        gate_passed=True,
        executed=gate.executed,
        joined=gate.joined,
        cpu_seconds=sum(stat.cpu_seconds for k in gate.matched for stat in exact[k]),
        unjoined_sample=gate.unjoined_sample,
        note=(
            f"buildstats join {gate.rate_pct:.1f}% ({gate.joined} of {gate.executed} executed tasks matched a "
            f"buildstats record). {_capture_phrase(run)}"
        ),
    )
