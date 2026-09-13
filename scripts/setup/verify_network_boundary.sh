#!/usr/bin/env bash
# scripts/setup/verify_network_boundary.sh
#
# Phase 4 (docs/roadmap/04-security-hardening.md, deliverable 4.5): a live,
# repeatable check of the "verification-engine cannot touch Kubernetes"
# invariant (CLAUDE.md invariant #3) against the REAL running stack + real
# Kind cluster — not just the static AST checks in
# tests/adversarial/test_verification_engine_k8s_boundary.py.
#
# Requires: docker compose up (this stack) and make kind-up/deploy-sample-app
# (the real Kind cluster) both already running.
set -euo pipefail

CONTROL_PLANE_IP=$(docker inspect smartcd-local-control-plane --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null || true)
if [ -z "$CONTROL_PLANE_IP" ]; then
  echo "Kind cluster's control-plane container not found — run 'make kind-up' first." >&2
  exit 1
fi

echo "== verification-engine has no kubernetes client library installed =="
if docker compose exec -T verification-engine pip show kubernetes >/dev/null 2>&1; then
  echo "FAIL: kubernetes package IS installed in verification-engine" >&2
  exit 1
fi
echo "OK: no kubernetes package installed"

echo "== verification-engine has no kubeconfig mounted =="
if docker compose exec -T verification-engine sh -c 'test -d /root/.kube || test -n "${KUBECONFIG:-}"'; then
  echo "FAIL: verification-engine has a kubeconfig path available" >&2
  exit 1
fi
echo "OK: no kubeconfig present"

echo "== an actual API call from verification-engine authenticates as system:anonymous and is rejected =="
RESPONSE=$(docker compose exec -T verification-engine python -c "
import httpx
r = httpx.get('https://${CONTROL_PLANE_IP}:6443/api/v1/namespaces', timeout=3, verify=False)
print(r.status_code)
")
if [ "$(echo "$RESPONSE" | tr -d '\r\n')" != "403" ]; then
  echo "FAIL: expected 403 Forbidden (anonymous, no credentials), got: $RESPONSE" >&2
  exit 1
fi
echo "OK: rejected 403 as system:anonymous — no credentials means no ability to act, regardless of raw network reachability"

echo
echo "All network-boundary checks passed."
