# Plan: repeatable live AWS canary-and-auto-rollback demo

**Audience: an AI coding agent (Antigravity) picking this up with no other context.** Follow
this document top to bottom. It is a plan for a **new, separate demo service** onboarded through
this platform's own existing, unmodified UI — it does **not** ask you to change any file under
`services/`, `shared/`, `policies/`, `frontend/src/` (except as explicitly listed in the
"What you will actually create" section), or the database schema. Read `CLAUDE.md` and
`AGENTS.md` at the repo root first for the platform's architecture and hard invariants before
starting — this plan deliberately stays inside all of them.

## 1. What "done" looks like

A presenter can, in front of a live audience, repeatedly:

1. Open the platform UI, go to this one demo service's workspace.
2. Click **Trigger New Rollout**.
3. Within a couple of minutes, the live hosted URL (a real AWS Application Load Balancer URL,
   nothing faked or mocked) starts showing a page that says **"VERSION 2 — NEW RELEASE"**.
4. Within roughly another 1–2 minutes, without anyone touching anything, the platform's real
   statistical verification engine detects a genuine, real elevated error rate, produces a real
   signed `FAILED` verdict, and the real autonomous rollback fires — real ECS/ALB traffic-weight
   patch, no pod/task restart trickery.
5. The live URL now shows **"VERSION 1 — STABLE"** again.
6. Click **Trigger New Rollout** again — the exact same sequence repeats, identically, as many
   times as wanted.

Nothing about this is scripted/faked for the demo — every part of steps 3–5 is the platform's
real, already-built, already-proven canary/verification/rollback machinery, running against a
real AWS account. The only thing genuinely new is a tiny demo web app and using the platform's
existing screens to onboard and tune it.

## 2. The one clever trick that makes this simple and repeatable

The platform already injects an environment variable called `DEPLOYMENT_COHORT` into every
container it deploys — `"baseline"` for the baseline service, `"canary"` for the canary service
(see `shared/aws_ecs_actuation.py`'s `register_task_definition`). This is real, already-working,
unrelated to anything you need to build.

**Build exactly one tiny app, one Docker image, that reads this env var and behaves differently
depending on its value:**

- `DEPLOYMENT_COHORT=baseline` → renders "VERSION 1 — STABLE" (e.g. green), and its
  metrics-bearing endpoint behaves normally (near-zero errors).
- `DEPLOYMENT_COHORT=canary` → renders "VERSION 2 — NEW RELEASE" (e.g. red/blue), and its
  metrics-bearing endpoint has a **guaranteed, hardcoded elevated error rate** — not random,
  not conditional, always on. This is not a bug in the demo app; it is the demo's entire point.

Because of this, **you never touch the git repo again after the first push.** Every single
`Trigger New Rollout` click builds and deploys the exact same code. The baseline service's task
definition is only ever updated by a real `graduate_canary()` promotion, which requires a
`HEALTHY` verdict at the final step — and since the canary cohort is hardcoded to fail, that
never happens, so baseline permanently keeps serving "VERSION 1." This is what makes the loop
exactly repeatable with zero manual intervention between runs — it falls straight out of the
platform's real, existing graduate/rollback logic, not out of anything new you build.

## 3. What you will actually create

All of it lives in a **brand-new, separate GitHub repository** — call it `demo-canary-flip` or
similar. Nothing in this platform's own repository changes except this one plan file.

### 3.1 — The app

A single small HTTP server (Node/Express is simplest and matches this platform's other demo
apps' conventions, but any language is fine — the platform builds from a Dockerfile either way).

Routes:

- `GET /` — renders the big, unmistakable "VERSION 1 — STABLE" or "VERSION 2 — NEW RELEASE"
  page based on `process.env.DEPLOYMENT_COHORT`. Always returns 200, always renders successfully
  — a human clicking the live link must never see a broken page, only the version banner. Keep
  it visually loud: full-page background color, huge text, nothing else needed.
- `GET /healthz` — always returns 200 `{"status": "healthy"}` regardless of cohort. This is what
  ECS/ALB health-checks against — it must never fail, or the canary task will be killed by ECS
  before verification ever gets a chance to run.
- `POST /api/checkout` (or any name — this is the metrics-bearing endpoint the load generator in
  §3.3 will hit repeatedly): if `DEPLOYMENT_COHORT === "baseline"`, succeed ~99% of the time
  (e.g. 200 status, tiny simulated latency). If `DEPLOYMENT_COHORT === "canary"`, fail (500
  status) a real, meaningfully elevated fraction of the time — **20–30% is a good, reliably
  statistically-significant choice**; too low and the SPRT test needs more samples/time to be
  confident, too high isn't more convincing and doesn't matter either way. Do **not** make this
  random-chance-of-being-healthy or env-toggle-controlled — hardcode canary = always elevated
  error rate. That is what makes the demo reliably repeat identically every single trigger.
- `PORT`: read from `process.env.PORT` (the platform already injects this — see `CLAUDE.md`'s
  documented `PORT` env var trap; **do not hardcode a port**, or the container will listen on the
  wrong port and the ALB health check will fail every deploy).

Keep the whole app to one file if possible. This does not need a database, a frontend build
step, or anything beyond a single process. Match the "single-container full-stack" constraint
this platform already documents (`CLAUDE.md`'s established pattern) — one process serves
everything.

### 3.2 — Dockerfile

A minimal Dockerfile for whatever language you pick (Node `node:20-slim` base is simplest).
`EXPOSE` is cosmetic only — the real listen port comes from `process.env.PORT` at runtime, not
from anything baked into the image.

### 3.3 — Load generator script

**Real and important — do not skip this.** This platform's own hard invariant #7
(`CLAUDE.md`) is: verification never produces a confident verdict below **N ≥ 100 real samples**,
enforced independently in both `confidence.py` and OPA. Nothing fakes or lowers this — and it
shouldn't be lowered; it's a real safety floor. That means during the canary observation window,
something needs to generate at least ~100–200 real HTTP requests against the live canary's
`/api/checkout` endpoint, or verification will sit at low confidence / `UNVERIFIABLE` instead of
reaching a clean `FAILED`.

Write a tiny script (bash `curl` loop, or a 15-line Python script — either is fine) in this same
new repo, e.g. `load_generator.sh`:

```bash
#!/usr/bin/env bash
# Usage: ./load_generator.sh <live-url> <duration-seconds>
URL="$1"
DURATION="${2:-150}"
END=$((SECONDS + DURATION))
while [ $SECONDS -lt $END ]; do
  curl -s -o /dev/null -X POST "$URL/api/checkout" &
  sleep 0.2
done
wait
```

This produces ~5 requests/second — comfortably clears the N ≥ 100 floor within the ~2-minute
observation window from §4. Run this **manually, once, right after each Trigger click**, pointed
at the live URL — or, better, leave it running continuously in a terminal throughout the whole
demo session so you don't have to remember to start it every time. It is a demo utility, not
part of the platform and not part of the app's own container.

## 4. Onboarding — use the real UI, exactly like any other service

Do this through the actual running platform UI (`localhost:3000` or wherever it's hosted) — do
**not** call internal APIs directly, and do not hand-write a pipeline YAML from scratch. This is
what "everything should be exactly the same" means: the same **New Service** wizard, the same
**Trigger New Rollout** button, the same **Policy & Gates** tab every other service uses.

1. Push the repo from §3 to GitHub (public repo is simplest — no `GITHUB_TOKEN` credential
   plumbing needed).
2. Click **+ New Service** on the Projects Overview page.
3. **Step 1**: connect the new repo (Public Git Repository tab is fine if you don't want to deal
   with OAuth), branch `main`.
4. **Step 2**: build config —
   - Container image: a real ECR URI, lowercase, e.g.
     `<account-id>.dkr.ecr.us-east-1.amazonaws.com/demo-canary-flip` (the platform auto-creates
     the ECR repo on first build — see `CLAUDE.md`'s documented `ensure_ecr_repository_exists`
     behavior; you do not need to create it yourself).
   - Leave test command blank (this demo app has no test suite — that's fine, it's genuinely
     optional per this platform's own design).
   - Container port: whatever your app listens on by convention (e.g. `8080`) — this is just the
     declared value the wizard uses to configure the ALB health check and `PORT` injection; the
     app itself must read `process.env.PORT` at runtime as described in §3.1, not hardcode this
     number.
   - Health check path: `/healthz`.
5. **Deploy target: AWS ECS** (the only option the wizard offers — see `CLAUDE.md`'s documented
   2026-09-16 scope decision). **Deploy mode: canary** — not blue-green. Blue-green deliberately
   never runs statistical verification at all (health-check-gated cutover only), so it would
   never demonstrate the "verification catches the bad deploy and rolls back" story this demo
   exists to show.
6. **Step 3**: review, create the service.
7. **First trigger — a one-time bootstrap, not part of the repeating loop.** Click **Trigger New
   Rollout** once. A brand-new service's very first deployment always ships straight to 100%
   traffic with no canary comparison at all — this is an existing, deliberate platform invariant
   (see `VerificationInspector.tsx`'s own documented "first deployment" empty state), not
   something to work around. After this first run completes, baseline is now genuinely serving
   "VERSION 1." This step only ever needs to happen once for this service's whole lifetime.

## 5. Tuning the canary policy — the one place you'll edit generated YAML

**Real constraint to know about, not a bug:** the New Service wizard's frontend
(`frontend/src/pages/NewProject.tsx`) always submits a fixed traffic-step preset
(`10% → 25% → 50% → 100%`), not something the wizard UI itself lets you customize — and the
YAML generator's own formula (`projects_router.py`) enforces a **minimum 120-second dwell** on
any real (non-final) canary step, and a **minimum real-sample floor from the guardrails config**
— both deliberate safety floors, not something to remove.

Given that, don't fight the wizard for a literal one-click "under a minute" loop — that isn't
achievable while respecting the platform's own real safety floors and real AWS Fargate task
startup time (see §7's honest timing note). Instead, after the service is created, open its
workspace's **Policy & Gates** tab (this screen exists specifically to let you edit a service's
already-generated declarative YAML — using it is "the same UI," not a workaround) and change the
canary steps to:

```yaml
canary_loop:
  steps:
    - trafficWeight: 20
      minDuration: 120s
      minSampleSize: 100
    - trafficWeight: 100
      minDuration: 0s
      minSampleSize: 0
      requiresManualApproval: true
```

Why this exact shape:

- **Step 1 (20%, 120s, real minSampleSize) is the one that actually matters** — a real,
  non-trivial canary step, at the shortest duration the platform's own formula ever produces
  (`max(120, weight * 6)` bottoms out at 120s for any weight ≤ 20). The guaranteed elevated
  error rate on `/api/checkout` will produce a genuine `FAILED` verdict inside this window (with
  the load generator from §3.3 running), which fires the real autonomous rollback — this is the
  whole demo, and it happens before the pipeline ever reaches step 2.
- **Step 2 exists only so step 1 isn't treated as "final"** by the generator's own logic (a
  single-step-at-100% policy gets forced into `requiresManualApproval` with a zero-length
  window, which risks an inconclusive verdict instead of a clean `FAILED`) — it is never actually
  reached in this demo, since rollback always fires at step 1.
- **20% traffic weight, honestly explained:** a real ALB weighted target group routes each
  individual request randomly according to that weight — it does **not** mean every single click
  on the live URL is guaranteed to land on canary. Refreshing 3–5 times during the ~2-minute
  window reliably shows "VERSION 2" at least once. This is not a shortcoming to hide — say so
  explicitly in the demo narration ("this is genuinely weighted, randomized traffic splitting,
  not a scripted flip") — it's a more honest and more impressive demo than a fake deterministic
  flip would be. If a guaranteed-every-click flip matters more than speed/honesty for your
  specific presentation, raising the weight to e.g. 50% costs proportionally more dwell time
  (`50 * 6 = 300s`) per the same formula — a real tradeoff to make deliberately, not by accident.

## 6. The repeating loop, once bootstrapped

From here on, every single **Trigger New Rollout** click does exactly this, unattended:

1. Build (same code, seconds) → deploy canary task (`DEPLOYMENT_COHORT=canary`, real AWS Fargate
   task, real ECS API calls).
2. Canary becomes healthy (`/healthz` always passes) → traffic shifts to 20% canary / 80%
   baseline on the real ALB listener rule.
3. Verification engine pulls real CloudWatch telemetry off the real ALB/target group, runs the
   real SPRT/statistical tests against the real elevated error rate the canary is genuinely
   producing.
4. A real, HMAC-signed `FAILED` verdict is produced once N ≥ 100 real samples and the 120s
   window are satisfied.
5. OPA's `autoRollbackOnVerdict: ["FAILED"]` guardrail authorizes the action; policy-controller's
   real `emergency_rollback`/ECS-equivalent path patches the ALB weights back to 100% baseline /
   0% canary and idles the canary task — zero human input.
6. Live URL now deterministically shows "VERSION 1" again (weight is back to 100/0, no more
   randomness).
7. Baseline's task definition was never touched — it's still the exact same "VERSION 1" image
   it's always been. Click Trigger again; go to step 1.

## 7. Honest timing expectations — set these before the live demo, not during it

Real AWS Fargate task provisioning + ALB health-check grace period genuinely takes roughly
30–90 seconds before a canary task is even considered healthy enough for traffic to start
flowing to it, on top of the 120-second observation window from §5, on top of another ~10–30
seconds for the real rollback actuation (ALB listener-rule patch + ECS service update) to fully
propagate. **Realistic full loop time is roughly 3–5 minutes end to end, not "about a minute."**
This is not a shortcoming of this plan — it's what a real, non-faked AWS deployment genuinely
takes, and it's still an excellent, honest live demo at that pace. Don't try to compress it
further by lowering the N ≥ 100 sample floor or the platform's other guardrails — that would
mean faking the exact thing this whole demo exists to prove is real.

To keep repeat cycles (trigger #2, #3, ...) as tight as realistically possible: consider **not**
scaling the canary task to literal zero after rollback if you want a faster warm-start on the
next trigger (an ECS service update on an already-running task definition is faster than a cold
task launch) — this is an optional speed tweak, not required for correctness.

## 8. Test plan — verify live before calling this done

Mirror this platform's own established discipline: never claim something works without watching
it happen against the real running system.

1. Trigger once (the bootstrap run from §4 step 7) — confirm in **Verification Inspector** that
   it shows the "first deployment, no comparison to run yet" empty state, not an error.
2. Curl or open the live URL — confirm it shows "VERSION 1."
3. Start the load generator (§3.3) pointed at the live URL.
4. Click **Trigger New Rollout** a second time.
5. Watch **Pipeline View** — confirm the stage timeline advances through build → deploy → the
   canary step, and the traffic-weight gauge shows a real non-zero canary weight.
6. Refresh the live URL a few times during this window — confirm "VERSION 2" appears on at least
   one load.
7. Watch **Verification Inspector** — confirm a real `FAILED` verdict appears, with real
   evidence (an actual elevated error-rate number in the SPRT evidence block, not zero).
8. Watch **Audit Ledger** — confirm a real rollback action row appears, with a real
   `authorized_by`/timestamp.
9. Refresh the live URL again — confirm it's back to "VERSION 1."
10. **Repeat steps 3–9 at least once more** — the whole point is that it's a loop; one successful
    cycle proves it can happen, a second identical cycle proves it's actually repeatable and not
    a one-off fluke.

## 9. Explicit do-not-touch list

To keep this fully separate from the platform's own working code, as required:

- Nothing under `services/*/src/` (pipeline-worker, policy-controller, verification-engine,
  api-gateway, explainability-service)
- Nothing under `shared/`
- Nothing under `policies/` (OPA)
- Nothing under `frontend/src/` (the UI you use is already built and already correct for this)
- No database migration, no `db/schema.sql` change
- No change to any existing project/service already onboarded on this platform

Everything this plan asks for is either (a) content in a brand-new, separate GitHub repository,
or (b) using already-existing platform UI screens (New Service wizard, Policy & Gates editor,
Trigger New Rollout button) exactly as any real user would.

## 10. Cleanup / reset

If something goes wrong mid-build and you want to start over: delete the demo service from the
Projects Overview (if a delete action exists in this UI version) or simply stop triggering it —
it costs nothing while idle beyond the one warm canary task if you chose not to scale it to zero
(§7). No platform-side cleanup is ever required, since nothing platform-side was changed.
