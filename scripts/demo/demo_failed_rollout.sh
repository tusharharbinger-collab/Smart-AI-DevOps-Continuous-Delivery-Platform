#!/usr/bin/env bash
# scripts/demo/demo_failed_rollout.sh
#
# Deliberately breaks the live Kind canary (payment-service-canary) so the
# next rollout you trigger from the UI produces a genuine FAILED verdict and
# a real autonomous rollback — not a mocked one. Flips the v1.1.0 sample-app's
# own INJECT_ERRORS toggle (see services/sample-app/v1.1.0/main.py): a real
# 2.0% error rate + 65ms latency spike replaces its default healthy behavior
# (0.5% errors, 42ms), which is exactly what SPRT's Bernoulli log-likelihood
# ratio and CUSUM are built to catch.
#
# This script ONLY breaks the canary — it does not trigger a rollout itself.
# Trigger the rollout from the platform UI (Trigger New Rollout) so the demo
# audience sees a real click start it, not a script.
set -euo pipefail

CONTEXT="kind-smartcd-local"
NAMESPACE="production"
DEPLOYMENT="payment-service-canary"

echo "💥 Breaking the live canary (${DEPLOYMENT} in ${NAMESPACE})..."
echo "   Setting INJECT_ERRORS=true — real 2.0% error rate + 65ms latency spike."

if ! kubectl cluster-info --context "${CONTEXT}" >/dev/null 2>&1; then
  echo "❌ Kind cluster '${CONTEXT}' is not reachable. Run 'make kind-up' first." >&2
  exit 1
fi

kubectl set env "deployment/${DEPLOYMENT}" -n "${NAMESPACE}" --context "${CONTEXT}" \
  INJECT_ERRORS=true

echo "⏳ Waiting for the canary pod to restart with the broken build..."
kubectl rollout status "deployment/${DEPLOYMENT}" -n "${NAMESPACE}" --context "${CONTEXT}" --timeout=60s

echo "🔎 Confirming the break actually took effect..."
LIVE_ENV=$(kubectl get "deployment/${DEPLOYMENT}" -n "${NAMESPACE}" --context "${CONTEXT}" \
  -o jsonpath='{.spec.template.spec.containers[0].env}')
if [[ "${LIVE_ENV}" != *'"name":"INJECT_ERRORS","value":"true"'* ]]; then
  echo "❌ INJECT_ERRORS did not take effect. Live env: ${LIVE_ENV}" >&2
  exit 1
fi
echo "   INJECT_ERRORS=true confirmed on the live Deployment spec."

echo ""
echo "✅ Canary is broken and ready."
echo ""
echo "Next steps for the demo:"
echo "  1. In the UI, click 'Trigger New Rollout' on the payments project."
echo "  2. Watch the kubectl httproute -w terminal: weight should advance to the"
echo "     first canary step, then the verification engine's SPRT/CUSUM should"
echo "     reject the null hypothesis within that step's observation window."
echo "  3. Verification Inspector should show a FAILED verdict; the HTTPRoute"
echo "     weight should be patched back to 0% canary automatically — no manual"
echo "     rollback click needed."
echo ""
echo "When you're done, restore the healthy canary with:"
echo "  bash scripts/demo/demo_healthy_rollout.sh"
