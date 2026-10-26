"""Verify the kind deployment and compare ordered (waves) vs big-bang (everything at once).

  python e2e/verify.py estate/services.yaml deploy-result.json bigbang-seconds
"""
import datetime as dt
import json
import subprocess
import sys

import yaml


def init_waits(namespace: str) -> dict[str, float]:
    """Seconds each pod spent in its init containers (waiting for dependencies to answer)."""
    pods = json.loads(subprocess.check_output(["kubectl", "-n", namespace, "get", "pods", "-o", "json"]))["items"]
    out = {}
    for p in pods:
        app = p["metadata"]["labels"]["app"]
        total = 0.0
        for s in p["status"].get("initContainerStatuses", []) or []:
            t = s.get("state", {}).get("terminated")
            if t:
                f = dt.datetime.fromisoformat(t["finishedAt"].replace("Z", "+00:00"))
                b = dt.datetime.fromisoformat(t["startedAt"].replace("Z", "+00:00"))
                total += (f - b).total_seconds()
        out[app] = total
    return out


def main() -> None:
    services = yaml.safe_load(open(sys.argv[1]))["services"]
    result = json.load(open(sys.argv[2]))
    bigbang_seconds = float(sys.argv[3])
    run = result["services"]

    order_ok = all(run[n]["start"] >= max((run[d]["ready"] for d in s["depends_on"]), default=0) - 1e-6
                   for n, s in services.items())
    waves_init = init_waits("shop")
    bang_init = init_waits("shop-bigbang")
    summary = {
        "services": len(services),
        "waves": result["predicted"]["waves"],
        "ordered": {"total_seconds": result["total_seconds"],
                    "pods_waiting_over_2s": sum(v > 2 for v in waves_init.values()),
                    "max_init_wait_s": round(max(waves_init.values()), 1)},
        "big_bang": {"total_seconds": bigbang_seconds,
                     "pods_waiting_over_2s": sum(v > 2 for v in bang_init.values()),
                     "max_init_wait_s": round(max(bang_init.values()), 1),
                     "sum_init_wait_s": round(sum(bang_init.values()), 1)},
        "checks": {
            "every service started after all its dependencies were Ready": order_ok,
            "all 30 services Ready in the ordered namespace": len(waves_init) == len(services),
            "ordered deploy: no pod had to wait for a dependency": all(v <= 2 for v in waves_init.values()),
        },
    }
    summary["passed"] = all(summary["checks"].values())
    json.dump(summary, open("e2e-summary.json", "w"), indent=2)
    print(json.dumps(summary, indent=2))
    sys.exit(0 if summary["passed"] else 1)


if __name__ == "__main__":
    main()
