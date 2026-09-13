"""
services/sample-app/v1.1.0/main.py
Canary payments-service (v1.1.0).
Normal mode: healthy (same stats as baseline → no regression detected → PROMOTE).
INJECT_ERRORS=true mode: 3× error rate spike → SPRT rejects H0 → ROLLBACK within 30s.

Exposes real Prometheus metrics at /metrics (spec §Phase 2 — Real Telemetry):
request count by outcome and a latency histogram, both labeled with this
instance's `cohort` (baseline/canary) so verification-engine's PromQL
queries can compare the two cohorts directly, instead of receiving
synthesized numbers standing in for real traffic.
"""
import os
import time
import random
import uvicorn
from fastapi import FastAPI, Response
from fastapi.responses import PlainTextResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

app = FastAPI(title="payments-service", version="1.1.0")

INJECT_ERRORS = os.environ.get("INJECT_ERRORS", "false").lower() == "true"
DEPLOYMENT_COHORT = os.environ.get("DEPLOYMENT_COHORT", "canary")
APP_VERSION = os.environ.get("APP_VERSION", "v1.1.0")

# No `cohort` label here — Prometheus's scrape config (monitoring/prometheus.yml)
# attaches `cohort` externally per-target, which is the authoritative source
# (an app instance shouldn't need to self-report which cohort it's in for
# its own metrics; that's an infra-level concern). Baking a same-named label
# in here would make Prometheus rename it to `exported_cohort` on scrape.
REQUEST_COUNT = Counter(
    "payments_requests_total",
    "Total payment checkout requests",
    ["outcome"],
)
REQUEST_LATENCY = Histogram(
    "payments_request_duration_seconds",
    "Payment checkout request latency",
    buckets=(0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.1, 0.15, 0.2, 0.3, 0.5),
)


@app.get("/healthz")
def healthz():
    return {"status": "healthy", "version": APP_VERSION, "cohort": DEPLOYMENT_COHORT}


@app.get("/readyz")
def readyz():
    return {"ready": True}


@app.get("/metrics")
def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/api/v1/payments/checkout")
def checkout(response: Response):
    """
    Canary: healthy by default (42ms, 0.5% errors).
    INJECT_ERRORS=true: 65ms latency + 2.0% error rate → triggers SPRT REJECT_H0 + CUSUM.
    """
    if INJECT_ERRORS:
        latency = random.gauss(0.065, 0.012)   # 65ms ± 12ms (degraded)
        error_rate = 0.020                      # 2.0% → p1 in SPRT config
    else:
        latency = random.gauss(0.042, 0.005)   # same as baseline → HEALTHY
        error_rate = 0.005

    time.sleep(max(0, latency))
    REQUEST_LATENCY.observe(latency)

    if random.random() < error_rate:
        response.status_code = 500
        REQUEST_COUNT.labels(outcome="error").inc()
        return {"success": False, "error": "payment_processor_timeout"}

    REQUEST_COUNT.labels(outcome="success").inc()
    return {
        "success": True,
        "transaction_id": f"txn-{random.randint(100000, 999999)}",
        "version": APP_VERSION,
    }


@app.get("/api/v1/payments/health")
def payment_health():
    return {"service": "payments", "version": APP_VERSION, "cohort": DEPLOYMENT_COHORT}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
