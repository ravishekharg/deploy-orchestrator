"""Markdown release plan with a Mermaid Gantt chart."""
from __future__ import annotations

from orch.graph import Graph


def _fmt(sec: float) -> str:
    return f"{int(sec // 60)}m{int(sec % 60):02d}s"


def markdown(g: Graph, workers: int) -> str:
    waves = g.waves()
    cp_len, cp = g.critical_path()
    makespan, times = g.schedule(workers)
    lines = [
        "# Release plan", "",
        f"{len(g.deps)} services, {sum(len(d) for d in g.deps.values())} dependencies, {len(waves)} waves.", "",
        "| | Release time |", "|---|---|",
        f"| Sequential (one at a time) | {_fmt(sum(g.duration.values()))} |",
        f"| Sync waves (each wave waits for its slowest service) | {_fmt(g.wave_makespan())} |",
        f"| Critical-path scheduling, {workers} parallel slots | **{_fmt(makespan)}** |",
        f"| Lower bound for {workers} slots (critical path vs total work / slots) | {_fmt(g.lower_bound(workers))} |",
        "", f"**Critical path ({_fmt(cp_len)}):** " + " -> ".join(cp), "",
        "## Waves", "", "| Wave | Services |", "|---|---|",
    ]
    lines += [f"| {i} | {', '.join(w)} |" for i, w in enumerate(waves)]
    redundant = g.redundant_edges()
    if redundant:
        lines += ["", "**Redundant declared dependencies** (implied by others): "
                  + ", ".join(f"{a} -> {b}" for a, b in redundant)]
    lines += ["", f"## Schedule ({workers} slots)", "", "```mermaid", "gantt", "  dateFormat X", "  axisFormat %M:%S"]
    for i, w in enumerate(waves):
        lines.append(f"  section wave {i}")
        for n in w:
            s, e = times[n]
            crit = "crit, " if n in cp else ""
            lines.append(f"  {n} :{crit}{int(s)}, {int(e)}")
    lines.append("```")
    return "\n".join(lines) + "\n"
