"""orch: order, schedule and deploy many microservices from their dependency graph.

  orch plan services.yaml --workers 6                 # waves, critical path, schedule (Markdown + Gantt)
  orch plan k8s/ --infer                              # build the graph from Kubernetes manifests
  orch check services.yaml                            # exit 1 on cycles (CI gate)
  orch impact services.yaml --changed payments        # blast radius: what to retest and redeploy
  orch argocd k8s/ --out annotated/                   # add argocd.argoproj.io/sync-wave annotations
  orch matrix services.yaml                           # CI matrix: one job list per wave (JSON)
  orch deploy k8s/ --namespace shop                   # kubectl apply wave by wave, wait for rollouts
  orch generate --out estate/                         # sample 30-service estate (services.yaml + manifests)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import yaml

from orch import generate, k8s
from orch.graph import CycleError, Graph
from orch.report import markdown


def _graph(source: str, infer: bool) -> Graph:
    p = pathlib.Path(source)
    if infer or p.is_dir():
        return k8s.infer_graph(k8s.load_docs([p]))
    return Graph.from_dict(yaml.safe_load(p.read_text(encoding="utf-8"))["services"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orch", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "check", "impact", "matrix"):
        s = sub.add_parser(name)
        s.add_argument("source", help="services.yaml or a directory of Kubernetes manifests")
        s.add_argument("--infer", action="store_true", help="treat source as Kubernetes manifests")
        if name == "plan":
            s.add_argument("--workers", type=int, default=6)
            s.add_argument("--out")
        if name == "impact":
            s.add_argument("--changed", required=True, help="comma-separated service names")
    a_cd = sub.add_parser("argocd")
    a_cd.add_argument("source")
    a_cd.add_argument("--out", required=True)
    dep = sub.add_parser("deploy")
    dep.add_argument("source", help="directory with one <service>.yaml per service")
    dep.add_argument("--namespace", default="default")
    dep.add_argument("--timeout", type=int, default=300)
    dep.add_argument("--out", default="deploy-result.json")
    gen = sub.add_parser("generate")
    gen.add_argument("--out", default="estate")
    gen.add_argument("--seed", type=int, default=11)
    gen.add_argument("--namespace", default="shop")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "generate":
            services = generate.estate(a.seed)
            out = pathlib.Path(a.out)
            generate.manifests(services, out / "k8s", a.namespace)
            (out / "services.yaml").write_text(yaml.safe_dump({"services": services}, sort_keys=False),
                                               encoding="utf-8")
            print(f"wrote {len(services)} services to {out}/services.yaml and {out}/k8s/")
            return 0
        if a.cmd == "argocd":
            docs = k8s.load_docs([pathlib.Path(a.source)])
            g = k8s.infer_graph(docs)
            out = pathlib.Path(a.out)
            out.mkdir(parents=True, exist_ok=True)
            (out / "all.yaml").write_text(yaml.safe_dump_all(k8s.annotate_sync_waves(docs, g), sort_keys=False),
                                          encoding="utf-8")
            print(f"annotated {len(g.deps)} services in {len(g.waves())} waves -> {out}/all.yaml")
            return 0
        if a.cmd == "deploy":
            src = pathlib.Path(a.source)
            g = k8s.infer_graph(k8s.load_docs([src]))
            result = k8s.deploy_waves(g, src, a.namespace, a.timeout)
            result["predicted"] = {"waves": len(g.waves()), "order": g.waves()}
            pathlib.Path(a.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps({"total_seconds": result["total_seconds"], "waves": len(g.waves())}))
            return 0

        g = _graph(a.source, a.infer)
        if a.cmd == "check":
            cycles = g.cycles()
            for c in cycles:
                print("cycle: " + " -> ".join(c + [c[0]]))
            print(f"{len(g.deps)} services, {'no cycles' if not cycles else f'{len(cycles)} cycle(s)'}")
            return 1 if cycles else 0
        if a.cmd == "plan":
            text = markdown(g, a.workers)
            if a.out:
                pathlib.Path(a.out).write_text(text, encoding="utf-8")
            sys.stdout.write(text)
            return 0
        if a.cmd == "impact":
            changed = [c.strip() for c in a.changed.split(",")]
            radius = g.blast_radius(changed)
            print(json.dumps({"changed": changed, "affected": radius, "count": len(radius)}, indent=2))
            return 0
        if a.cmd == "matrix":
            print(json.dumps([{"wave": i, "include": [{"service": s} for s in w]} for i, w in enumerate(g.waves())]))
            return 0
    except CycleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
