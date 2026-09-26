# AI Infra Provisioning — Real Execution Plan (AWS now, multi-cloud-ready later)

*(Answers the open question left at the end of `AI_AGENTIC_ORCHESTRATION_PLAN.md`'s Phase 4: the Infra Architect Agent currently only PROPOSES infrastructure — a topology, Terraform text, a cost estimate — nothing it generates is ever executed. This plan is how that proposal becomes real, created AWS resources, scoped to AWS only for now but designed so another cloud can be added later without redesigning the approval flow. Design only — no code written yet.)*

---

## 1. Research — How Every Real Vendor Actually Gates This

Researched (web, Sept 2026): Pulumi Neo (the most aggressive "AI builds your infra" product on the market), Harness IaCM, 2026 AI-code-security data, Terraform-vs-CloudFormation tradeoffs, multi-tenant Terraform state-management practice.

**1. No vendor — not even the most agent-forward one — lets an AI agent apply directly and unsupervised.**
Pulumi Neo's own architecture: policy-as-code runs on the **preview output**, whether a human or the agent proposed the change — non-compliant changes are blocked before they ever reach the cloud. Neo operates strictly within the *initiating human's own RBAC permissions*; it cannot escalate privilege or do anything that human couldn't already do themselves. "Review mode, the default, requires human approval at every step." Harness IaCM's pipeline is structurally identical: explicit approval steps sit *between* `plan` and `apply` regardless of who or what authored the plan.
→ **This confirms the platform's existing Ground Rule 0a is not a compromise — it's the actual production pattern**, independently converged on by the two most relevant vendors in this space.

**2. The security data argues directly against ever executing raw AI-generated IaC unsupervised.**
2026 flagship models (GPT-5.x, Gemini 3, Claude 4.6) produce secure code only ~55% of the time out of the box; IAM/permission reasoning across many interdependent resources is exactly the class of "contextual reasoning across a whole system" problem LLMs are weakest at. A hardcoded credential, an overly permissive security group, or a wrong dependency order in AI-generated infra code propagates directly into a real, billable, exploitable AWS account if nothing deterministic stands between generation and execution.

**3. Terraform's per-tenant state management is a real, avoidable burden for an AWS-only platform.**
Practitioner consensus: safe multi-tenant Terraform needs a remote backend (S3 + DynamoDB locking) and a workspace-or-state-file-per-tenant strategy, specifically to stop one tenant's `apply` from corrupting another's state. This is real infrastructure this platform would have to build and operate just to use Terraform as the execution engine — not a cost inherent to "AI generates infra," a cost specific to *choosing Terraform* as the executor.

**4. AWS CloudFormation removes that burden entirely, for an AWS-only platform, using nothing but the library this codebase already depends on everywhere.**
CloudFormation is AWS-native: AWS itself tracks each stack's state (no external backend, no locking to build), and every action goes through `boto3` — the exact library `aws_ecs_actuation.py`/`ecs_onboarding.py` already use for everything else. Crucially, CloudFormation's **Change Sets** (`create_change_set`) are AWS's own built-in equivalent of `terraform plan` — computing exactly what would change with zero risk, before anything executes — satisfying the "preview before apply" requirement every vendor above treats as non-negotiable, natively, with no new tooling. Failed stacks **roll back automatically** — a property a hand-written sequence of individual boto3 calls (create RDS, then ECS, then an ALB rule...) would have to reimplement manually for partial-failure cleanup.

**Conclusion this plan is built on**: the Infra Architect Agent's output becomes a **CloudFormation template**, not an executed Terraform plan — reviewed via a real Change Set, approved by a human, then executed via `boto3`. This is materially safer, needs no new state-management infrastructure, and reuses the exact library and AWS-account model already proven in this codebase.

---

## 2. Scope Decision: AWS Now, Multi-Cloud-Ready Later

**AWS is the only implementation for now** (matches the existing platform-wide deploy-target decision already recorded in `CLAUDE.md`: ECS Fargate is the sole focus for new work). But the execution layer is designed behind a narrow interface so a second cloud can be added later **without touching the approval/gating state machine at all** — only a new implementation of that interface.

```python
# shared/provisioning/base.py (the only new abstraction this plan introduces)
class InfraProvisioner(Protocol):
    def generate_template(self, proposal: InfraProposal) -> str: ...
    def preview_changes(self, template: str, stack_name: str) -> ChangePreview: ...
    def execute_changes(self, preview: ChangePreview) -> ProvisioningResult: ...
    def check_status(self, execution_id: str) -> ProvisioningStatus: ...
```

- **`AwsCloudFormationProvisioner`** — the only concrete implementation built now. `generate_template()` asks the Infra Architect Agent (Groq) for a CloudFormation template instead of/alongside the existing Terraform text (Terraform stays as a human-readable summary in the UI if useful — it is never the thing executed). `preview_changes()` calls `create_change_set`. `execute_changes()` calls `execute_change_set`. `check_status()` polls stack events.
- **Future**: an `AzureArmProvisioner` or `GcpDeploymentManagerProvisioner` would implement the same four methods against their own native IaC primitive (ARM/Bicep change previews, GCP Deployment Manager previews) — each cloud's own "Change Set" equivalent, same reasoning as §1.4, not necessarily Terraform even then. The state machine, approval endpoints, readiness gate (Phase 3), and UI never need to know which provisioner is behind the interface — they only ever call these four methods and read `IntentSpec.aws_region`-equivalent to pick a provisioner instance. This is the entire "multi-cloud-ready" story — a real interface with one real implementation, not speculative multi-cloud code that's never exercised.

---

## 3. State Machine Extension

Extends `infra_build_state` (migration `0016`, already built) — new states **after** `INFRA_APPROVED`, which is where the existing Phase 4 work currently stops:

```
INFRA_APPROVED
  → INFRA_CHANGE_SET_CREATING   (calling generate_template() + preview_changes())
  → INFRA_CHANGE_SET_READY      (a real AWS Change Set exists; human sees the real diff)
  → INFRA_EXECUTION_APPROVED    (a SECOND, explicit human approval — reviewing a cost/topology
                                  proposal is not the same act as authorizing real spend; these
                                  are deliberately two separate approval clicks, not one)
  → INFRA_PROVISIONING          (execute_changes() called, stack is being created)
  → INFRA_PROVISIONED           (terminal success — real resources exist)
  → INFRA_PROVISIONING_FAILED   (terminal failure — CloudFormation's own rollback already
                                  reverted partial changes; this state just records why)
```

New columns on `infra_build_state`: `cloud_provider` (`'aws'` only for now, but a real column from day one — not hardcoded — so a future provider is a data value, not a schema migration), `change_set_id`, `stack_name`, `stack_arn`, `provisioning_error`.

---

## 4. New Endpoints (api-gateway)

- `POST /infra-drafts/{draft_id}/create-change-set` — only valid from `INFRA_APPROVED`. Calls the provisioner's `generate_template()` + `preview_changes()`, stores the Change Set id, advances to `INFRA_CHANGE_SET_READY`.
- `GET /infra-drafts/{draft_id}/change-set` — returns the real AWS Change Set diff (resources to be added/modified/removed) for the UI to render.
- `POST /infra-drafts/{draft_id}/execute` — role-gated (`lead-sre`, matching the existing approval endpoint's convention), only valid from `INFRA_CHANGE_SET_READY`. This is the second, distinct approval — advances to `INFRA_EXECUTION_APPROVED` then immediately triggers `execute_changes()` → `INFRA_PROVISIONING`.
- `GET /infra-drafts/{draft_id}/status` — polls `check_status()`, updates `INFRA_PROVISIONED`/`INFRA_PROVISIONING_FAILED` when the stack settles.

## 5. Frontend Changes

- `InfraTopologyGraph.tsx` (already built) gains a second mode: after `INFRA_CHANGE_SET_READY`, render the **real Change Set diff** (resources to add, in green; nothing to remove on a first execution) instead of just the AI's proposed topology — this is the human's actual "here's what's about to really happen" moment, distinct from the earlier "here's what the AI thinks should exist" preview.
- A second, explicitly-labeled confirmation ("Execute — this will create real, billable AWS resources") gates the `execute` call — never the same click as approving the design proposal.
- A polling status view (stage-timeline-style, reusing the visual language of `StageTimeline.tsx`) shows `INFRA_PROVISIONING → INFRA_PROVISIONED`/`FAILED` live.

---

## 6. What This Plan Deliberately Does Not Do

- **Does not replace `ecs_onboarding.py`.** That path (creating a project's ECS service/target group on the existing shared `smartcd-platform-alb`/cluster) keeps working exactly as it does today — this plan is for provisioning the *dedicated* resources (RDS, ElastiCache, S3, or eventually a dedicated cluster/ALB) the Infra Architect Agent proposes, layered alongside it, not instead of it.
- **Does not build Terraform execution.** Per §1's research, Terraform is the wrong tool for an AWS-only platform's execution layer specifically because of per-tenant state management — the generated Terraform text (already built in Phase 4) stays a human-readable artifact only.
- **Does not build a second cloud provider.** The interface in §2 exists so that work is additive later; nothing here builds Azure/GCP support now.

---

## 7. Phased Build Order

**Phase 7a — CloudFormation template generation**
- Extend `infra_generator.py`'s system prompt/schema so `InfraGenerationResult` includes a `cloudformation_template` field (JSON string) alongside the existing `iac_terraform` field — same native structured-output discipline already used for the rest of that schema.

**Phase 7b — `AwsCloudFormationProvisioner` + state machine extension**
- New `shared/provisioning/base.py` (the `InfraProvisioner` protocol) and `shared/provisioning/aws_cloudformation.py`.
- Migration extending `infra_build_state` with the new columns and status values from §3.
- The four new api-gateway endpoints from §4.

**Phase 7c — Frontend: real Change Set review + execute + status**
- The graph's second mode, the explicit second-approval UI, and the polling status view from §5.

**Phase 7d — Tests**
- Unit tests for `AwsCloudFormationProvisioner` (mocked `boto3` client, matching this codebase's existing `_FakeAsyncClient`-style test doubles) covering: template generation, change-set creation, execution, and status polling — plus the two-approval state-machine transitions (rejecting `execute` from any state other than `INFRA_CHANGE_SET_READY`, mirroring the existing `approve_infra_draft` 409 guard).
- One live-verified run against a real AWS account (same discipline as this session's Phase 4/6 live testing) before calling this done — mocked tests alone were not sufficient evidence for any other phase in this project and shouldn't be treated as sufficient here either.

---

## 7A. Real Cost Calculation + Real Policy Enforcement (BUILT and live-verified 2026-09-26 — see PROJECT_STATUS.md 9.10)

> Status: 7e was built as an AWS Price List calculator (`shared/provisioning/aws_pricing.py`) instead of Infracost, and 7f as `policies/infra_guardrails.rego` evaluated by api-gateway before approval. The model's own cost/policy claims are kept only as labelled `ai_*` fields. The text below is the original plan.

**Honest status check**: `infra_generator.py`'s `estimated_monthly_cost_usd`/`cost_breakdown` and `policy_checks` are all numbers/verdicts **the LLM invented as part of its own JSON response** — not independently computed or verified by anything. This was fine as a placeholder while the execution layer (§1–§7) didn't exist yet, but now that real resources actually get created, self-reported AI numbers are not an acceptable substitute for real tools, and this platform already holds a stricter standard everywhere else (verification is real statistical tests, never an LLM's opinion — invariant 1).

**Research finding**: AWS's own `CloudFormation.estimate_template_cost()` boto3 call exists but returns a link to AWS's "Simple Monthly Calculator," a tool AWS deprecated years ago — it's a legacy API that no longer reliably works and isn't worth building against. The correct tool is **Infracost** — an open-source CLI purpose-built for this: it parses Terraform HCL (which `infra_generator.py` already produces) against a real, weekly-updated pricing database covering AWS/Azure/GCP, and returns exact calculated costs per resource, not a guess. ([Infracost docs](https://www.infracost.io/docs/faq/))

### What gets built

**Real cost calculation — replaces the AI's guessed numbers, doesn't just add a second opinion alongside them:**
- New `shared/costing/infracost_client.py` — shells out to the `infracost` CLI (or calls its `breakdown` command with `--format json`) against the AI-generated `iac_terraform`, parses the real `totalMonthlyCost` and per-resource breakdown.
- `INFRA_DRAFTING`'s flow (in `create_infra_draft`) is extended: after Groq returns its proposal, **overwrite** `estimated_monthly_cost_usd`/`cost_breakdown` with Infracost's real numbers before the row is ever persisted or shown to a human — the AI's own cost guess never reaches the UI.
- Needs `INFRACOST_API_KEY` (Infracost's free-tier Cloud Pricing API key, same pattern as `GROQ_API_KEY`) and the `infracost` binary available inside `explainability-service`'s container (a `Dockerfile` addition, similar to how this repo already vendors `bin/opa.exe` for Windows).

**Real policy enforcement — replaces the AI's self-reported `policy_checks`:**
- New `policies/infra_guardrails.rego` (a new package, e.g. `infra.guardrails`), evaluated against the **topology JSON** (not the Terraform text — a rego policy over structured JSON is exactly what `delivery_guardrails.rego` already does for verdicts, and this platform's OPA server already runs and is already called this way from `policy-controller`'s `opa_evaluator.py` — same integration pattern, new policy file). Real rules from day one, matching what the AI currently only *claims* to check: encryption-at-rest required for any RDS/S3 node, no public database access (no `0.0.0.0/0` ingress on a DB security group), mandatory resource tags, and a hard cost ceiling tied to the locked `IntentSpec.monthly_budget_usd`.
- Called the same place Infracost is — right after `INFRA_DRAFTING`, before a human ever sees the proposal. The AI's own `policy_checks` field can stay in the response as an *explanatory* narrative (useful context), but the real pass/fail that gates `auto_advance`/`warn`/`require_approval` (Phase 3's existing tiered logic) must come from this real OPA evaluation, not the LLM's self-report.
- This closes a real gap against invariant 1's own stated principle — extended here from "never a static threshold in verification" to "never trust the AI's own claim about its own compliance."

### Phased build order for this addition

**Phase 7e — Infracost integration**
- `infracost` binary + `INFRACOST_API_KEY` wired into `explainability-service`'s container.
- `infracost_client.py` + wiring into `create_infra_draft` to overwrite the AI's cost fields with real ones.
- Tests: mocked subprocess/CLI output (matching this codebase's existing pattern of never calling a real external CLI/API in unit tests).

**Phase 7f — Real OPA infra policy**
- `policies/infra_guardrails.rego` + `policies/tests/infra_guardrails_test.rego` (OPA's own test format, matching `guardrails_test.rego`'s existing convention).
- A new evaluator call (either a new function in `shared/` calling the existing OPA server's REST API, or reusing `policy-controller`'s existing `opa_evaluator.py` pattern) wired into `create_infra_draft`, feeding the topology JSON + `IntentSpec` as OPA input.
- Phase 3's `evaluate_deployment_readiness()` stays as-is for its existing signals (detection confidence, archetype) but now also folds in the real OPA verdict as a `require_approval`-forcing condition on any policy failure — never overridden by an `auto_advance` outcome.

**Is this possible?** Yes, entirely — both tools (Infracost, OPA) are standard, already either used in this codebase (OPA) or a well-established open-source CLI (Infracost) with no exotic integration required. Neither needs new architecture; both slot into the exact same `create_infra_draft` flow already built in Phase 4/7a–7d.

---

## Status — ✅ IMPLEMENTED (Phases 7a–7c; 7d partially — mocked tests done, no live AWS run yet)

**7a** — `infra_generator.py`'s `InfraGenerationResult` gained `cloudformation_template` (a real CFN JSON template, alongside the existing audit-only `iac_terraform`); system prompt updated to require it cover every topology node.

**7b** — `shared/provisioning/base.py` (the `InfraProvisioner` protocol) + `shared/provisioning/aws_cloudformation.py` (the real, only implementation — `preview_changes`/`execute_changes`/`check_status`, all real `boto3` CloudFormation calls: `create_change_set`, `execute_change_set`, `describe_stacks`). Migration `0017` extends `infra_build_state` with `cloud_provider`/`change_set_id`/`stack_name`/`stack_arn`/`change_set_changes`/`provisioning_error`/`provisioning_outputs` and six new status values (`INFRA_CHANGE_SET_CREATING/READY/FAILED`, `INFRA_EXECUTION_APPROVED`, `INFRA_PROVISIONING`, `INFRA_PROVISIONED`, `INFRA_PROVISIONING_FAILED`) — `db/schema.sql` kept in sync. Three new pipeline-worker endpoints (`/infra-provisioning/change-set`, `/execute`, `/status`) and four new api-gateway endpoints (`create-change-set`, `execute`, `status`, plus the existing `approve`) implementing the exact two-approval flow from §3.

**7c** — `RequirementsForm.tsx` gained the full real-provisioning UI: "Preview Real AWS Changes" (once `INFRA_APPROVED`) → renders the actual Change Set diff (real resource actions/types/ids) → a mandatory "I understand this creates real, billable AWS resources" checkbox gating a destructive-styled "Execute" button → live polling with a progress indicator → a success panel showing real stack outputs, or a clear failure banner. `infraDrafts.ts` extended with the matching typed client functions.

**7d** — 31 new tests across three services (11 `aws_cloudformation.py` boto3-mocked, 7 pipeline-worker endpoint, 13 api-gateway endpoint) — all passing (full suites: api-gateway 246/246, pipeline-worker 203/203, explainability-service 53/53). **No live AWS CloudFormation run has happened yet** — this dev environment's AWS credentials were exposed and rotation was still pending as of this implementation, so real execution against a live account remains genuinely unverified, unlike every other phase in this project (which were all live-tested before being called done). Treat this phase as code-complete but not yet proven the way Phases 4–6 were.

**Two real bugs found and fixed while smoke-testing the new routes against the live (mocked-AWS-credentials) stack**, beyond the unit-tested boto3 logic itself:
- The router handlers must never call `db.commit()` themselves (same class of bug as Phase 4's original finding, now also present initially in `create_infra_draft`'s Phase 7 additions before being caught the same way).
- **`WHERE draft_id = :draft_id` against a UUID column raises an opaque 500 (`asyncpg.exceptions.DataError`) for a non-UUID path parameter**, instead of a clean 404 — found by smoke-testing a deliberately malformed draft id against the real endpoint. Fixed with a new `_require_valid_uuid_or_404()` helper applied to all five draft_id-taking endpoints (including the pre-existing Phase 4 ones, which had the identical latent bug, just never exercised with a malformed id before this).

**What's still not connected** (unchanged from the original plan): `ecs_onboarding.py`'s shared-ALB path is untouched; no second cloud provider exists; the approved-and-provisioned infra draft still isn't linked back to a project record after `POST /projects`.

**Newly identified gap (§7A, flagged 2026-09-24, not yet built)**: the cost estimate and policy checks a human currently reviews before approving a draft are **entirely self-reported by the LLM** — not computed by Infracost, not verified by OPA. This is real work still to do (Phases 7e–7f), not a rounding error — treat §7A as equally important as 7a–7d, since "the human approved a real spend based on a number the AI made up" is exactly the kind of gap this platform's own invariants exist to prevent everywhere else.

---

## Sources
- [Meet Neo, Your Newest Platform Engineer — Pulumi Blog](https://www.pulumi.com/blog/pulumi-neo/)
- [What Is Agentic Infrastructure? — Pulumi](https://www.pulumi.com/what-is/what-is-agentic-infrastructure/)
- [Harness Infrastructure as Code Management (IaCM) Overview](https://developer.harness.io/infrastructure-as-code-management/new-to-iacm/overview)
- [AI Agents Are Writing Your Infrastructure Code. Is Anyone Governing It? — DevOps.com](https://devops.com/ai-agents-are-writing-your-infrastructure-code-is-anyone-governing-it/)
- [Terraform vs. CloudFormation: A Side-by-Side Comparison — KodeKloud](https://kodekloud.com/blog/terraform-vs-cloudformation/)
- [How to Manage Terraform State for Multi-Tenancy — OneUptime](https://oneuptime.com/blog/post/2025-12-18-terraform-state-multi-tenancy/view)
- [FAQ | Infracost](https://www.infracost.io/docs/faq/)
- [EstimateTemplateCost - AWS CloudFormation (the legacy, effectively non-functional API considered and rejected)](https://docs.aws.amazon.com/AWSCloudFormation/latest/APIReference/API_EstimateTemplateCost.html)
