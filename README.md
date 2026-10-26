# deploy-orchestrator

`orch` turns a microservice estate into a **safe, fast release plan** using graph algorithms, then deploys it to
Kubernetes in that order. It answers the questions every team releasing dozens of services runs into:
*Is there a circular dependency? What can go in parallel? What is the fastest possible release? If I change
`payments`, what else must be retested?*

The dependency graph comes from a `services.yaml` or is **inferred from Kubernetes manifests** (env vars and args
that reference another Service's DNS name, plus `orch/depends-on` annotations).

| Question | Algorithm | Complexity |
|---|---|---|
| Circular dependencies, and the exact cycle | **Tarjan's strongly connected components** (iterative) | O(V + E) |
| Deploy order and parallel waves | **Kahn's topological sort** by levels (wave = longest dependency chain) | O(V + E) |
| Fastest possible release | **critical path**: dynamic programming over the topological order | O(V + E) |
| Fastest release with N parallel deploy slots | **list scheduling**, priority = longest remaining path (max-heap of ready services, min-heap of running ones) | O((V + E) log V) |
| What does a change affect? | **BFS on the reversed graph** (blast radius) | O(V + E) |
| Which declared dependencies are redundant? | **transitive reduction** via reachability sets | O(V·(V + E)) |

## Results

Measured in CI (GitHub-hosted `ubuntu-24.04`, kind with 1 control plane + 2 workers; every run repeats it).

**Release plan for the sample estate** (30 services, 50 dependencies, deploy times 30–240 s):

| Strategy | Release time |
|---|---|
| One service at a time | 42m54s |
| Plain sync waves (each wave waits for its slowest service) | 9m19s |
| **Critical-path list scheduling, 6 parallel slots** | **8m48s** |
| Lower bound for 6 slots | 8m09s |

The plan also flagged 10 declared dependencies that are already implied by others (e.g. `orders -> auth` when
`orders` already reaches `auth` through another service). Full report with Gantt chart:
[`examples/release-plan.md`](examples/release-plan.md).

**Real deployment on Kubernetes:** 30 services whose init containers block until every dependency answers over
HTTP.

| | Total time | Pods that had to wait for a dependency | Longest wait | Pod time spent blocked |
|---|---|---|---|---|
| **`orch deploy`** (5 waves, rollouts in parallel within a wave) | 41 s | **0 of 30** | 1 s | — |
| Everything applied at once | 48 s | 24 of 30 | 38 s | 572 s |

All checks passed: every service started only after all its dependencies were Ready, no pod waited in the ordered
run, and a cyclic `services.yaml` was rejected by `orch check`. With real services the difference is larger than
here: a pod that starts before its database usually crash-loops and backs off instead of waiting politely, and
the big-bang release takes as long as the slowest chain of back-offs.

## Outputs

```bash
orch generate --out estate                              # sample 30-service estate (services.yaml + manifests)
orch check estate/services.yaml                         # CI gate: exit 1 and print the cycle
orch plan estate/k8s --workers 6                        # waves, critical path, schedule, Gantt chart (Markdown)
orch impact estate/services.yaml --changed postgres     # blast radius
orch argocd estate/k8s --out annotated/                 # argocd.argoproj.io/sync-wave on every workload + Service
orch matrix estate/services.yaml                        # CI matrix: one job list per wave
orch deploy estate/k8s --namespace shop                 # kubectl apply wave by wave, rollouts in parallel per wave
```

- **Argo CD:** the annotated manifests make Argo CD sync in dependency order (each wave must be healthy before the
  next starts).
- **CI fan-out:** the workflow builds its job matrix from `orch matrix`; the `build-wave` jobs run wave by wave.
- **Direct deploy:** `orch deploy` applies each wave and waits for all of its rollouts in parallel.

## Tests

- Property-based tests against **brute-force references** on random graphs: Tarjan's SCCs equal mutual
  reachability, every reported cycle is real, waves respect every edge and place each service as early as
  possible, the critical path equals the best chain found by exhaustive enumeration, the schedule never runs more
  than N deploys at once and never starts a service before its dependencies finish, and it sits between the lower
  bound and the sequential time (and is optimal when slots are unlimited). Removing the reported redundant edges
  leaves reachability and waves unchanged.
- Manifest inference round-trips: the generated estate's declared dependencies are recovered exactly from its
  Kubernetes YAML.
- **End to end on kind:** 30 real services (each with init containers that wait until its dependencies answer
  over HTTP) deployed by `orch deploy`, then the same estate applied all at once in another namespace for
  comparison. A cyclic input must fail the gate.
