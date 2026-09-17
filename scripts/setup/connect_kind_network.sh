#!/usr/bin/env bash
# scripts/setup/connect_kind_network.sh
#
# Wires the docker-compose control-plane services to a real Kind cluster:
#   1. Generates k8s/generated/incluster-kubeconfig.yaml — a copy of the
#      current kubeconfig with the API server address rewritten from
#      `127.0.0.1:<port>` (only reachable from the host) to the Kind
#      control-plane container's name (reachable from any container on the
#      `kind` docker network).
#   2. Attaches the running pipeline-worker and policy-controller containers
#      to the `kind` network so they can actually reach that address.
#
# Run this AFTER `make kind-up` (or `kind create cluster --config
# k8s/kind-config.yaml --name smartcd-local`) and AFTER `docker compose up`
# has started pipeline-worker/policy-controller at least once.
set -euo pipefail

CLUSTER_NAME="${KIND_CLUSTER_NAME:-smartcd-local}"
CONTROL_PLANE="${CLUSTER_NAME}-control-plane"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${REPO_ROOT}/k8s/generated/incluster-kubeconfig.yaml"

mkdir -p "${REPO_ROOT}/k8s/generated"

echo "Generating in-cluster kubeconfig -> ${OUT}"
kubectl config view --raw --minify --flatten \
  --context "kind-${CLUSTER_NAME}" \
  | sed -E "s#server: https://127\.0\.0\.1:[0-9]+#server: https://${CONTROL_PLANE}:6443#" \
  > "${OUT}"

for svc in pipeline-worker policy-controller; do
  # Real gap found live: this used to guess the container name by
  # concatenating the repo directory name (e.g.
  # "smartaidevopscontinuousdeliveryplatform-pipeline-worker-1"), but Docker
  # Compose's actual naming depends on how it normalizes the project name —
  # here it keeps hyphens ("smart-ai-devops-continuous-delivery-platform-
  # pipeline-worker-1"), so the guess never matched and every run silently
  # printed the "not found" warning. Ask Compose directly instead of
  # guessing — it always knows its own project's real container names.
  container="$(cd "${REPO_ROOT}" && docker compose ps -q "${svc}" 2>/dev/null)"
  if [ -n "${container}" ]; then
    if docker network connect kind "${container}" 2>/dev/null; then
      echo "Connected ${svc} (${container}) to the kind network"
    else
      echo "${svc} (${container}) already on the kind network (or connect failed — check manually)"
    fi
  else
    echo "WARNING: ${svc} container not found — start it with 'docker compose up -d ${svc}' first"
  fi
done

echo
echo "Restart pipeline-worker and policy-controller to pick up the new kubeconfig:"
echo "  docker compose restart pipeline-worker policy-controller"
echo "Then verify: curl localhost:8001/readyz   (should show \"kubernetes\": \"ok\")"
