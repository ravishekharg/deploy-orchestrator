#!/usr/bin/env bash
# End to end on kind: deploy a 30-service estate in dependency waves (orch deploy), then the same estate all at
# once in another namespace, and compare. Init containers really wait for each dependency over HTTP.
set -euo pipefail
cd "$(dirname "$0")/.."

orch generate --out estate --namespace shop
orch generate --out estate-bigbang --namespace shop-bigbang
orch check estate/services.yaml
orch plan estate/k8s --workers 6 --out plan.md > /dev/null

# images are pulled once up front so pull time does not distort either run
for img in hashicorp/http-echo:1.0 busybox:1.37; do docker pull -q "$img" && ./kind load docker-image "$img"; done

kubectl create namespace shop
orch deploy estate/k8s --namespace shop --timeout 300 --out deploy-result.json

kubectl create namespace shop-bigbang
start=$(date +%s)
kubectl apply -n shop-bigbang -f estate-bigbang/k8s/ > /dev/null
for d in $(kubectl -n shop-bigbang get deploy -o name); do
  kubectl -n shop-bigbang rollout status "$d" --timeout=600s > /dev/null
done
bigbang=$(( $(date +%s) - start ))

python e2e/verify.py estate/services.yaml deploy-result.json "$bigbang"

# negative control: a cycle must fail the CI gate
printf 'services:\n  a: {depends_on: [b]}\n  b: {depends_on: [c]}\n  c: {depends_on: [a]}\n' > cyclic.yaml
if orch check cyclic.yaml; then echo "FAIL: cycle not detected"; exit 1; else echo "PASS: cycle detected and rejected"; fi
