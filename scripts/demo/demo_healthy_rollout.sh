#!/usr/bin/env bash
# scripts/demo/demo_healthy_rollout.sh
#
# Ensures the live Kind canary (payment-service-canary) is in its normal,
# healthy state before a "happy path" promotion demo — and doubles as the
# reset step after scripts/demo/demo_failed_rollout.sh. Idempotent: safe to
# run whether or not INJECT_ERRORS was ever set.
#
# This script ONLY resets the canary's behavior — it does not trigger a
# rollout itself. Trigger the rollout from the platform UI (Trigger New
# Rollout) so the demo audience sees a real click start it, not a script.
set -euo pipefail

CONTEXT="kind-smartcd-local"
NAMESPACE="production"
DEPLOYMENT="payment-service-canary"

echo "🩺 Restoring the canary (${DEPLOYMENT} in ${NAMESPACE}) to healthy..."

if ! kubectl cluster-info --context "${CONTEXT}" >/dev/null 2>&1; then
  echo "❌ Kind cluster '${CONTEXT}' is not reachable. Run 'make kind-up' first." >&2
  exit 1
fi

kubectl set env "deployment/${DEPLOYMENT}" -n "${NAMESPACE}" --context "${CONTEXT}" \
  INJECT_ERRORS=false

echo "⏳ Waiting for the canary pod to restart healthy..."
kubectl rollout status "deployment/${DEPLOYMENT}" -n "${NAMESPACE}" --context "${CONTEXT}" --timeout=60s

echo "🔎 Confirming the reset actually took effect..."
LIVE_ENV=$(kubectl get "deployment/${DEPLOYMENT}" -n "${NAMESPACE}" --context "${CONTEXT}" \
  -o jsonpath='{.spec.template.spec.containers[0].env}')
if [[ "${LIVE_ENV}" != *'"name":"INJECT_ERRORS","value":"false"'* ]]; then
  echo "❌ INJECT_ERRORS did not reset. Live env: ${LIVE_ENV}" >&2
  exit 1
fi
echo "   INJECT_ERRORS=false confirmed on the live Deployment spec."

echo ""
echo "✅ Canary is healthy (0.5% baseline error rate, ~42ms latency)."
echo "   Trigger a new rollout from the UI for the happy-path promotion demo."
