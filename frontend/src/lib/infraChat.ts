/**
 * frontend/src/lib/infraChat.ts
 *
 * AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §7 (Phase F) — turns a real InfraDraft (before/after a
 * proposal, edit, or component-add) into plain-language chat messages. Every number quoted here is read
 * straight off the draft the backend already computed (apply_independent_cost's real AWS-Price-List
 * figure, OPA's real policy_evaluation) — this module narrates real state, it never estimates or
 * fabricates anything of its own. Kept pure/no I/O so it's testable without mounting the chat UI.
 */
import type { InfraDraft } from "@/api/infraDrafts";

export interface InfraChatMessage {
  id: string;
  role: "assistant" | "user";
  content: string;
}

let seq = 0;
function nextId(): string {
  seq += 1;
  return `msg-${seq}`;
}

function costLine(draft: InfraDraft): string {
  const proposal = draft.infra_proposal;
  if (!proposal || proposal.no_additional_infrastructure) return "";
  const source =
    proposal.cost_estimate?.source === "aws-price-list"
      ? " (from real AWS prices)"
      : proposal.cost_estimate?.source === "ai_estimate_unverified"
        ? " (unverified AI estimate — pricing data was unavailable)"
        : "";
  return `Estimated cost: $${proposal.estimated_monthly_cost_usd.toFixed(2)}/mo${source}.`;
}

function policyLine(draft: InfraDraft): string {
  const evalResult = draft.infra_proposal?.policy_evaluation;
  if (!evalResult) return "";
  if (!evalResult.allowed) {
    return `⚠ Blocked by policy: ${evalResult.deny.map((d) => d.message).join("; ")}`;
  }
  if (evalResult.warn.length > 0) {
    return `Policy: passed with ${evalResult.warn.length} warning(s) — ${evalResult.warn.map((w) => w.message).join("; ")}`;
  }
  return "Policy: passed all checks.";
}

/** The AI's first chat turn, right after createInfraDraft() succeeds. */
export function describeNewProposal(draft: InfraDraft): string {
  const proposal = draft.infra_proposal;
  if (!proposal) return "I couldn't generate a proposal for this yet.";
  if (proposal.no_additional_infrastructure) {
    return (
      `${proposal.needs_summary ?? "This app needs nothing beyond what the platform already builds for every project."} ` +
      `No additional infrastructure, no extra cost.`
    );
  }
  const resourceCount = proposal.topology.nodes.length;
  const lines = [
    proposal.needs_summary ?? `I've put together a proposal with ${resourceCount} resource(s).`,
    costLine(draft),
    policyLine(draft),
  ].filter(Boolean);
  return lines.join(" ");
}

/** A follow-up turn after editInfraDraft()/checkComponentCompatibility()+edit — states what changed, not just the new total. */
export function describeEdit(previous: InfraDraft, next: InfraDraft, instruction: string): string {
  const prevCost = previous.infra_proposal?.estimated_monthly_cost_usd ?? 0;
  const nextCost = next.infra_proposal?.estimated_monthly_cost_usd ?? 0;
  const prevNodes = new Set((previous.infra_proposal?.topology.nodes ?? []).map((n) => n.id));
  const nextNodes = next.infra_proposal?.topology.nodes ?? [];
  const added = nextNodes.filter((n) => !prevNodes.has(n.id));

  const parts: string[] = [];
  if (added.length > 0) {
    parts.push(`Added: ${added.map((n) => n.label).join(", ")}.`);
  } else {
    parts.push(`Updated the proposal for "${instruction}".`);
  }
  if (Math.abs(nextCost - prevCost) > 0.001) {
    parts.push(`Cost: $${prevCost.toFixed(2)}/mo -> $${nextCost.toFixed(2)}/mo.`);
  }
  const policy = policyLine(next);
  if (policy) parts.push(policy);
  return parts.join(" ");
}

export function userMessage(content: string): InfraChatMessage {
  return { id: nextId(), role: "user", content };
}

export function assistantMessage(content: string): InfraChatMessage {
  return { id: nextId(), role: "assistant", content };
}
