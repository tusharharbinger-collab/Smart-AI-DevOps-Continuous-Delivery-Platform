#!/usr/bin/env bash
# scripts/setup/connect_eks.sh
#
# Confirms pipeline-worker/policy-controller can reach the real EKS cluster
# via shared/eks_auth.py's IAM-based auth (EKS_CLUSTER_NAME/AWS_REGION/
# AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY in .env) — no kubeconfig file or
# Docker-network trick needed this time, unlike connect_kind_network.sh:
# EKS has a real reachable API endpoint, not a host-only address.
set -euo pipefail

echo "Checking pipeline-worker can reach the real EKS cluster..."
curl -sf http://localhost:8001/readyz | grep -q '"kubernetes":"ok"' \
  && echo "OK — pipeline-worker is authenticated against EKS." \
  || echo "NOT READY — check EKS_CLUSTER_NAME/AWS_REGION/AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY in .env, then: docker compose up -d pipeline-worker policy-controller"
