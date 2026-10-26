import itertools

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from orch.graph import CycleError, Graph


def g_from(edges: dict[str, list[str]], durations: dict[str, float] | None = None) -> Graph:
    g = Graph()
    for n, ds in edges.items():
        g.add(n, ds, (durations or {}).get(n, 60.0))
    return g


@st.composite
def dags(draw, max_nodes=12):
    """Random DAG: node i may only depend on nodes < i."""
    n = draw(st.integers(1, max_nodes))
    names = [f"s{i:02d}" for i in range(n)]
    edges = {names[i]: [names[j] for j in range(i) if draw(st.booleans()) and draw(st.booleans())]
             for i in range(n)}
    durs = {name: float(draw(st.integers(1, 100))) for name in names}
    return g_from(edges, durs)


@st.composite
def digraphs(draw, max_nodes=9):
    n = draw(st.integers(1, max_nodes))
    names = [f"n{i}" for i in range(n)]
    edges = {a: [b for b in names if draw(st.integers(0, 4)) == 0] for a in names}
    return g_from(edges)


def reach(g: Graph) -> dict[str, set[str]]:
    r = {n: set() for n in g.deps}
    for n in g.deps:                                       # DFS from every node: brute-force reachability
        stack = list(g.deps[n])
        while stack:
            m = stack.pop()
            if m not in r[n]:
                r[n].add(m)
                stack.extend(g.deps[m])
    return r


def test_waves_and_critical_path_on_a_small_estate():
    g = g_from({"db": [], "cache": [], "auth": ["db"], "orders": ["auth", "db", "cache"], "web": ["orders"]},
               {"db": 120, "cache": 30, "auth": 40, "orders": 60, "web": 20})
    assert g.waves() == [["cache", "db"], ["auth"], ["orders"], ["web"]]
    assert g.critical_path() == (240.0, ["db", "auth", "orders", "web"])
    assert g.wave_makespan() == 240.0
    assert g.redundant_edges() == [("orders", "db")]          # orders -> auth -> db already implies it
    assert g.blast_radius(["db"]) == ["auth", "orders", "web"]


def test_cycle_is_reported_with_its_path():
    g = g_from({"a": ["b"], "b": ["c"], "c": ["a"], "d": ["a"], "e": ["e"]})
    cycles = sorted(g.cycles())
    assert cycles == [["a", "b", "c"], ["e"]]
    with pytest.raises(CycleError, match="a -> b -> c -> a"):
        g.waves()


def test_unknown_dependency_is_rejected():
    with pytest.raises(ValueError, match="unknown dependencies"):
        Graph.from_dict({"a": {"depends_on": ["ghost"]}})


def test_scheduling_beats_waves_when_durations_differ():
    # wave 0 = {slow-db (300s), fast-cache (10s)}; app-on-cache can start at 10s with a scheduler,
    # but with plain waves it waits for the slow db.
    g = g_from({"slow-db": [], "fast-cache": [], "app-on-cache": ["fast-cache"], "app-on-db": ["slow-db"]},
               {"slow-db": 300, "fast-cache": 10, "app-on-cache": 200, "app-on-db": 10})
    makespan, _ = g.schedule(workers=4)
    assert makespan == 310 and g.wave_makespan() == 500


@settings(max_examples=300, deadline=None)
@given(digraphs())
def test_tarjan_matches_brute_force_mutual_reachability(g):
    r = reach(g)
    expected = {frozenset(n2 for n2 in g.deps if n2 == n or (n2 in r[n] and n in r[n2])) for n in g.deps}
    assert {frozenset(c) for c in g.strongly_connected_components()} == expected
    for c in g.cycles():                                   # every reported cycle is a real cycle
        for a, b in zip(c, c[1:] + c[:1], strict=True):
            assert b in g.deps[a]


@settings(max_examples=300, deadline=None)
@given(dags())
def test_waves_respect_every_dependency(g):
    wave_of = {n: i for i, w in enumerate(g.waves()) for n in w}
    assert set(wave_of) == set(g.deps)
    for n, ds in g.deps.items():
        for d in ds:
            assert wave_of[d] < wave_of[n]
        if ds:                                             # and each service is as early as possible
            assert wave_of[n] == 1 + max(wave_of[d] for d in ds)


@settings(max_examples=200, deadline=None)
@given(dags(max_nodes=9))
def test_critical_path_matches_brute_force(g):
    def chains(n):                                         # every dependency chain ending at n
        if not g.deps[n]:
            return [[n]]
        return [c + [n] for d in g.deps[n] for c in chains(d)]
    best = max(sum(g.duration[x] for x in c) for n in g.deps for c in chains(n))
    length, path = g.critical_path()
    assert length == best
    assert sum(g.duration[x] for x in path) == length
    assert all(path[i] in g.deps[path[i + 1]] for i in range(len(path) - 1))


@settings(max_examples=300, deadline=None)
@given(dags(), st.integers(1, 6))
def test_schedule_is_valid_and_within_bounds(g, workers):
    makespan, times = g.schedule(workers)
    for n, ds in g.deps.items():                           # dependencies finish before dependents start
        for d in ds:
            assert times[d][1] <= times[n][0] + 1e-9
    events = sorted({t for s, e in times.values() for t in (s, e)})
    for t in events:                                       # never more than `workers` deploys at once
        assert sum(1 for s, e in times.values() if s <= t < e) <= workers
    assert g.lower_bound(workers) - 1e-9 <= makespan <= sum(g.duration.values()) + 1e-9
    if workers >= len(g.deps):
        assert makespan == pytest.approx(g.critical_path()[0])   # enough slots: optimal


@settings(max_examples=200, deadline=None)
@given(dags())
def test_blast_radius_is_reverse_reachability(g):
    r = reach(g)
    for changed in itertools.islice(g.deps, 3):
        assert set(g.blast_radius([changed])) == {n for n in g.deps if changed in r[n]}


@settings(max_examples=150, deadline=None)
@given(dags(max_nodes=10))
def test_removing_redundant_edges_keeps_the_same_order(g):
    reduced = Graph({n: set(ds) for n, ds in g.deps.items()}, dict(g.duration))
    for a, b in g.redundant_edges():
        reduced.deps[a].discard(b)
    assert reach(reduced) == reach(g)
    assert reduced.waves() == g.waves()
