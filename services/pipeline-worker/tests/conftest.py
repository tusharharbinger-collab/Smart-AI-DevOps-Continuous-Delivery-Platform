"""
services/pipeline-worker/tests/conftest.py

Puts the repo root on sys.path so `from shared import ...` (used by
deploy_task.py/onboarding.py's EKS auth, mirroring main.py's existing
`shared.logging_config`/`shared.redis_streams` imports) resolves during
tests. Each test file's own sys.path insert (pipeline-worker's own root, for
`from src...` imports) is unaffected — this only adds the missing entry.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
