"""Dependency-graph algorithms for releasing many microservices in the right order.

An edge  A -> B  means "A depends on B": B must be deployed (and healthy) before A.

| Question                                   | Algorithm                                    | Cost        |
|--------------------------------------------|----------------------------------------------|-------------|
| Is there a dependency cycle? Which one?    | Tarjan's strongly connected components       | O(V + E)    |
| In which order / waves can we deploy?      | Kahn's topological sort, wave = longest path | O(V + E)    |
| Fastest possible release (unlimited slots) | critical path: DP over topological order     | O(V + E)    |
| Fastest release with N parallel slots      | list scheduling by longest remaining path    | O((V+E) log V) |
| What does changing X affect?               | BFS on the reversed graph (dependents)       | O(V + E)    |
| Which declared dependencies are redundant? | transitive reduction via reachability        | O(V * (V + E)) |
"""
from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass, field


class CycleError(ValueError):
    def __init__(self, cycles: list[list[str]]) -> None:
        self.cycles = cycles
        super().__init__("dependency cycles: " + "; ".join(" -> ".join(c + [c[0]]) for c in cycles))


@dataclass
class Graph:
    deps: dict[str, set[str]] = field(default_factory=dict)        # service -> services it depends on
    duration: dict[str, float] = field(default_factory=dict)       # estimated deploy seconds

    @classmethod
    def from_dict(cls, services: dict[str, dict]) -> Graph:
        missing = {d for spec in services.values() for d in spec.get("depends_on", [])} - set(services)
        if missing:                                  # check first: add() would silently create them
            raise ValueError(f"unknown dependencies: {sorted(missing)}")
        g = cls()
        for name, spec in services.items():
            g.add(name, spec.get("depends_on", []), float(spec.get("deploy_seconds", 60)))
        return g

    def add(self, name: str, depends_on: list[str] | set[str] = (), duration: float = 60.0) -> None:
        self.deps.setdefault(name, set()).update(depends_on)
        self.duration[name] = duration
        for d in depends_on:
            self.deps.setdefault(d, set())
            self.duration.setdefault(d, 60.0)

    @property
    def nodes(self) -> list[str]:
        return sorted(self.deps)

    def dependents(self) -> dict[str, set[str]]:
        rev: dict[str, set[str]] = {n: set() for n in self.deps}
        for n, ds in self.deps.items():
            for d in ds:
                rev[d].add(n)
        return rev

    # ---------------------------------------------------------------- cycles
    def strongly_connected_components(self) -> list[list[str]]:
        """Tarjan's algorithm, iterative (no recursion limit on big graphs)."""
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        on_stack: set[str] = set()
        stack: list[str] = []
        sccs: list[list[str]] = []
        counter = 0
        for root in self.nodes:
            if root in index:
                continue
            work = [(root, iter(sorted(self.deps[root])))]
            index[root] = low[root] = counter
            counter += 1
            stack.append(root)
            on_stack.add(root)
            while work:
                v, it = work[-1]
                advanced = False
                for w in it:
                    if w not in index:
                        index[w] = low[w] = counter
                        counter += 1
                        stack.append(w)
                        on_stack.add(w)
                        work.append((w, iter(sorted(self.deps[w]))))
                        advanced = True
                        break
                    if w in on_stack:
                        low[v] = min(low[v], index[w])
                if advanced:
                    continue
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[v])
                if low[v] == index[v]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == v:
                            break
                    sccs.append(sorted(comp))
        return sccs

    def cycles(self) -> list[list[str]]:
        """One concrete cycle per non-trivial SCC (or self-dependency), for a readable error."""
        out = []
        for comp in self.strongly_connected_components():
            members = set(comp)
            if len(comp) == 1 and comp[0] not in self.deps[comp[0]]:
                continue
            start, path, seen = comp[0], [comp[0]], {comp[0]}
            node = start
            while True:                              # walk inside the SCC until we return to a visited node
                nxt = min(d for d in self.deps[node] if d in members)
                if nxt in seen:
                    out.append(path[path.index(nxt):])
                    break
                path.append(nxt)
                seen.add(nxt)
                node = nxt
        return out

    # ---------------------------------------------------------------- ordering
    def waves(self) -> list[list[str]]:
        """Kahn's algorithm by levels: wave k holds services whose longest dependency chain has length k."""
        cyc = self.cycles()
        if cyc:
            raise CycleError(cyc)
        indeg = {n: len(ds) for n, ds in self.deps.items()}
        rev = self.dependents()
        current = sorted(n for n, d in indeg.items() if d == 0)
        out = []
        while current:
            out.append(current)
            nxt = []
            for n in current:
                for m in rev[n]:
                    indeg[m] -= 1
                    if indeg[m] == 0:
                        nxt.append(m)
            current = sorted(nxt)
        return out

    def topological_order(self) -> list[str]:
        return [n for w in self.waves() for n in w]

    def critical_path(self) -> tuple[float, list[str]]:
        """Longest duration-weighted chain = minimum release time with unlimited parallel deploys."""
        finish: dict[str, float] = {}
        prev: dict[str, str | None] = {}
        for n in self.topological_order():
            start, p = 0.0, None
            for d in self.deps[n]:
                if finish[d] > start:
                    start, p = finish[d], d
            finish[n] = start + self.duration[n]
            prev[n] = p
        end = max(finish, key=lambda n: (finish[n], n))
        path = [end]
        while prev[path[-1]] is not None:
            path.append(prev[path[-1]])
        return finish[end], path[::-1]

    def wave_makespan(self) -> float:
        """Release time when each wave waits for its slowest member (what plain sync waves do)."""
        return sum(max(self.duration[n] for n in w) for w in self.waves())

    def schedule(self, workers: int) -> tuple[float, dict[str, tuple[float, float]]]:
        """List scheduling with `workers` parallel deploy slots.

        Priority = length of the longest remaining chain from the service to the end of the release
        ("upward rank"): services on the critical path go first. Ready services wait in a max-heap,
        running deploys in a min-heap of finish times.
        """
        if workers < 1:
            raise ValueError("workers must be >= 1")
        order = self.topological_order()
        rev = self.dependents()
        rank: dict[str, float] = {}
        for n in reversed(order):
            rank[n] = self.duration[n] + max((rank[m] for m in rev[n]), default=0.0)
        remaining = {n: len(ds) for n, ds in self.deps.items()}
        ready = [(-rank[n], n) for n in order if remaining[n] == 0]
        heapq.heapify(ready)
        running: list[tuple[float, str]] = []
        times: dict[str, tuple[float, float]] = {}
        now = 0.0
        while ready or running:
            while ready and len(running) < workers:
                _, n = heapq.heappop(ready)
                times[n] = (now, now + self.duration[n])
                heapq.heappush(running, (now + self.duration[n], n))
            now, done = heapq.heappop(running)
            finished = [done]
            while running and running[0][0] == now:      # everything finishing at the same instant
                finished.append(heapq.heappop(running)[1])
            for f in finished:
                for m in rev[f]:
                    remaining[m] -= 1
                    if remaining[m] == 0:
                        heapq.heappush(ready, (-rank[m], m))
        return max(e for _, e in times.values()), times

    def lower_bound(self, workers: int) -> float:
        return max(self.critical_path()[0], sum(self.duration.values()) / workers)

    # ---------------------------------------------------------------- impact
    def blast_radius(self, changed: list[str]) -> list[str]:
        """Every service that (transitively) depends on a changed one, in BFS order by distance."""
        rev = self.dependents()
        seen = set(changed)
        q = deque(changed)
        out = []
        while q:
            n = q.popleft()
            for m in sorted(rev[n]):
                if m not in seen:
                    seen.add(m)
                    out.append(m)
                    q.append(m)
        return out

    def redundant_edges(self) -> list[tuple[str, str]]:
        """Edges A->B where A also reaches B through another dependency (removable without changing order)."""
        reach: dict[str, set[str]] = {}
        for n in self.topological_order():
            r: set[str] = set()
            for d in self.deps[n]:
                r |= {d} | reach[d]
            reach[n] = r
        out = []
        for a, ds in self.deps.items():
            for b in ds:
                if any(b in reach[c] for c in ds if c != b):
                    out.append((a, b))
        return sorted(out)
