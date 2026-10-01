/**
 * AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md Phase E — the real gap this closes: the wizard's Continue
 * button used to allow proceeding to project creation regardless of whether a generated infra proposal had
 * ever been approved or provisioned. These tests cover every real InfraDraft shape the wizard can be
 * holding at the moment a human clicks Continue.
 */
import { describe, expect, it } from "vitest";
import { computeInfraBlockReason } from "./infraContinueGate";
import type { InfraDraft } from "@/api/infraDrafts";

function draft(status: InfraDraft["status"], noAdditionalInfra = false): InfraDraft {
  return {
    draft_id: "draft-1",
    project_id: null,
    status,
    intent_spec: {} as InfraDraft["intent_spec"],
    archetype: "stateless_web_service",
    infra_proposal: { no_additional_infrastructure: noAdditionalInfra } as InfraDraft["infra_proposal"],
    readiness_outcome: null,
    readiness_reasons: [],
    error_message: null,
    cloud_provider: "aws",
    change_set_id: null,
    stack_name: null,
    stack_arn: null,
    change_set_changes: [],
    provisioning_error: null,
    provisioning_outputs: {},
    source: "ai_created",
    existing_resources: {},
    parent_draft_id: null,
    aws_connection_id: null,
    created_at: null,
    updated_at: null,
  };
}

describe("computeInfraBlockReason", () => {
  it("never blocks when the user hasn't gone through the AI infra flow at all", () => {
    expect(computeInfraBlockReason(null)).toBeNull();
  });

  it("blocks a real (additional-infrastructure) draft that is only approved, not provisioned, with accurate guidance", () => {
    const reason = computeInfraBlockReason(draft("INFRA_APPROVED", false));
    expect(reason).toMatch(/Confirm & Build/);
  });

  it("allows continuing once a real draft reaches INFRA_PROVISIONED", () => {
    expect(computeInfraBlockReason(draft("INFRA_PROVISIONED", false))).toBeNull();
  });

  it("blocks a real draft still pending approval, telling the human to use the chat's Looks good action", () => {
    expect(computeInfraBlockReason(draft("INFRA_PENDING_APPROVAL", false))).toMatch(/Looks good/);
  });

  it("real bug fix: a FAILED change set gets accurate guidance, never the generic 'run Execute' message", () => {
    // Found live: this used to say "approve it and run Execute above" for a change set that had already
    // FAILED validation - nonsensical, since a failed change set can't be executed at all.
    const reason = computeInfraBlockReason(draft("INFRA_CHANGE_SET_FAILED", false));
    expect(reason).toMatch(/change set failed validation/);
    expect(reason).toMatch(/suggested fix/);
    expect(reason).not.toMatch(/run "Execute"/);
  });

  it("gives distinct, accurate guidance for every other real non-terminal status", () => {
    expect(computeInfraBlockReason(draft("INFRA_DRAFT_FAILED", false))).toMatch(/failed to generate/);
    expect(computeInfraBlockReason(draft("INFRA_CHANGE_SET_READY", false))).toMatch(/Confirm & Build/);
    expect(computeInfraBlockReason(draft("INFRA_PROVISIONING", false))).toMatch(/still being built/);
    expect(computeInfraBlockReason(draft("INFRA_PROVISIONING_FAILED", false))).toMatch(/Provisioning failed/);
  });

  it("allows continuing at INFRA_APPROVED for a standard-only (no_additional_infrastructure) draft, which never provisions", () => {
    expect(computeInfraBlockReason(draft("INFRA_APPROVED", true))).toBeNull();
  });

  it("still blocks a standard-only draft that hasn't even reached INFRA_APPROVED yet", () => {
    const reason = computeInfraBlockReason(draft("INFRA_PENDING_APPROVAL", true));
    expect(reason).toMatch(/Preview Infrastructure \(AI\)/);
  });

  it("allows continuing at INFRA_PROVISIONED too for a standard-only draft (belt-and-suspenders)", () => {
    expect(computeInfraBlockReason(draft("INFRA_PROVISIONED", true))).toBeNull();
  });
});
