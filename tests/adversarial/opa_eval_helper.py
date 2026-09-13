"""
tests/adversarial/opa_eval_helper.py

Evaluates policies/delivery_guardrails.rego directly via the `opa eval` CLI
(bin/opa.exe on Windows, `opa` on PATH elsewhere) instead of requiring a
running OPA HTTP server on localhost:8181. This keeps the adversarial suite
self-contained: `make test-opa` already exercises the server-mode path via
docker compose, so these tests exercise the same policy file a different,
dependency-free way and can run in any environment that has the CLI vendored
(bin/opa.exe is committed for Windows).
"""
import json
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
POLICY_FILE = REPO_ROOT / "policies" / "delivery_guardrails.rego"


def _opa_binary() -> str:
    windows_opa = REPO_ROOT / "bin" / "opa.exe"
    if windows_opa.exists():
        return str(windows_opa)
    found = shutil.which("opa")
    if found:
        return found
    raise RuntimeError(
        "No `opa` binary found (checked bin/opa.exe and PATH) — "
        "cannot evaluate policies/delivery_guardrails.rego"
    )


def eval_guardrails(input_doc: dict) -> dict:
    """
    Runs `opa eval -d policies/delivery_guardrails.rego -I data.delivery.guardrails`
    against the given input document and returns the evaluated result object
    (the same shape OPA's REST API returns under `result`).
    """
    opa_bin = _opa_binary()
    proc = subprocess.run(
        [
            opa_bin, "eval",
            "--data", str(POLICY_FILE),
            "--format", "json",
            "--stdin-input",
            "data.delivery.guardrails",
        ],
        input=json.dumps(input_doc),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"opa eval failed: {proc.stderr or proc.stdout}")

    parsed = json.loads(proc.stdout)
    # `opa eval` wraps results as {"result": [{"expressions": [{"value": {...}}]}]}
    expressions = parsed["result"][0]["expressions"]
    return expressions[0]["value"]
