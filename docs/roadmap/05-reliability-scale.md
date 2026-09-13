# Phase 5 — Reliability & Scale

**Status: done, verified live end-to-end (3 replicas each of pipeline-worker/policy-controller, 10 concurrent rollouts processed exactly once, a real crash-recovery resume, and two real bugs found and fixed by actually killing Redis/OPA mid-flight).**

## Goal
The platform keeps working correctly when multiple pipelines run concurrently, a worker process crashes mid-rollout, or a service needs to run more than one replica.

## Why now
Running 2 replicas of `pipeline-worker` before this phase would double-process every triggered rollout — Redis pub/sub delivers to every subscriber, with no consumer-group semantics.

## What shipped

### 5.1 — A real task queue
- [x] **Redis Streams + consumer groups** chosen over Celery (per the roadmap's own stated tradeoff — no new infra dependency, and Redis was already load-bearing everywhere else). New `shared/redis_streams.py`: `ensure_group`/`publish`/`read_one`/`ack`/`claim_stale_pending`/`get_delivery_count`, async throughout (a sync client's blocking `XREADGROUP` would freeze the whole event loop for up to 5s per poll).
- [x] `pipeline:start` (api-gateway → pipeline-worker) and `verdicts` (verification-engine → policy-controller) both converted from bare pub/sub to a stream + consumer group — exactly one replica now processes each message, with a periodic stale-pending reclaim loop (30s idle threshold) and dead-lettering after 3 delivery attempts.
- [ ] `pipeline:manual_rollback` (the UI's "Emergency Rollback" button) was found to have **zero subscribers** — a pre-existing, unrelated bug, not touched here (its docstring's claim that it goes through HMAC/OPA like a real verdict doesn't match its actual crypto model — api-gateway has no `VERDICT_SIGNING_KEY` to sign a synthetic verdict with — a design question, not a queue-mechanism fix, and out of this phase's scope).

### 5.2 — Crash recovery, for real
- [x] pipeline-worker now has a **real Postgres connection** (`src/db.py`, asyncpg) — before this, `ExecutionStateStore`'s DB path was dead code (`db_session` was always `None` in the real app), so `execution_state` rows were never durably written and `reconciler.py` could never find anything to resume.
- [x] `reconciler.py` rewritten: re-fetches the interrupted run's own registered manifest (`pipelines.policy_yaml` via the newly-added `execution_state.pipeline_id` FK — migration `0005`) and re-enters `PipelineOrchestrator.start_pipeline` with `resume_from_stage` set to the stage it died on (re-run, not skipped — every stage type is already idempotent by design).
- [x] Wired into `main.py`'s lifespan: runs once at startup AND on a 60s periodic loop (a worker could crash while other replicas stay healthy and never restart — startup-only reconciliation would leave that pipeline stuck forever).
- [x] `tests/test_crash_recovery.py`: simulates a worker dying mid-"progressive_verify" (with "test" already completed), runs the real reconciler against real Redis+Postgres, and asserts the resumed pipeline reaches `COMPLETED` **without re-executing the already-completed stage**.

### 5.3 — Horizontal scaling
- [x] `docker-compose.scale-test.yml` (+ `make scale-test-up`/`scale-test-down`) — an override that drops the fixed host-port mapping so 3 replicas of pipeline-worker/policy-controller can actually start (Compose can't bind one host port to 3 containers).
- [x] **Live-verified**: 10 concurrent rollouts triggered across 2 services (payments + checkout), processed by 3 replicas each — every run_id and every verdict_id appears **exactly once** across all replicas' logs (`grep | sort | uniq -c` — no duplicates, no drops), all 10 reached `COMPLETED`.
- [x] DB connection pooling reviewed, not changed: `db/session.py`'s `NullPool` remains appropriate at current request volume (confirmed no pool-related errors during the 10-concurrent-trigger test); `AsyncAdaptedQueuePool` is the documented follow-up once real production load justifies the added complexity of pool sizing under the RLS `SET LOCAL`-per-transaction pattern.

### 5.4 — Graceful degradation
- [x] **OPA unreachable is NOT uniformly "fail closed."** Found (and confirmed live, by actually stopping OPA) that blocking an autonomous ROLLBACK because the guardrail check itself is down is not safe — it leaves a known-bad canary serving traffic. Fixed: `opa_evaluator.py` now distinguishes "OPA reached, said no" from "OPA unreachable"; `controller.py` fails closed (blocks) for PROMOTE_STEP but fails *open* (rolls back anyway, `authorized_by="FAILSAFE:opa_unreachable"`) for ROLLBACK specifically — never overriding a real, reachable policy rejection. 3 unit tests + a real live test (real error injection → real FAILED verdict → real rollback with OPA fully down; separately, real HEALTHY verdicts confirmed still blocked from promoting while OPA was down).
- [x] **Redis unreachable**: two real bugs found by actually stopping the Redis container.
  1. Phase 4's login rate-limiting made `/api/v1/auth/login` 500 with a raw traceback the moment Redis was unreachable — even for the parts of login that don't need Redis. Fixed with an explicit, documented policy: rate-limiting fails *open* (skip the check, log a warning, let login proceed) since it's a defensive add-on; refresh-token issuance (a genuine hard Redis dependency by design) now fails with a clean `503`, not a raw `500`.
  2. **policy-controller's verdict consumer silently died forever** on any Redis error during `read_one` — the retry `try/except` only wrapped the message-processing step, not the read itself, so a transient Redis blip permanently killed the background task with no crash log anywhere. Pipeline-worker's equivalent loop already had this right; policy-controller's didn't. Fixed to match, and verified live: stopped Redis, restarted it, confirmed policy-controller resumed processing verdicts automatically (before the fix, it stayed dead even after Redis came back).

## Real bugs found and fixed (found by actually running this against the real stack, not by reasoning about the code)
1. **`asyncio.run()` from a worker thread against an event-loop-bound asyncpg pool** — `ConnectionDoesNotExistError`. Fixed with `run_coroutine_threadsafe` back onto the loop the pool actually belongs to (`db.py`'s `run_from_thread`).
2. **Pipeline manifest's `tenantId` is a human-readable slug** (`"acme-corp"`), not the real `tenants.tenant_id` UUID `execution_state`'s FK column needs — previously harmless (only ever used as a Redis lock-key string). Fixed by threading the real UUID through from api-gateway's trigger command, overriding the manifest's own value.
3. **`asyncpg` needs a real `datetime`, not an ISO string**, for a `TIMESTAMPTZ` column — unlike psycopg2, it doesn't auto-cast.
4. **The per-(tenant,service) concurrency lock is correct, pre-existing, intentional behavior** — discovered while designing the horizontal-scaling test, when firing 10 truly-simultaneous same-service triggers caused 8 of them to be legitimately `REJECTED` (not a bug; the test was redesigned to interleave across services instead).
5. **OPA-unreachable fail-closed was unsafe for ROLLBACK** — see 5.4 above.
6. **Redis-unreachable crashed login entirely, and permanently killed policy-controller's verdict consumer** — see 5.4 above.

## Acceptance criteria
- [x] Starting 3 replicas of `pipeline-worker` (and policy-controller) and triggering 10 concurrent rollouts across 2 different services results in exactly 10 completed pipelines, no duplicates, no dropped triggers — verified live via container logs and `execution_state`/`pipeline_executions` rows.
- [x] Killing a pipeline-worker process mid-rollout and starting a fresh one results in the interrupted pipeline resuming from its last recorded stage — verified by `tests/test_crash_recovery.py`, not just by reading the code.

## Depends on
Nothing strictly — sequenced after Phase 3 so multi-service horizontal-scaling testing had a second real service to trigger against.
