"""
services/explainability-service/tests/conftest.py

Ensures the repo root is on sys.path before ANY test module is collected, so `src.predictive_risk_scorer`'s
`from shared.llm_router import ...` (added when it was migrated onto the LLM provider failover router,
AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.5) resolves regardless of test collection order - this
service's `pythonpath = ["."]` (pyproject.toml) only covers `src`, and `shared/` lives two directories up.
Individual test files no longer need their own sys.path.insert for this.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
