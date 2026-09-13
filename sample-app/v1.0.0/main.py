"""
services/sample-app/v1.0.0/main.py
Baseline payments-service (v1.0.0) — healthy, stable error rate.

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

app = FastAPI(title="payments-service", version="1.0.0")

INJECT_ERRORS = os.environ.get("INJECT_ERRORS", "false").lower() == "true"
DEPLOYMENT_COHORT = os.environ.get("DEPLOYMENT_COHORT", "baseline")
APP_VERSION = os.environ.get("APP_VERSION", "v1.0.0")

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
    """Simulates a payment checkout. Baseline has ~0.5% error rate."""
    latency = random.gauss(0.042, 0.005)   # 42ms ± 5ms
    time.sleep(max(0, latency))
    REQUEST_LATENCY.observe(latency)

    error_rate = 0.020 if INJECT_ERRORS else 0.005   # 2% if errors injected, 0.5% baseline
    if random.random() < error_rate:
        response.status_code = 500
        REQUEST_COUNT.labels(outcome="error").inc()
        return {"success": False, "error": "payment_processor_timeout"}

    REQUEST_COUNT.labels(outcome="success").inc()
    return {"success": True, "transaction_id": f"txn-{random.randint(100000, 999999)}"}


@app.get("/api/v1/payments/health")
def payment_health():
    return {"service": "payments", "version": APP_VERSION, "cohort": DEPLOYMENT_COHORT}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
