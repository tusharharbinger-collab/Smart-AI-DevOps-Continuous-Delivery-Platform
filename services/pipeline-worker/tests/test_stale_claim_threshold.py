"""
services/pipeline-worker/tests/test_stale_claim_threshold.py

Real bug found live (2026-09-17): STALE_CLAIM_MIN_IDLE_MS was 30 seconds —
calibrated against the original lightweight demo pipeline, never updated
once gate1's real build+test dry run (observed: 77s for a small repo) and
real blue-green pipeline stages (ECS stabilization waits, up to 120s
target-group health polling, live-URL verification retries) started
actually running. Every real gate1 check and pipeline run legitimately
exceeded 30s while still genuinely in progress, so the stale-pending
reclaim loop treated a live, working consumer as dead and handed the SAME
message to a second consumer — confirmed live: one push produced two
independent real pipeline_executions for the identical commit, one of
which failed from the resulting ECS target-group churn.

This doesn't re-test the reclaim mechanism itself (see shared/redis_streams.py
and its own coverage) — it pins the one number that broke, so it can't
silently drop back to a value shorter than real observed work again.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.main as main_module

# The longest single real delay this platform's own code can legitimately
# wait on before a stage naturally finishes or times out on its own terms:
# wait_for_target_group_healthy's default 120s timeout, plus real build/
# clone/push time, plus retry backoff elsewhere. 30s (the original,
# buggy value) is comfortably below this; the fix must be comfortably above it.
LONGEST_KNOWN_LEGITIMATE_STAGE_SECONDS = 120


def test_stale_claim_threshold_is_comfortably_longer_than_real_work_can_take():
    assert main_module.STALE_CLAIM_MIN_IDLE_MS >= LONGEST_KNOWN_LEGITIMATE_STAGE_SECONDS * 1000 * 2, (
        "STALE_CLAIM_MIN_IDLE_MS must stay well above the longest real stage "
        "duration (wait_for_target_group_healthy alone can legitimately take "
        "120s) or every real gate1 check / pipeline run risks being reclaimed "
        "and reprocessed while still genuinely in progress, producing "
        "duplicate rollouts for the same commit (see this file's docstring "
        "for the exact live incident this guards against)."
    )
