"""
tests/conftest.py

Root conftest for the cross-service test suites (tests/adversarial/, tests/e2e/).

Both verification-engine and policy-controller use hyphenated directory
names (not valid Python package identifiers) and BOTH name their own
top-level package `src` internally — so we can't add both service roots to
sys.path at once (that would collide on the name `src`). Instead we add each
service's `src/` directory itself onto sys.path, and import their modules
without the `src.` prefix (e.g. `from tests_statistical.mann_whitney import
run_mann_whitney`, `from verdict_verifier import verify_and_parse`). The
top-level module names inside each service's `src/` are distinct across the
two services (tests_statistical/preprocessing/scoring/engine/verdict... vs.
controller/opa_evaluator/actuation_executor/alert_dispatcher/verdict_verifier...),
so this does not collide either.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

VERIFICATION_ENGINE_SRC = REPO_ROOT / "services" / "verification-engine" / "src"
POLICY_CONTROLLER_SRC = REPO_ROOT / "services" / "policy-controller" / "src"

for path in (VERIFICATION_ENGINE_SRC, POLICY_CONTROLLER_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
