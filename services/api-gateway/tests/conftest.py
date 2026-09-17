import os
import sys

# Repo root, so `import shared.<module>` resolves the same way it does
# inside every service's container (shared/ is mounted read-only into
# each). api-gateway had no tests/ directory at all before this — first
# real test coverage for this service.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
