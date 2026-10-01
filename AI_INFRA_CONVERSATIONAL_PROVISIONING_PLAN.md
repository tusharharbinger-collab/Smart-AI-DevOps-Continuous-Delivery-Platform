# AI Infra Provisioning V2 — Iterative, Compatibility-Verified, Existing-Resource-Aware Plan

**Status: PLAN ONLY — nothing in this document has been built yet.** This is a design plan, written after (a) reading the real current implementation end-to-end (`shared/repo_scanner.py`, `shared/intent_spec.py`, `shared/infra_needs.py`, `services/explainability-service/src/infra_generator.py`, `shared/provisioning/aws_discovery.py`, `services/api-gateway/src/routers/projects_router.py`'s `/infra-drafts` routes, `frontend/src/components/wizard/RequirementsForm.tsx`) and (b) the two existing plan docs that already shipped real phases (`AI_AGENTIC_ORCHESTRATION_PLAN.md`, `AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md`), and (c) a round of web research on how similar tools do this today (AWS's own `agent-plugins`/Launch Wizard pattern — analyze codebase → recommend services with rationale → estimate cost → let a human modify and re-estimate live → provision on approval; and Terraform 1.5+'s `import {}` block pattern of "preview before anything touches state"). It exists so the next build pass has an accurate target and doesn't re-invent what's already real.

## 1. What's real today vs. what the user actually asked for

The user's ask, restated as a sequence:
1. AI first understands **the user's actual code** (not just file names) and proposes exact resources + exact cost.
2. Human either agrees, **or** gives a free-text prompt for more/different, **or** picks a specific component (EC2, ECS, etc.) from a list — the agent then verifies whether it's usable, explains how, and re-quotes, before the human confirms.
3. Only once the human agrees does infra actually get created.
4. Only after that does the flow move to building the pipeline.
5. If existing AWS resources already satisfy the need, the human can select them — but they must be **visibly shown as existing and running**, not just offered as an abstract toggle.

Here is what's real today, function-by-function, and the gap against each point:

| Point | Today | Gap |
|---|---|---|
| 1. Understand the code | `repo_scanner.py` is **purely file-signature/manifest based** — Dockerfile presence, `package.json`/`requirements.txt` **dependency names** (e.g. `boto3`, `psycopg2`), Procfile process types, test-config files. It never opens and reads actual source files for how those dependencies are *used* (no boto3 call-site scanning, no DB connection-string detection, no cron/schedule pattern detection). `match_golden_path_archetype` then maps that signal set to one of six fixed archetypes. | Real gap. The AI never sees a line of the user's actual application code before proposing resources — only manifest metadata. |
| 1. Exact cost | `apply_independent_cost()` already replaces the AI's self-reported cost with a **real AWS Price List lookup**, called after every create *and* every edit, before OPA evaluates it. This is solid and correct already. | No gap — reuse as-is. |
| 2. Free-text "I want more" | `POST /infra-drafts/{id}/edit` already exists: one free-text instruction (3–1000 chars) → `generate_infra_proposal(edit=...)` → deterministic post-hoc checks (`_require_retained_resources_preserved`, no-op detection) → new draft row → re-enters the approval gate → cost re-attached. | No gap in the free-text path itself — reuse as-is. |
| 2. Pick a specific component, agent verifies + explains before regenerating | **Does not exist.** There is no structured "add this resource type" input anywhere (the edit body is `{instruction: str}` only), and there is no pre-generation compatibility/explanation step at all — the only verification is post-hoc, on the *output* diff, after a full regeneration has already happened. | Real gap — this is the core of what needs building. |
| 3. Infra created only after agreement | Already true: `approve_infra_draft` → `create-change-set` → `execute` (role-gated) is already a distinct, human-gated sequence (Phase 7, already built). | No gap. |
| 4. Pipeline only after infra | The wizard's step ordering already puts Requirements (infra) before Policy & Deploy (pipeline), but it needs to be confirmed the "Continue" action is actually gated on `INFRA_PROVISIONED` and not just `INFRA_APPROVED` (approved but not yet executed). Flagged as **needs verification**, not assumed either way. | Possible small gap — verify, don't assume, before building. |
| 5. Existing resources, visibly running | `aws_discovery.py` already does real, read-only, per-slot `describe_*` calls and **already returns a live status field** (`DBInstanceStatus`, ElastiCache status, ECS cluster status, ALB scheme/dns) for the resource types it lists — but only `database` and `cache` slots are actually wired into `ARCHETYPE_SLOTS` for the UI today, and `RequirementsForm.tsx` renders them as a **plain `<select>` dropdown** with just `label` text, not a real status-visible browser. | Real gap on the frontend (and partial backend wiring gap) — the raw data already exists, it's just not surfaced. |

## 2. Target flow, mapped onto real components

```
Step 0 (NEW)  Deep code-evidence scan     → richer infra_signals, cited evidence
                       │
Step 1 (EXISTS)  generate_infra_proposal  → apply_independent_cost (real $)
                       │
Step 2  Human reviews proposal ──────────────────────────────┐
                       │                                      │
              ┌────────┴────────┐                             │
        "Looks good"      "I want changes"                    │
              │                  │                             │
              │         ┌────────┴─────────┐                  │
              │   Free-text prompt   Pick a component          │
              │    (EXISTS: edit)   (NEW: structured picker)   │
              │         │                  │                   │
              │         │        NEW: compatibility check      │
              │         │        (can_add / how / caveats)     │
              │         │                  │                   │
              │         │           Human confirms ───┐        │
              │         └──────────────────┴──────────┤        │
              │                                        ▼        │
              │                        generate_infra_proposal(edit=...)
              │                        (EXISTS, unchanged) → new draft
              │                                        │        │
              │                                loop back to Step 2 ┘
              ▼
     approve_infra_draft (EXISTS)
              │
     create-change-set → execute (EXISTS, Phase 7)
              │
       INFRA_PROVISIONED
              │
     ── only now ──► Pipeline Architect Agent / pipeline-preview (EXISTS)

Existing-resource path (parallel to Step 1, when source="existing"):
  discover_existing() (EXISTS, real describe_* calls)
        │
  NEW: resource browser UI — status-visible, per-slot, refreshable
        │
  Human picks specific resource(s)
        │
  describe_selected() (EXISTS — re-verifies every pick against real AWS,
                        never trusts client-supplied config)
        │
  generate_infra_proposal(existing_resources=...) (EXISTS) → Step 2 loop above
```

The key design decision: **the new "add a component" flow reuses the existing edit pipeline underneath** (`generate_infra_proposal(edit=...)`, the Retain-preservation check, cost re-attachment, the tiered approval gate). It does not introduce a second, parallel resource-creation code path. It only adds a *new, cheaper, pre-flight step in front of* the existing one — the compatibility check — so a human sees "yes, this works, here's how" (or "no, here's why not") **before** the system spends a full regeneration cycle (and before it counts against Groq's documented 8,000 TPM cap that this codebase has already hit and worked around once).

## 3. New pieces required

### 3.1 Deep code-evidence scan (extends `repo_scanner.py`, doesn't replace it)
- Stays bounded and cheap: scan only the top-N largest source files matching the already-detected language, capped by file count and byte size (mirroring the existing `truncated` flag pattern `_fetch_repo_tree_and_manifests` already uses for the file tree).
- Regex/heuristic pass (not full AST parsing, to stay fast and dependency-free) for known SDK call-site patterns: e.g. `boto3.client(["']s3["']`, `psycopg2.connect(`, `redis.Redis(`, `sqlalchemy.create_engine(`, cron-like scheduling calls for worker detection.
- Output: additional `infra_signals` fields carrying **cited evidence** (file path + line number + the matched snippet) — never a bare boolean, so the proposal can say "we detected S3 usage in `src/upload.py:42`" instead of an unexplained toggle.
- Feeds into the *same* `match_golden_path_archetype` and the *same* `generate_infra_proposal` prompt — this is additional evidence, not a new decision path.

### 3.2 Structured "add a component" input + compatibility check (new)
- New small request shape for `POST /infra-drafts/{id}/edit` (additive — keep the existing free-text `instruction` field working unchanged): an optional structured `{resource_type: str, params: dict}` that the backend translates into a well-formed instruction string server-side, so `generate_infra_proposal`'s prompt contract never has to branch on "was this free text or structured."
- New endpoint, e.g. `POST /infra-drafts/{id}/check-component` — **deliberately separate from `/edit`**, so "just checking" never triggers a full regeneration or a real draft row:
  - Try a deterministic rule table first (fast, free, no LLM call) for well-known combinations — e.g. "EC2 alongside a Fargate archetype: allowed as a bastion/sidecar, not as primary compute" or "a second ALB: not allowed, this platform uses one shared ALB per the platform's own invariant."
  - Fall back to a small, cheap LLM call (plain JSON object mode is enough here — this schema is much simpler than the full infra-proposal schema) only for resource types the rule table doesn't cover — routed through the failover chain in §3.5, not a single hardcoded provider.
  - Returns `{compatible: bool, explanation: str, caveats: string[]}` — advisory and explanatory only, never a substitute for the real gate (see §5).
- Frontend shows this result inline with a Confirm/Cancel step **before** calling the real edit endpoint.
- **The picker must offer a genuinely complete catalog, not a short example list** — every entry below is a real, addable resource type, grouped by category (mirrors how the AWS Console itself groups services, so the picker reads as familiar rather than inventing its own taxonomy):

  | Category | Resource types in the picker |
  |---|---|
  | Compute | EC2 instance, additional Fargate/ECS service (non-platform-owned, project-specific), Lambda function, Batch job queue |
  | Storage | S3 bucket, EFS file system, additional EBS volume |
  | Database | Additional RDS instance/read replica, DynamoDB table, Aurora Serverless |
  | Cache | Additional ElastiCache node/cluster |
  | Messaging | SQS queue, SNS topic, EventBridge rule |
  | Networking | CloudFront distribution, additional security group, Route 53 record |
  | Security/Secrets | Secrets Manager secret, KMS key, WAF rule |
  | Observability | Additional CloudWatch alarm, CloudWatch Logs log group with custom retention |

  Each entry carries its own minimal `params` schema (e.g. an S3 bucket needs `versioning`/`public_access`; an SQS queue needs `fifo`/`visibility_timeout_seconds`) — the exact per-type param schemas are an implementation detail for the build phase, not enumerated here, but the **catalog itself must be complete at launch**, not trimmed to a handful of examples, since a partial picker would just recreate today's gap in a new shape. Entries that conflict with `_PLATFORM_OWNED_TYPES` (ALB, listener, listener-rule, target-group, the shared ECS cluster itself) are excluded from the picker entirely, not merely flagged incompatible after the fact — see §6.3.

### 3.5 LLM provider resilience — 4-way failover routing (new)
Every AI call in this flow (`generate_infra_proposal`, the new compatibility check in §3.2, and — per the existing invariant already documented in this codebase — the Pipeline Architect Agent, RCA generation, ChatOps, log-hygiene analysis, predictive risk scoring) currently depends on a **single provider** (`GROQ_API_KEY`, with a deterministic non-AI fallback only for the specific calls that already had one built). A single provider going down, being rate-limited (this codebase has already documented hitting Groq's 8,000 TPM cap live), or having a revoked key currently means those specific features degrade one at a time, each with its own bespoke fallback (or none).

Add a shared, ordered **LLM provider failover router** instead of a per-feature fallback:

- **Priority chain (as specified): Gemini (primary) → Groq (secondary) → OpenRouter (tertiary) → Mistral (quaternary).** Each provider entry carries: display name, active model, credential (stored the same way this codebase already stores other third-party secrets — never logged, never returned to the browser except masked), API endpoint, and a measured/last-known latency.
- A single shared client module (e.g. `shared/llm_router.py`) that every current Groq call site is migrated to use instead of calling Groq directly — same call signature shape (`messages`, `response_format`, `max_tokens`) so call sites don't need to know which provider actually served the request. On a request failure (timeout, 4xx auth/rate-limit, 5xx), it advances to the next provider in priority order and retries once per provider before giving up and falling back to that feature's own existing deterministic fallback (e.g. `_heuristic_risk_assessment`, `log_hygiene_analyzer`'s rule-based path) — the deterministic fallbacks this codebase already built stay the last resort, unchanged.
- A small admin/settings surface (matching the referenced screenshot's layout) showing all four providers as cards: priority badge, active model, masked credential, endpoint, last-measured latency, and **"Test Connection"** / **"Edit Credentials"** actions per provider — this is a genuinely new settings screen, not an extension of an existing one.
- Every provider's response is still schema-validated the same way a single-provider call is today (`InfraGenerationResult`/`PredictiveRiskReport` etc.) — the router changes *who answers*, never *what shape a caller trusts*.
- This is infrastructure for the whole platform's AI usage, not just this infra-provisioning flow — but it's listed here because §3.2's compatibility check is a new call site that should be built against the router from day one rather than hardcoded to Groq and migrated later.

### 3.3 Existing-resource visibility
- Backend: extend `ARCHETYPE_SLOTS` wiring in `aws_discovery.py` so slots beyond `database`/`cache` (the listers for `ecs_cluster` and `load_balancer` already exist in code) are exposed **only where genuinely useful** — see the open question in §6 about whether ECS cluster / load balancer should ever be offered at all, given this platform's shared-ALB/shared-cluster invariant.
- Frontend: replace the per-slot `<select>` in `RequirementsForm.tsx` with a real browser component — one card/row per discovered candidate showing engine, version, instance class, region, and a **status badge** (running/available vs. stopped/deleting, sourced directly from the already-returned status field), plus a manual refresh (re-run discovery on demand — never cache indefinitely, since "is it actually running right now" is the entire point).
- `describe_selected()`'s re-verification-on-selection behavior is unchanged and remains the trust boundary — the browser is a better *view* of the same real data, not a new trust path.

### 3.4 Wizard step-gating (verify, then fix if needed)
- Confirm whether "Continue" from the Requirements step today requires `INFRA_PROVISIONED` or merely `INFRA_APPROVED`. If it's the latter, tighten it — the user's requirement is explicit that pipeline-building starts only *after* infra is actually created, not merely approved.

## 4. What does NOT change (guardrails)

- Cost still comes only from `apply_independent_cost` (real AWS Price List) — the compatibility-check step never supplies a cost number of its own.
- OPA (`infra_guardrails.rego`) stays the single real safety/policy gate. The compatibility check is advisory/explanatory, never a substitute for it.
- `_require_retained_resources_preserved` and CloudFormation `DeletionPolicy: Retain` semantics for imported resources are unchanged.
- No new auto-apply path anywhere — every step stays propose → explain → human confirms, matching this codebase's existing "no AI in the safety-critical loop" invariant.
- `describe_selected()` remains the only trust boundary for an existing-resource pick — a client-supplied resource id/config is never trusted directly, exactly as today.
- The platform's shared-ALB/shared-ECS-cluster model (one ALB, one cluster, shared across all projects) is not something a per-project "add a component" or "attach existing" flow should be able to override — flagged explicitly as an open question in §6, not assumed.
- The failover router (§3.5) changes *which provider answers*, never the schema a caller trusts or the deterministic non-AI fallback a feature already had — every existing Pydantic/JSON-schema validation stays exactly as strict as it is today regardless of which of the four providers actually responded.

## 5. Suggested build order (incremental, independently shippable)

1. **Phase A — Existing-resource browser.** Frontend + the small backend slot-wiring extension. No new AI call, smallest risk, immediately visible value, and it's pure UI on top of data `aws_discovery.py` already returns today.
2. **Phase B — LLM provider failover router (§3.5).** Built before Phase C so the new compatibility-check call in Phase C is written against the router from day one rather than hardcoded to Groq and migrated later. Existing Groq call sites (`infra_generator.py`, RCA, ChatOps, log-hygiene, predictive-risk) are migrated onto it one at a time, each independently verifiable against its own existing tests.
3. **Phase C — Structured "add a component" + compatibility check.** New endpoint, new UI picker with the full catalog from §3.2, wired to the *existing* edit pipeline underneath. No changes to `generate_infra_proposal`'s core contract.
4. **Phase D — Deep code-evidence scan.** Extends `repo_scanner.py`, richer initial proposals, evidence citations in the UI (reusing the citation-style UI pattern already established by ChatOps/RCA elsewhere in this app).
5. **Phase E — Wizard step-gating verification/fix.** Smallest phase, do last once the rest of the flow exists to test against.

## 6. Open questions to resolve before implementation starts

1. **Compatibility check: rule table first, or always call the LLM router?** Recommend rule table first (cheaper, instant) with the router (§3.5) only as a fallback for resource types the table doesn't cover.
2. **Which resource types does the "Add a component" picker actually offer at launch?** §3.2 now lists a full catalog by category as the target — confirm whether all of it ships at once or is trimmed for a first pass.
3. **Should ECS cluster / load balancer ever appear as "existing, attachable" resources?** This platform's own invariant is one shared ALB + one shared ECS cluster for every onboarded project (`_PLATFORM_OWNED_TYPES` already forbids the AI proposal from recreating these). My read: the existing-resource browser should very likely stay scoped to *non-platform-owned* resource types (database, cache, storage, queue, etc.) and deliberately exclude ALB/cluster from "attachable," to avoid contradicting that invariant — but this is a real product decision, not mine to assume silently.
4. **Where do the four provider credentials (Gemini/Groq/OpenRouter/Mistral) get stored, and who can edit them?** This codebase already has an established pattern for third-party credentials (GitHub's OAuth token lives in Redis, never Postgres, because api-gateway has no `cryptography` dependency) — the router's credential store needs an explicit decision on which existing pattern it follows, and which role (if any, mirroring `require_role` gating already used for infra approval/execution) can view "Edit Credentials."
5. **Does every current Groq call site migrate to the router in one pass, or incrementally?** Recommend incremental (per Phase B above) so each migrated call site's existing tests (e.g. `predictive_risk_scorer.py`'s heuristic-fallback tests) keep passing independently rather than one large cutover.

---

**Update (2026-09-29): Phases A–E above are done and live-verified** (existing-resource browser, the 4-way LLM failover router with real Gemini/Groq/OpenRouter/Mistral credentials, the "Add a component" picker + compatibility check, the deep code-evidence scan, and the wizard step-gating fix). What follows is a new phase — **Phase F** — requested after seeing all of that live: the UI itself still looks like the original static form, and the real ask now is a genuinely different interaction model on top of the real backend Phases A–E already built. **Phase F is plan-only, same as the rest of this document — nothing below has been implemented.**

## 7. Phase F — Conversational UI, resource confirmation, and a live build view

### 7.1 What was asked for

Three things, in this order:
1. The infra-proposal step should feel like a **chat interface** — not a static form with a "Preview Infrastructure (AI)" button and a wall of panels underneath it.
2. **Before anything gets built**, the human should see a clear, explicit list of **all the resources that will be used** — not buried in a collapsed section.
3. **After confirming**, the human should see the infrastructure actually **being built on screen**, live — not one static sentence with a spinner.

### 7.2 What's real today (grounded by reading the actual code, not assumed)

- **No chat UI exists in this flow at all.** The current "Edit with AI" affordance is a single plain-text `Input` + "Apply" button in `RequirementsForm.tsx` — one-shot, not a conversation. This app DOES have two chat-style components elsewhere (`ChatOpsPanel.tsx`, and `DevOpsCopilotPanel.tsx`/`DevOpsCopilotDrawer.tsx`), but **the two Copilot files independently duplicate the same message-bubble/markdown-rendering code** — they share only a Zustand store (`copilot-store.ts`), not any actual rendering component. There is no reusable chat-bubble primitive in this codebase today.
- **The "resource list" data already exists twice, at two different points in the flow, but is barely surfaced:**
  - Right after the AI proposes something: `infra_proposal.topology.nodes` (id/type/label) — rendered today only as a graph (`InfraTopologyGraph`), never as a plain list.
  - Right before execution: `change_set_changes` (`{action, logical_id, resource_type}[]`) — a REAL CloudFormation change-set diff (exactly what will happen, not a guess), but rendered today as a bare, unstyled `<ul>` with no visual hierarchy, sitting inside a collapsed-feeling "Phase 7" section most users would never notice.
- **The execute confirmation is a bare HTML checkbox**, not a dialog or a distinct step — "I understand this will create real, billable AWS resources," then "Execute — Create Real Resources."
- **The "live build" view is one static sentence** ("Provisioning real AWS resources — this can take a few minutes…") with a spinner, while the frontend polls `GET /infra-drafts/{id}/status` every 3 seconds. That status poll returns **only an aggregate CloudFormation stack status** (`CREATE_IN_PROGRESS`/`CREATE_COMPLETE`/etc.) — never a per-resource breakdown.
- **The per-resource data a live build view would need DOES exist on the backend** — `shared/provisioning/aws_cloudformation.py::fetch_failure_events()` already calls CloudFormation's real `describe_stack_events` and returns `{resource, type, status, reason, timestamp}` per event — **but it is only ever called after a TERMINAL failure**, for the RCA panel (`InfraFailureAnalysisPanel`). Nothing calls it during an in-progress build. Wiring it into the live poll is real, new backend work — not a rewrite, an extension of a function that already does the real AWS call.
- **This app already has a working "live status timeline" pattern** — `StageTimeline.tsx` (pipeline stages: pending/active/done/failed icons, connected vertical line, per-item sub-steps). It's driven by regex-matching live LOG LINES, which doesn't apply to CloudFormation (no log stream, just periodic stack-event snapshots) — but the *visual pattern itself* (status icon per item, connecting line, sub-detail per item) is exactly the right shape to reuse for a new resource-provisioning timeline, fed by real stack events instead of log regex.

### 7.3 Target flow

```
Stage A — Chat with the Infra Architect
  Human describes/reviews needs conversationally. Every turn is a REAL call to an
  existing endpoint, never a new generation path:
    - Initial proposal            → createInfraDraft()          (exists)
    - "add a redis cache" (free text) → editInfraDraft()         (exists)
    - "Add a component" picker    → checkComponentCompatibility() + editInfraDraft()  (exists, Phase C)
    - "Use existing database"     → discoverExistingInfra() + describe_selected()     (exists, Phase A)
  Each AI turn's chat bubble states, in plain language, what changed and the new
  cost (e.g. "Added an EC2 bastion. Cost: $42/mo -> $47/mo") - grounded in the
  same apply_independent_cost() real-AWS-pricing number already computed today,
  never a chat-invented number.
  Ends when the human sends/clicks an explicit "This looks good" action.
        |
        v
Stage B — Resources & cost, confirm
  A dedicated screen (not a collapsed section) listing:
    - Every resource the proposal will touch, human-labeled by resource_type
      (e.g. "AWS::RDS::DBInstance" -> "Database"), grouped Add/Modify/Remove/Import
      exactly as CloudFormation's own real change-set already reports it via
      createInfraChangeSet() (EXISTS today - this triggers the real, zero-risk
      CFN change-set preview at this point, not before).
    - The real cost breakdown and policy checks (already computed, just given a
      properly designed dedicated screen instead of a small two-column grid).
  Confirmation is an explicit dialog (not a bare checkbox) - "You are about to
  create N real, billable AWS resources" with the resource list still visible
  inside it, then a single "Confirm & Build" action -> executeInfraChangeSet()
  (EXISTS today).
        |
        v
Stage C — Building your infrastructure (live)
  A new InfraBuildTimeline component, visually modeled on StageTimeline.tsx's
  pattern (status icon per item, connecting line) but driven by REAL per-resource
  CloudFormation events instead of log regex - one row per resource from the
  change-set list, each showing pending -> in-progress -> complete/failed as the
  REAL stack events arrive. Requires the one real backend gap below (§7.4).
  Ends at INFRA_PROVISIONED (terminal) - same status already tracked today.
```

### 7.4 What's actually new (the real gaps, not a rebuild)

1. **Backend: expose per-resource events during in-progress polling, not just after failure.** `GET /infra-drafts/{draft_id}/status` (and the pipeline-worker route underneath it) needs to also call `fetch_failure_events`-equivalent logic (rename/generalize it — it already does exactly the right AWS call, just gated on "already failed") while `status == INFRA_PROVISIONING`, and return the per-resource event list alongside the aggregate stack status. `InfraDraft`'s TS type gains a `resource_events: {resource, type, status, reason, timestamp}[]` field. This is the one genuinely new backend capability in this phase — everything else in Stage A/B is wiring the frontend to endpoints that already exist.
2. **Frontend: a shared chat-thread primitive**, extracted once rather than adding a THIRD independent copy of the message-bubble/markdown-rendering code already duplicated between `DevOpsCopilotPanel.tsx` and `DevOpsCopilotDrawer.tsx`. The new infra chat should consume this shared primitive, and — ideally, as a cheap side benefit — the two Copilot files should be refactored onto it too instead of leaving a third duplicate.
3. **Frontend: `InfraBuildTimeline.tsx`** (new), modeled on `StageTimeline.tsx`'s visual pattern, fed by the new `resource_events` field from §7.4.1 (a resource is "pending" until its first event, "active" between `_IN_PROGRESS` and a terminal event, "done"/"failed" on `_COMPLETE`/`_FAILED`).
4. **Frontend: a properly designed resource-confirmation screen/dialog** for Stage B — mostly a design/layout pass over data (`change_set_changes`, cost breakdown, policy checks) that's already fetched today, not new data.
5. **Frontend: the chat-turn wiring** — every existing mutation (edit, check-component, discover-existing) needs its result reworded as a chat bubble instead of updating a form panel in place. No new backend call shapes, just a new presentation layer over the exact same calls this session already built and tested (Phases A/C).

### 7.5 What does NOT change (guardrails, same spirit as §4)

- Every real AWS action stays exactly where it already is: `createInfraChangeSet` (real, zero-risk CFN dry-run) and `executeInfraChangeSet` (real, billable) are not merged, reordered, or auto-triggered by the chat — the chat only gets you TO the confirmation screen, never past it.
- Cost still comes only from `apply_independent_cost` (real AWS Price List) — a chat bubble narrating a cost change reads that same number, never estimates its own.
- The explicit "I understand this creates real, billable resources" confirmation stays mandatory before `executeInfraChangeSet` — moving it into a nicer dialog doesn't make it any less a required, distinct human action.
- `fetch_failure_events`/`describe_stack_events` stays read-only, exactly as it is today — Stage C's live timeline only ever reads AWS state, it never gates or changes what CloudFormation does.

### 7.6 Open questions for Phase F

1. **Does the chat interface replace the structured IntentSpec form fields too** (environment tier, min/max instances, budget, region, the Data/Networking toggles), **or stay scoped to the infra-proposal section only** (topology/edit/add-component/existing-resources), leaving the discrete settings as ordinary form fields? My read: keep the discrete settings as a form — a chat exchange for "what's your monthly budget ceiling" is worse UX than a labeled input, and this phase's actual complaint (per the request) is about the proposal/build experience, not the settings fields above it. Flagged, not assumed.
2. **Polling cadence for Stage C's live event feed** — the existing 3s poll interval was fine for a single status string; a per-resource event list arriving every 3s is more UI churn. Worth deciding whether to keep 3s or slow it down (e.g. 5–8s) now that there's more to visually update each tick.
3. **Should the Copilot duplication (`DevOpsCopilotPanel`/`DevOpsCopilotDrawer`) actually get refactored onto the new shared chat primitive in this same phase, or left alone and only the NEW infra chat uses it?** Doing both at once fixes a real, already-identified duplication problem; doing only the new one is smaller/safer and leaves that cleanup for later. Recommend the smaller path (new component only) unless there's appetite for the larger refactor now.
4. **Chat message persistence** — does the infra chat thread need to survive a page refresh/tab switch (like the AI Log Hygiene fix from earlier in this session, kept in react-query's cache), or is it acceptable for it to reset if the human navigates away mid-conversation, since the actual state of record is still the `InfraDraft` row (any chat message that changed something already produced a durable draft)? Recommend: acceptable to reset — the chat is a presentation of draft history, not the source of truth, and the draft itself is already durable.

### 7.7 Suggested build order for Phase F

1. **F1 — Resource confirmation screen (Stage B).** Smallest, highest-clarity-value, zero new backend work — pure frontend layout over data already fetched.
2. **F2 — Live build view (Stage C).** Backend gap (§7.4.1) first, then `InfraBuildTimeline.tsx` consuming it.
3. **F3 — Chat interface (Stage A).** Largest piece; benefits from F1/F2 already existing since the chat's "looks good" action hands off directly into Stage B/C.

---

No code has been written for Phase F (or anything else in this document). This remains the plan to review and adjust before any of it begins.
