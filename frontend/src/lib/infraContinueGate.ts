/**
 * frontend/src/lib/infraContinueGate.ts
 *
 * AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md Phase E — real gap found live: the wizard's "Continue" from
 * the Requirements step used to check ONLY `environment_tier`, with no check at all on the infra draft's
 * actual status. A human could generate a real AI infra proposal (or attach existing resources) and
 * Continue straight to project creation without ever approving OR provisioning it.
 *
 * The rule: a draft declaring `no_additional_infrastructure: true` has no provisioning step at all (the
 * platform's own standard topology is built separately at project creation) — INFRA_APPROVED is its
 * correct, permanent terminal state. Only a draft that DOES add real infrastructure is held to the
 * stricter INFRA_PROVISIONED bar. No draft at all → nothing to enforce (infra is optional, not mandatory,
 * in this flow).
 *
 * Extracted as a pure function (rather than inlined in NewProject.tsx) so it's testable without mounting
 * the whole multi-step wizard, which has many unrelated API dependencies (repo detection, pipeline
 * generation, project creation).
 */
import type { InfraDraft, InfraDraftStatus } from "@/api/infraDrafts";

// Real bug found live: every non-terminal status used to fall through to one generic message ("approve it
// and run Execute above, not just preview it") regardless of WHICH status the draft was actually in - for
// INFRA_CHANGE_SET_FAILED that's actively wrong (you cannot "Execute" a failed change set; the real next
// step is to fix the proposal and regenerate). Each real status gets its own accurate, actionable message.
const STATUS_GUIDANCE: Partial<Record<InfraDraftStatus, string>> = {
  INFRA_DRAFTING: "The proposal is still being generated — wait a moment before continuing.",
  INFRA_DRAFT_FAILED: "The proposal failed to generate — check the error above, adjust the Requirements, and try again before continuing.",
  INFRA_PENDING_APPROVAL: 'This proposal is still pending approval — review it above and click "Looks good" to continue.',
  INFRA_APPROVED: 'This proposal is approved but not yet built — review the real resource list above and click "Confirm & Build".',
  INFRA_CHANGE_SET_CREATING: "Computing the real AWS change set — wait a moment before continuing.",
  INFRA_CHANGE_SET_READY: 'The change set is ready but nothing has been built yet — click "Confirm & Build" above.',
  INFRA_CHANGE_SET_FAILED: "The change set failed validation — use the suggested fix above (or describe a fix in the chat) and try again before continuing.",
  INFRA_PROVISIONING: "Your infrastructure is still being built — wait for it to finish before continuing.",
  INFRA_PROVISIONING_FAILED: "Provisioning failed — review the failure analysis above, fix the proposal, and try again before continuing.",
};

export function computeInfraBlockReason(draft: InfraDraft | null): string | null {
  if (!draft) return null;

  const needsNoProvisioning = draft.infra_proposal?.no_additional_infrastructure === true;
  if (needsNoProvisioning) {
    if (draft.status === "INFRA_APPROVED" || draft.status === "INFRA_PROVISIONED") return null;
    return `This infrastructure proposal is still ${draft.status} — use "Preview Infrastructure (AI)" above to finish it before continuing.`;
  }

  if (draft.status === "INFRA_PROVISIONED") return null;
  return STATUS_GUIDANCE[draft.status] ?? `Your infrastructure isn't ready yet (currently ${draft.status}).`;
}
