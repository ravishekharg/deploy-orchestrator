import json

import yaml

from orch import generate, k8s
from orch.cli import main


def dep(name, env=(), args=(), ann=None):
    return {"kind": "Deployment", "metadata": {"name": name, "annotations": ann or {}},
            "spec": {"template": {"spec": {"containers": [
                {"name": "app", "env": [{"name": f"E{i}", "value": v} for i, v in enumerate(env)],
                 "args": list(args)}]}}}}


def svc(name):
    return {"kind": "Service", "metadata": {"name": name}}


def test_infer_dependencies_from_env_args_and_annotations():
    docs = [
        dep("orders", env=["http://payments.shop.svc.cluster.local:8080", "postgres://app:pw@postgres:5432/orders"],
            args=["--cache=redis:6379"]),
        dep("payments", env=["https://stripe.example.com"],
            ann={"orch/depends-on": "audit", "orch/deploy-seconds": "90"}),
        dep("audit"), dep("postgres"), dep("redis"),
        svc("orders"), svc("payments"), svc("audit"), svc("postgres"), svc("redis"),
    ]
    g = k8s.infer_graph(docs)
    assert g.deps["orders"] == {"payments", "postgres", "redis"}
    assert g.deps["payments"] == {"audit"} and g.duration["payments"] == 90.0
    assert g.waves() == [["audit", "postgres", "redis"], ["payments"], ["orders"]]


def test_generated_estate_round_trips_through_manifests(tmp_path):
    services = generate.estate(seed=11)
    generate.manifests(services, tmp_path / "k8s")
    g = k8s.infer_graph(k8s.load_docs([tmp_path / "k8s"]))
    assert {n: set(s["depends_on"]) for n, s in services.items()} == g.deps
    assert len(g.deps) == 30


def test_argocd_annotations_follow_waves(tmp_path):
    services = generate.estate(seed=3)
    generate.manifests(services, tmp_path / "k8s")
    assert main(["argocd", str(tmp_path / "k8s"), "--out", str(tmp_path / "out")]) == 0
    docs = list(yaml.safe_load_all((tmp_path / "out" / "all.yaml").read_text()))
    wave = {d["metadata"]["name"]: int(d["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])
            for d in docs if d["kind"] == "Deployment"}
    for name, spec in services.items():
        assert all(wave[d] < wave[name] for d in spec["depends_on"])


def test_cli_plan_check_impact_matrix(tmp_path, capsys):
    assert main(["generate", "--out", str(tmp_path)]) == 0
    src = str(tmp_path / "services.yaml")
    assert main(["plan", src, "--workers", "6", "--out", str(tmp_path / "plan.md")]) == 0
    text = (tmp_path / "plan.md").read_text()
    assert "Critical path" in text and "gantt" in text
    assert main(["check", src]) == 0
    capsys.readouterr()
    assert main(["impact", src, "--changed", "postgres"]) == 0
    impact = json.loads(capsys.readouterr().out)
    assert impact["count"] > 5
    assert main(["matrix", src]) == 0
    matrix = json.loads(capsys.readouterr().out)
    assert sum(len(w["include"]) for w in matrix) == 30


def test_cli_check_fails_on_cycle(tmp_path):
    f = tmp_path / "s.yaml"
    f.write_text(yaml.safe_dump({"services": {"a": {"depends_on": ["b"]}, "b": {"depends_on": ["a"]}}}))
    assert main(["check", str(f)]) == 1
    assert main(["plan", str(f)]) == 1
