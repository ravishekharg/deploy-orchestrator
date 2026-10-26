"""Generate a realistic layered microservice estate (services.yaml + Kubernetes manifests) for demos and e2e."""
from __future__ import annotations

import pathlib
import random

import yaml

# tier name, how many services, deploy seconds range
TIERS = [
    ("data", ["postgres", "redis", "kafka", "elasticsearch"], (120, 240)),
    ("platform", ["config", "auth", "feature-flags", "audit"], (40, 80)),
    ("core", ["users", "catalog", "inventory", "pricing", "payments", "shipping", "notifications", "search"],
     (50, 110)),
    ("domain", ["cart", "orders", "checkout", "recommendations", "reviews", "loyalty", "returns", "invoicing",
                "fraud", "reporting"], (40, 90)),
    ("edge", ["web-bff", "mobile-bff", "partner-api", "admin-api"], (30, 60)),
]


def estate(seed: int = 11) -> dict[str, dict]:
    rnd = random.Random(seed)
    services: dict[str, dict] = {}
    lower: list[str] = []
    for tier, names, (lo, hi) in TIERS:
        for n in names:
            deps = sorted(rnd.sample(lower, k=min(len(lower), rnd.randint(1, 3)))) if lower else []
            services[n] = {"depends_on": deps, "deploy_seconds": rnd.randint(lo, hi), "tier": tier}
        lower += names
    return services


def manifests(services: dict[str, dict], out: pathlib.Path, namespace: str = "shop") -> None:
    """One file per service: Deployment (http-echo) + Service. Init containers wait until each dependency
    answers, so a wrong deploy order would show up as pods stuck in Init."""
    out.mkdir(parents=True, exist_ok=True)
    for name, spec in services.items():
        deps = spec["depends_on"]
        wait = [{
            "name": f"wait-{d}", "image": "busybox:1.37",
            "command": ["sh", "-c", f"until wget -q -T 2 -O /dev/null http://{d}:5678; do sleep 1; done"],
            "resources": {"requests": {"cpu": "5m", "memory": "8Mi"}},
        } for d in deps]
        env = [{"name": f"{d.upper().replace('-', '_')}_URL", "value": f"http://{d}.{namespace}.svc.cluster.local:5678"}
               for d in deps]
        deployment = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": namespace, "labels": {"app": name, "tier": spec["tier"]},
                         "annotations": {"orch/deploy-seconds": str(spec["deploy_seconds"])}},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}}, "spec": {
                         "initContainers": wait,
                         "containers": [{"name": "app", "image": "hashicorp/http-echo:1.0",
                                         "args": ["-text", f"{name} ok", "-listen", ":5678"], "env": env,
                                         "ports": [{"containerPort": 5678}],
                                         "readinessProbe": {"httpGet": {"path": "/", "port": 5678},
                                                            "periodSeconds": 2},
                                         "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                                                       "limits": {"memory": "32Mi"}}}]}}},
        }
        service = {"apiVersion": "v1", "kind": "Service",
                   "metadata": {"name": name, "namespace": namespace},
                   "spec": {"selector": {"app": name}, "ports": [{"port": 5678, "targetPort": 5678}]}}
        (out / f"{name}.yaml").write_text(yaml.safe_dump_all([deployment, service], sort_keys=False), encoding="utf-8")
