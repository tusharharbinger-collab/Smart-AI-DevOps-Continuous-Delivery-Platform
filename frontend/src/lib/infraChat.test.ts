/**
 * AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §7 (Phase F) — infraChat.ts turns a real InfraDraft into
 * plain-language chat text. Every assertion here checks the narration reads real numbers off the draft,
 * never inventing its own.
 */
import { describe, expect, it } from "vitest";
import { describeEdit, describeNewProposal } from "./infraChat";
import type { InfraDraft } from "@/api/infraDrafts";

function draft(overrides: Partial<InfraDraft> = {}): InfraDraft {
  return {
    draft_id: "d1", project_id: null, status: "INFRA_PENDING_APPROVAL",
    intent_spec: {} as InfraDraft["intent_spec"], archetype: "stateless_web_service",
    infra_proposal: {
      name: "proposal", topology: { nodes: [], edges: [] }, iac_terraform: "", estimated_monthly_cost_usd: 0,
      cost_breakdown: [], policy_checks: [],
    },
    readiness_outcome: null, readiness_reasons: [], error_message: null, cloud_provider: "aws",
    change_set_id: null, stack_name: null, stack_arn: null, change_set_changes: [], provisioning_error: null,
    provisioning_outputs: {}, source: "ai_created", existing_resources: {}, parent_draft_id: null,
    aws_connection_id: null, created_at: null, updated_at: null,
    ...overrides,
  };
}

describe("describeNewProposal", () => {
  it("reads the real cost and its source, never inventing a number", () => {
    const d = draft({
      infra_proposal: {
        name: "p", topology: { nodes: [{ id: "db", type: "AWS::RDS::DBInstance", label: "Database" }], edges: [] },
        iac_terraform: "", estimated_monthly_cost_usd: 42.5,
        cost_breakdown: [], policy_checks: [],
        cost_estimate: { source: "aws-price-list" },
        needs_summary: "This app needs a database.",
      },
    });
    const text = describeNewProposal(d);
    expect(text).toContain("This app needs a database.");
    expect(text).toContain("$42.50/mo");
    expect(text).toContain("from real AWS prices");
  });

  it("states policy denial plainly when the proposal is blocked", () => {
    const d = draft({
      infra_proposal: {
        name: "p", topology: { nodes: [], edges: [] }, iac_terraform: "", estimated_monthly_cost_usd: 10,
        cost_breakdown: [], policy_checks: [],
        policy_evaluation: { engine: "opa", policy: "x", allowed: false, deny: [{ rule: "r", resource: "res", severity: "deny", message: "encryption required" }], warn: [] },
      },
    });
    expect(describeNewProposal(d)).toContain("Blocked by policy: encryption required");
  });

  it("says nothing extra is needed for a standard-only proposal, with no cost line", () => {
    const d = draft({
      infra_proposal: {
        name: "p", topology: { nodes: [], edges: [] }, iac_terraform: "", estimated_monthly_cost_usd: 0,
        cost_breakdown: [], policy_checks: [], no_additional_infrastructure: true,
        needs_summary: "Nothing beyond the platform's standard build.",
      },
    });
    const text = describeNewProposal(d);
    expect(text).toContain("Nothing beyond the platform's standard build.");
    expect(text).not.toContain("Estimated cost");
  });
});

describe("describeEdit", () => {
  it("names the real added resource and the real cost delta", () => {
    const before = draft({
      infra_proposal: {
        name: "p", topology: { nodes: [], edges: [] }, iac_terraform: "", estimated_monthly_cost_usd: 40,
        cost_breakdown: [], policy_checks: [],
      },
    });
    const after = draft({
      infra_proposal: {
        name: "p", topology: { nodes: [{ id: "ec2-1", type: "AWS::EC2::Instance", label: "EC2 instance" }], edges: [] },
        iac_terraform: "", estimated_monthly_cost_usd: 47,
        cost_breakdown: [], policy_checks: [],
      },
    });
    const text = describeEdit(before, after, "add an ec2 instance");
    expect(text).toContain("Added: EC2 instance.");
    expect(text).toContain("$40.00/mo -> $47.00/mo");
  });

  it("falls back to naming the instruction when nothing was structurally added (e.g. a config tweak)", () => {
    const before = draft();
    const after = draft();
    const text = describeEdit(before, after, "make the database Multi-AZ");
    expect(text).toContain('Updated the proposal for "make the database Multi-AZ".');
  });
});
