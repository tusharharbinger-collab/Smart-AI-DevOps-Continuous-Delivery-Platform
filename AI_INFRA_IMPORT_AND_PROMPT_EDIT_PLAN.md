# Existing-vs-AI-Created Infrastructure + Prompt-Driven Infra Editing — Plan

*(Answers: "is this possible?" — yes, and the existing CloudFormation Change
Set architecture from `AI_INFRA_PROVISIONING_EXECUTION_PLAN.md` already
supports most of the plumbing this needs. Design only — no implementation
yet.)*

---

## Status (2026-09-27)

**Phases A-D are implemented in code and unit-tested; live AWS verification is NOT done yet** (per this
repo's convention, nothing here counts as done until proven against the real account).

- **A** - migration `0018` (`source`, `existing_resources`, `parent_draft_id`) + `db/schema.sql`.
- **B** - `shared/provisioning/aws_discovery.py` (read-only, archetype-scoped, `describe_selected` re-verifies picks
  against AWS), pipeline-worker `GET /infra-provisioning/discover-existing` + `POST .../describe-existing`,
  api-gateway `GET /infra-drafts/discover-existing`.
- **C** - `generate_infra_proposal(existing_resources=...)` with `_require_retain_on_imports` (a missing
  `DeletionPolicy: Retain` triggers the corrective retry); `preview_changes(import_existing=...)` sends
  `ImportExistingResources=True` (a pure `IMPORT` change set cannot create new resources, so it would break the
  mixed "some existing, some new" case).
- **D** - `POST /infra-drafts/{id}/edit` (new linked row, re-enters the approval gate),
  `_require_retained_resources_preserved` (an edit can never remove or un-retain a Retain resource - enforced in code,
  not just prompted), stack-name inheritance so editing a provisioned draft is an UPDATE of the same stack, and the
  wizard UI (source toggle, discovery picklists, "Edit with AI").

**Live-verified 2026-09-27** (real stack, real AWS read-only, real Groq): migration `0018` applied; discovery returns
the archetype-scoped slots from the real account; an `existing` pick AWS cannot confirm is rejected 422 with nothing
written; `ai_created` still works; a prompt edit ("add an ElastiCache Redis cache") produced a NEW linked draft that
really contains `aws_elasticache_cluster` while the parent stayed untouched.

Two real bugs only live testing found, both fixed and covered by tests: (1) the base prompt's "never add resources
the archetype's shape does not call for" made the model **ignore edits entirely** (returned the proposal
byte-identical) - the human's edit instruction now overrides that restriction in edit mode, and an edit that changes
nothing is rejected so the corrective retry fixes it; (2) an edit sends the whole current proposal (~5,900 tokens
requested) so it cannot fit Groq's 8,000 tokens/minute window right after a create - rate-limit waits are now
bounded (3 x <=15s) and independent of the correction-retry budget (a create-then-edit takes ~40s; still under the
gateway's 70s timeout).

**Real-AWS verification (2026-09-27, throwaway `db.t3.micro`, fully deleted afterwards - account confirmed clean):**
- Discovery listed the real instance; the agent received its VERIFIED real configuration and reproduced it exactly
  (engine 18.3, 20GB, encrypted, private, `DeletionPolicy: Retain`, real identifier).
- A real `ImportExistingResources` change set came back `READY` with `Import DBInstance` + `Add` for the new
  resources - the plan's section 4 risk (template must match the real resource) did not materialize.
- The import **executed for real** (`CREATE_COMPLETE`) through the platform's own `preview_changes`/`execute_changes`.
- An edited draft's change set targeted the **same stack** as an UPDATE (`Add Uploads` only; the imported DB untouched).
- **Deleting the stack that imported the database left the database running** - `Retain` works.
- Live edits: "add an S3 bucket" kept the retained DB; "remove the database and replace it with a brand new one"
  was **rejected by the Retain validator on attempt 1, corrected on the retry, and the result still had the original
  DB retained**.

More live-only findings, fixed and covered by tests: an edit sending the whole proposal got a hard **413** (one
request larger than the plan's whole 8,000 tokens/minute) - the edit prompt is now slimmed (template as an object,
derived/audit parts dropped; measured ~2,500 prompt + ~3,800 completion tokens, accepted at the full 8192
ceiling). Lowering the edit `max_tokens` to 5500 was tried and truncated larger edits, and `reasoning_effort=low` breaks
strict JSON generation - both rejected; a `json_validate_failed` 400 is now retried once; a rejected-then-corrected edit legitimately takes ~75s so the gateway timeout is now
150s, and timeouts no longer surface as an empty message.

**Not verified:** the wizard UI in a browser (type-checks and builds; not clicked through). An execute of the
*agent-generated* full stack (ALB + ECS + DB) was deliberately not run - it would create billable resources; only its
change-set preview was.

---

## §0. The two asks, restated precisely

1. **"Existing vs AI-created" option** — when a human is about to have the
   Infra Architect Agent build infrastructure for a project, let them choose
   between (a) **AI-created**: the agent designs and provisions brand-new
   AWS resources (today's only path), or (b) **existing**: the human points
   at AWS resources that already exist (a real RDS instance, an existing
   ECS cluster, etc.) and the platform brings those under its management
   instead of creating duplicates.
2. **Prompt-driven infra editing** — after a proposal exists (either
   AI-created or an imported existing one), let the human type a free-form
   instruction ("add a Redis cache," "make the database Multi-AZ," "shrink
   the task to 512MB") and have the Infra Architect Agent revise the
   proposal accordingly, subject to the same human-approval gate as any
   other change.

## §R. Research — how the industry does both of these

**1. AWS CloudFormation has a native "import" operation built for exactly
ask #1 — this is not something to build from scratch.** A `CreateChangeSet`
call with `ChangeSetType: IMPORT` maps an existing, unmanaged AWS resource
(identified by its real-world identifier — e.g. a DB instance's
`DBInstanceIdentifier`) onto a logical ID in a CloudFormation template, and
CloudFormation subsequently manages it as if it had created it — without
ever creating or replacing the real resource. Every imported resource
**must** declare `DeletionPolicy: Retain` (CloudFormation enforces this),
which is also exactly the safety property we want: the platform must never
be able to delete a resource the human said pre-existed. AWS also shipped a
newer `ImportExistingResources` parameter on regular (non-IMPORT) change
sets in late 2023, which auto-detects and imports a resource that already
exists instead of failing the change set — useful for a "some resources are
new, some already exist" mixed case. Cross-account and cross-region import
is not supported (the resource must be in the same account/region the
Change Set targets — matches this platform's single-account-per-tenant
model already).
→ *Directly answers ask #1's mechanism.* `shared/provisioning/aws_cloudformation.py`'s
existing `preview_changes()`/`execute_changes()` already wrap
`create_change_set`/`execute_change_set` — they need a new `change_set_type`
branch, not a new subsystem.

**2. Pulumi Neo (Pulumi's 2025-2026 AI agent for infrastructure) is the
closest existing analog to ask #2.** Neo accepts natural-language requests
("find all Lambda functions on deprecated runtimes and upgrade them," "fix
S3 buckets that violate our security policy"), builds an execution plan
that accounts for real dependencies in the target infrastructure, and
**that plan goes through review before anything executes** — "Operating
Modes" range from full human review to autonomous, but review-first is the
default posture. AWS Q Developer's IaC feature works the same shape:
conversational request → generated/modified template → iterative
refinement, never a silent direct-apply.
→ *Validates the plan-then-approve architecture this platform already has*
(Ground Rule 0a, `infra_build_state`'s tiered approval gate) as the correct
posture for ask #2 — an edit prompt should produce a new proposal revision
that re-enters the SAME approval/change-set-preview pipeline every other
proposal goes through, never a direct mutation.

**3. "Attach existing infrastructure" as a first-class onboarding choice
(not just an advanced/enterprise escape hatch) is an established PaaS
pattern.** Porter's Enterprise tier lets a customer attach an existing
Kubernetes cluster instead of provisioning one; Qovery's BYOC (Bring Your
Own Cloud) mode manages databases and services that already live in the
customer's own cloud account rather than always provisioning fresh ones.
→ *Validates ask #1 as a real, wanted feature* in this exact platform
category, not a niche request.

## §1. Design — "Existing vs AI-Created" (ask #1)

### 1.1 New step in the Requirements Form flow

A new toggle, shown before "Preview Infrastructure (AI)":

```
Infrastructure source:
( ) AI-created — the Infra Architect Agent designs new AWS resources     [default, today's behavior]
( ) Existing — attach AWS resources that already exist in your account
```

Choosing **Existing** replaces the "Preview Infrastructure (AI)" button's
call with a two-step flow:

1. **Discover.** A new endpoint, `GET /infra-drafts/discover-existing?archetype=...`,
   calls the same read-only boto3 enumeration pattern already proven live
   this session (the AWS-teardown sweep: `describe_db_instances`,
   `list_clusters`, `describe_load_balancers`, `describe_cache_clusters`)
   scoped to the resource **types** the chosen archetype actually needs
   (e.g. `web_service_with_database` → RDS instances + ECS clusters + ALBs
   only, never everything in the account). Returns a picklist per resource
   type: `{id, name, engine/type, region}` — no AI call involved, pure
   AWS API enumeration.
2. **Confirm selections.** The human picks one resource per required slot
   from the picklist (or leaves a slot as "create new" if, say, they have
   an existing database but want a fresh ECS service). This selection is
   the "locked" input — same posture as the existing IntentSpec: a human
   decision, never inferred.

### 1.2 What the Infra Architect Agent does differently for "existing"

`generate_infra_proposal()` gains an `existing_resources: dict | None`
parameter (a slot → real AWS identifier map from step 1.1.2). When present,
the system instruction changes from "design new resources for this
archetype" to: *"The following resources already exist and must be
imported, never recreated: {existing_resources}. Generate a CloudFormation
template with `DeletionPolicy: Retain` on every imported resource and its
real identifier for the import mapping; only resources NOT in
existing_resources should be newly designed."* The response schema is
unchanged (`InfraGenerationResult` already has everything needed) — this is
a system-instruction and post-validation change, not a new schema.

New validation (`_require_deletion_policy_retain_on_imports()`, mirroring
the existing `_require_no_additional_properties()` pattern): reject any
generated template where an imported resource is missing
`DeletionPolicy: Retain` — this is the one safety property that must never
depend on the model getting it right unprompted.

### 1.3 What execution does differently

`infra_build_state` gains a `source` column (`'ai_created' | 'existing'`,
default `'ai_created'` — existing rows are unaffected). `create_infra_change_set`
(api-gateway) threads `source` through to pipeline-worker's
`POST /infra-provisioning/change-set`, which picks `ChangeSetType: IMPORT`
(pure-import case) or a normal `UPDATE`/`CREATE` change set with
`ImportExistingResources: true` (mixed case) instead of always `CREATE`.
`shared/provisioning/aws_cloudformation.py`'s `preview_changes()` needs one
new parameter (`change_set_type`) threaded into its existing
`create_change_set` call — the polling/status/execute logic is already
identical for every change set type, so `execute_changes()`/`check_status()`
need no changes at all.

### 1.4 What this does NOT do

Never imports a resource the human didn't explicitly pick from the
discovery picklist (no "auto-detect and adopt anything that looks
related" — that's how you accidentally adopt someone else's production
database). Never removes `DeletionPolicy: Retain` from an imported resource
on a later edit (ask #2) — once imported, always retained, permanently.

## §2. Design — Prompt-driven infra editing (ask #2)

### 2.1 New endpoint

`POST /infra-drafts/{draft_id}/edit` (api-gateway), body: `{"instruction": "add a Redis cache"}`.
Requires the draft to be in any non-terminal-provisioning state (not
`INFRA_PROVISIONING` itself — an edit mid-apply is a race, reject with 409).

### 2.2 What it calls

A new explainability-service function, `edit_infra_proposal(current_proposal, instruction, intent_spec, archetype)`
— structurally identical to `generate_infra_proposal()` (same Groq call
shape, same `InfraGenerationResult` schema, same
`_repair_or_reject_cloudformation_json`/retry/rate-limit handling this
session just hardened) but with a different system instruction: *"You are
EDITING an existing infrastructure proposal, not designing from scratch.
Current proposal: {current_proposal}. Apply ONLY this change: {instruction}.
Preserve every other resource, topology edge, and cost line exactly unless
the instruction requires touching it. If the current proposal has any
resource with `DeletionPolicy: Retain` (an imported, pre-existing
resource), you MUST NOT remove, replace, or recreate it — only add new
resources or modify a NON-retained resource's properties."* This is the
single most important safety line in the whole feature: an edit must never
be able to silently turn an "import my existing DB" choice into "delete and
recreate my existing DB."

### 2.3 What happens to the result

The edited proposal becomes a NEW `infra_build_state` row (never overwrites
the old one in place — an edit history is an audit trail, same principle as
why `pipeline_executions` rows are never mutated after creation), with
`parent_draft_id` pointing at the draft it edited (new nullable column).
It re-runs `evaluate_deployment_readiness()` and re-enters the SAME tiered
approval gate as any fresh proposal — a `warn`/`require_approval` outcome
pauses for a human click exactly like today, `auto_advance` still requires
the existing "Use this infrastructure" explicit action added this session.
**An edit is never auto-applied, full stop** — this is Ground Rule 0a
applied to a new entry point, not a new rule.

If the ORIGINAL draft was already `INFRA_PROVISIONED` (real resources
exist), the edited proposal's `create-change-set` step naturally becomes a
normal CloudFormation **UPDATE** change set against the *same, already-existing
stack name* — `shared/provisioning/aws_cloudformation.py`'s `_stack_exists()`
check already branches on exactly this, so a live "modify my running
infra via a prompt" is not new execution logic, just a new
proposal-generation entry point feeding the pipeline that already exists.

### 2.4 Frontend

`RequirementsForm.tsx` (and, per this session's earlier addition, wherever
a project's persisted infra draft is later viewable) gains a text input +
"Edit with AI" button next to the topology graph, visible whenever a draft
exists. Submitting shows the edited proposal exactly like a fresh one
(topology graph, cost breakdown, policy checks, the "Use this
infrastructure" button) — reusing 100% of the rendering already built this
session, since the response shape is unchanged.

## §3. Build order

- **Phase A** — `infra_build_state.source`/`parent_draft_id` columns
  (migration), `_require_deletion_policy_retain_on_imports()` validator.
- **Phase B** — `GET /infra-drafts/discover-existing` (read-only boto3
  enumeration, no AI call — lowest-risk, ship first).
- **Phase C** — `generate_infra_proposal(existing_resources=...)` +
  `ChangeSetType: IMPORT`/`ImportExistingResources` threading through
  `aws_cloudformation.py`. Test against a real, disposable AWS resource
  (a throwaway RDS instance) before trusting it against anything real.
- **Phase D** — `edit_infra_proposal()` + `POST /infra-drafts/{id}/edit` +
  frontend "Edit with AI" input. Ship after Phase C since the edit
  instruction's "never touch a Retain resource" safety rule is only
  testable once real imported resources exist to protect.

## §4. Open risk to flag explicitly

CloudFormation IMPORT requires the template's logical-ID shape to exactly
match how the real resource is configured in some respects (e.g. you can't
import an RDS instance under a logical ID whose template properties
contradict the instance's real, immutable properties like engine or
storage type) — the Infra Architect Agent's generated template for an
imported resource must describe the resource **as it actually is**, not as
the archetype would ideally want it. This is a real "the AI must observe
before it proposes" constraint: Phase C's discovery step (§1.1.1) should
capture enough of each candidate resource's actual configuration (engine,
instance class, VPC) to hand to the agent as ground truth, not just its ID.

---

Sources:
- [Import AWS resources into a CloudFormation stack](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/import-resources.html)
- [Importing existing resources into a stack](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/resource-import-existing-stack.html)
- [Import AWS resources into a CloudFormation stack manually](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/import-resources-manually.html)
- [AWS CloudFormation simplifies resource import with a new parameter for ChangeSets (ImportExistingResources)](https://aws.amazon.com/about-aws/whats-new/2023/11/aws-cloudformation-import-parameter-changesets)
- [CreateChangeSet API Reference](https://docs.aws.amazon.com/AWSCloudFormation/latest/APIReference/API_CreateChangeSet.html)
- [Import existing resources into a CloudFormation stack — AWS re:Post best practices](https://repost.aws/knowledge-center/cfn-best-practices-for-importing-resources)
- [Pulumi Neo docs](https://www.pulumi.com/docs/pulumi-cloud/neo/)
- [Pulumi bets infrastructure's next decade belongs to AI agents — The New Stack](https://thenewstack.io/pulumi-infrastructure-agent-era/)
- [Pulumi AI Predictions for 2026: A DevOps Engineer's Guide](https://www.pulumi.com/blog/ai-predictions-2026-devops-guide/)
- [Amazon Q Developer features](https://aws.amazon.com/q/developer/features/)
- [Amazon Q Developer best practices for code generation](https://docs.aws.amazon.com/prescriptive-guidance/latest/best-practices-code-generation/code-generation.html)
- [Qovery: We Started on a Managed PaaS, Now We Need SOC 2 and Our Own VPC (BYOC options)](https://www.qovery.com/blog/soc-2-own-vpc-where-to-go-after-managed-paas-byoc-options)
- [Northflank: Best options for BYOC in cloud computing in 2026](https://northflank.com/blog/best-options-for-byoc-in-cloud-computing)
