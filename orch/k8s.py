"""Kubernetes side: infer the dependency graph from manifests, annotate Argo CD sync waves, deploy wave by wave.

Inference: a workload depends on another service when its container env values or args mention that Service's
DNS name (`payments`, `payments.shop`, `payments.shop.svc.cluster.local`, with or without scheme/port).
Annotations override or add:  orch/depends-on: "a,b"   orch/deploy-seconds: "90"
"""
from __future__ import annotations

import concurrent.futures as cf
import pathlib
import re
import subprocess
import time
from collections.abc import Iterable

import yaml

from orch.graph import Graph

WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet"}
HOST = re.compile(r"(?<![\w.-])([a-z0-9]([-a-z0-9]*[a-z0-9])?)(?:\.[a-z0-9-]+)*(?::\d+)?")


def load_docs(paths: Iterable[pathlib.Path]) -> list[dict]:
    docs = []
    for p in paths:
        files = sorted(p.rglob("*.yaml")) + sorted(p.rglob("*.yml")) if p.is_dir() else [p]
        for f in files:
            docs += [d for d in yaml.safe_load_all(f.read_text(encoding="utf-8")) if d]
    return docs


def _strings(container: dict) -> list[str]:
    out = [e.get("value", "") for e in container.get("env", []) or [] if isinstance(e.get("value"), str)]
    return out + [a for a in (container.get("args") or []) + (container.get("command") or []) if isinstance(a, str)]


def infer_graph(docs: list[dict]) -> Graph:
    workloads = {d["metadata"]["name"]: d for d in docs if d.get("kind") in WORKLOADS}
    services = {d["metadata"]["name"] for d in docs if d.get("kind") == "Service"}
    known = services | set(workloads)
    g = Graph()
    for name, w in workloads.items():
        ann = w["metadata"].get("annotations") or {}
        deps: set[str] = {x.strip() for x in ann.get("orch/depends-on", "").split(",") if x.strip()}
        spec = w["spec"]["template"]["spec"]
        for c in (spec.get("containers") or []) + (spec.get("initContainers") or []):
            for s in _strings(c):
                for m in HOST.finditer(s.split("://", 1)[-1]):
                    host = m.group(1)
                    if host in known and host != name:
                        deps.add(host)
        g.add(name, deps & set(workloads), float(ann.get("orch/deploy-seconds", 60)))
    return g


def annotate_sync_waves(docs: list[dict], g: Graph) -> list[dict]:
    """Copy of the manifests with argocd.argoproj.io/sync-wave on every workload and its Service."""
    wave_of = {n: i for i, w in enumerate(g.waves()) for n in w}
    out = []
    for d in docs:
        d = yaml.safe_load(yaml.safe_dump(d))                        # deep copy
        name = d.get("metadata", {}).get("name")
        if d.get("kind") in WORKLOADS | {"Service"} and name in wave_of:
            d["metadata"].setdefault("annotations", {})["argocd.argoproj.io/sync-wave"] = str(wave_of[name])
        out.append(d)
    return out


def deploy_waves(g: Graph, manifest_dir: pathlib.Path, namespace: str, timeout: int = 300) -> dict:
    """kubectl apply one wave at a time; within a wave, wait for all rollouts in parallel."""
    results: dict[str, dict] = {}
    t0 = time.monotonic()

    def rollout(svc: str) -> tuple[str, float, float]:
        start = time.monotonic() - t0
        subprocess.run(["kubectl", "apply", "-n", namespace, "-f", str(manifest_dir / f"{svc}.yaml")],
                       check=True, capture_output=True)
        subprocess.run(["kubectl", "-n", namespace, "rollout", "status", f"deploy/{svc}", f"--timeout={timeout}s"],
                       check=True, capture_output=True)
        return svc, start, time.monotonic() - t0

    for i, wave in enumerate(g.waves()):
        with cf.ThreadPoolExecutor(max_workers=len(wave)) as pool:
            for svc, start, end in pool.map(rollout, wave):
                results[svc] = {"wave": i, "start": round(start, 2), "ready": round(end, 2)}
    return {"services": results, "total_seconds": round(time.monotonic() - t0, 2)}
