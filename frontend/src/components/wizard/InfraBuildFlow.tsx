/**
 * frontend/src/components/wizard/InfraBuildFlow.tsx
 *
 * AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §7 (Phase F) — REPLACES the old static
 * form-panel infra section in RequirementsForm.tsx with a linear, step-wise flow:
 *
 *   Setup (account + AI-created/existing) -> Chat (propose, iterate) -> Resources & confirm
 *   -> Building (live) -> Done
 *
 * Every step calls a real, already-existing endpoint (createInfraDraft, editInfraDraft,
 * approveInfraDraft, createInfraChangeSet, executeInfraChangeSet, getInfraProvisioningStatus,
 * discoverExistingInfra) - this is a new presentation layer over real state transitions the backend
 * already enforces (INFRA_PENDING_APPROVAL -> INFRA_APPROVED -> INFRA_CHANGE_SET_READY ->
 * INFRA_PROVISIONING -> INFRA_PROVISIONED), never a new one.
 */
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { CheckCircle2, Loader2, ShieldCheck, ShieldX, Sparkles } from "lucide-react";
import {
  approveInfraDraft, createInfraDraft, discoverExistingInfra, editInfraDraft, getInfraProvisioningStatus,
  type DiscoveredResources, type InfraDraft, type InfraEditMode, type InfraSource,
} from "@/api/infraDrafts";
import { assistantMessage, describeEdit, describeNewProposal, userMessage, type InfraChatMessage } from "@/lib/infraChat";
import { InfraChatPanel } from "@/components/wizard/InfraChatPanel";
import { InfraResourceConfirmation } from "@/components/wizard/InfraResourceConfirmation";
import { InfraBuildTimeline } from "@/components/wizard/InfraBuildTimeline";
import { InfraTopologyGraph } from "@/components/wizard/InfraTopologyGraph";
import { ExistingResourceBrowser } from "@/components/wizard/ExistingResourceBrowser";
import { AddComponentDialog } from "@/components/wizard/AddComponentDialog";
import { AwsAccountPicker } from "@/components/wizard/AwsAccountPicker";
import { InfraFailureAnalysisPanel } from "@/components/wizard/InfraFailureAnalysisPanel";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import type { IntentSpec } from "@/api/intentSpec";

const SLOT_LABELS: Record<string, string> = {
  database: "Database (RDS)", cache: "Cache (ElastiCache)", ecs_cluster: "ECS cluster", load_balancer: "Load balancer",
};

type Stage = "setup" | "chat" | "confirm" | "building" | "done";

function stageForDraft(draft: InfraDraft | null): Stage {
  if (!draft) return "setup";
  switch (draft.status) {
    case "INFRA_DRAFTING":
    case "INFRA_DRAFT_FAILED":
    case "INFRA_PENDING_APPROVAL":
      return "chat";
    case "INFRA_APPROVED":
    case "INFRA_CHANGE_SET_CREATING":
    case "INFRA_CHANGE_SET_READY":
    case "INFRA_CHANGE_SET_FAILED":
      // A standard-only draft never has a change set to build - approving it is the whole story.
      return draft.infra_proposal?.no_additional_infrastructure ? "done" : "confirm";
    case "INFRA_PROVISIONING":
      return "building";
    case "INFRA_PROVISIONED":
    case "INFRA_PROVISIONING_FAILED":
      return "done";
    default:
      return "chat";
  }
}

export function InfraBuildFlow({
  value, onDraftChange,
}: {
  value: IntentSpec;
  onDraftChange?: (draft: InfraDraft | null) => void;
}) {
  const [draft, setDraftState] = useState<InfraDraft | null>(null);
  const setDraft = (next: InfraDraft | null) => {
    setDraftState(next);
    onDraftChange?.(next);
  };
  const [messages, setMessages] = useState<InfraChatMessage[]>([]);
  const [drafting, setDrafting] = useState(false);
  const [draftError, setDraftError] = useState<string | null>(null);

  const [awsConnectionId, setAwsConnectionId] = useState<string | null>(null);
  const [infraSource, setInfraSource] = useState<InfraSource>("ai_created");
  const [discovered, setDiscovered] = useState<DiscoveredResources | null>(null);
  const [discovering, setDiscovering] = useState(false);
  const [discoverError, setDiscoverError] = useState<string | null>(null);
  const [selection, setSelection] = useState<Record<string, string>>({});
  const [showExistingBrowser, setShowExistingBrowser] = useState(false);
  const [chatEditMode, setChatEditMode] = useState<InfraEditMode>("add");
  const selectedCount = Object.keys(selection).length;
  // Only a real proposal that already has additions is something to "replace" - a fresh/standard-only
  // draft has nothing to choose between (bootstrapping is the same either way).
  const hasExistingAdditions = Boolean(
    draft && !draft.infra_proposal?.no_additional_infrastructure && (draft.infra_proposal?.additions?.length ?? 0) > 0
  );

  const nothingToAttach = !value.needs_database && !value.needs_cache;
  const visibleSlots = Object.entries(discovered ?? {}).filter(
    ([slot]) => (slot === "database" && value.needs_database) || (slot === "cache" && value.needs_cache),
  );

  const stage = stageForDraft(draft);

  async function handleDiscover() {
    if (!value.archetype) return;
    setDiscovering(true);
    setDiscoverError(null);
    try {
      setDiscovered(await discoverExistingInfra(value.archetype, value.aws_region, awsConnectionId));
      setSelection({});
    } catch (err) {
      setDiscoverError((err as Error).message);
    } finally {
      setDiscovering(false);
    }
  }

  function pickExisting(slot: string, id: string) {
    setSelection((prev) => {
      const next = { ...prev };
      if (id) next[slot] = id; else delete next[slot];
      return next;
    });
  }

  async function handlePreviewInfrastructure() {
    setDrafting(true);
    setDraftError(null);
    try {
      const result = await createInfraDraft(value, {
        source: infraSource,
        existingSelection: infraSource === "existing" ? selection : {},
        awsConnectionId,
      });
      setDraft(result);
      setMessages([assistantMessage(describeNewProposal(result))]);
    } catch (err) {
      setDraftError((err as Error).message);
    } finally {
      setDrafting(false);
    }
  }

  const [chatBusy, setChatBusy] = useState(false);
  async function handleSendChatMessage(instruction: string) {
    if (!draft) return;
    const modeUsed = chatEditMode;
    setMessages((prev) => [...prev, userMessage(modeUsed === "replace" ? `${instruction} (start fresh)` : instruction)]);
    setChatBusy(true);
    try {
      const result = await editInfraDraft(draft.draft_id, instruction, { mode: modeUsed });
      setMessages((prev) => [...prev, assistantMessage(describeEdit(draft, result, instruction))]);
      setDraft(result);
      setChatEditMode("add"); // a replace is a one-shot choice - back to the normal default for the next message
    } catch (err) {
      setMessages((prev) => [...prev, assistantMessage(`I couldn't apply that: ${(err as Error).message}`)]);
    } finally {
      setChatBusy(false);
    }
  }

  function handleComponentApplied(newDraft: InfraDraft) {
    if (draft) setMessages((prev) => [...prev, assistantMessage(describeEdit(draft, newDraft, "add that component"))]);
    setDraft(newDraft);
  }

  const [approving, setApproving] = useState(false);
  async function handleLooksGood() {
    if (!draft) return;
    if (draft.status === "INFRA_APPROVED") return; // already approved (auto_advance) - nothing to do
    setApproving(true);
    try {
      const result = await approveInfraDraft(draft.draft_id);
      setDraft(result);
      toast.success(
        result.infra_proposal?.no_additional_infrastructure
          ? "Selected — nothing extra to build."
          : "Approved — here's exactly what this will build."
      );
    } catch (err) {
      toast.error("Could not approve this proposal", { description: (err as Error).message });
    } finally {
      setApproving(false);
    }
  }

  // ── Stage C: live build polling ──
  useEffect(() => {
    if (!draft || draft.status !== "INFRA_PROVISIONING") return;
    let cancelled = false;
    const interval = setInterval(async () => {
      try {
        const result = await getInfraProvisioningStatus(draft.draft_id);
        if (cancelled) return;
        setDraft(result);
        if (result.status === "INFRA_PROVISIONED") toast.success("Real AWS resources provisioned successfully.");
        if (result.status === "INFRA_PROVISIONING_FAILED") toast.error("Provisioning failed", { description: result.provisioning_error ?? undefined });
      } catch {
        // A transient poll failure isn't fatal - the next tick tries again.
      }
    }, 4000);
    return () => { cancelled = true; clearInterval(interval); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft?.draft_id, draft?.status]);

  const policyBlockers = draft?.infra_proposal?.policy_evaluation?.allowed === false
    ? draft.infra_proposal.policy_evaluation.deny : [];

  return (
    <div className="space-y-4 border-t pt-4" data-testid="infra-build-flow">
      <Label className="text-xs font-semibold uppercase text-muted-foreground">Infrastructure</Label>

      {/* ── Setup: only shown before the first proposal exists ── */}
      {stage === "setup" && (
        <div className="space-y-3">
          <AwsAccountPicker
            value={awsConnectionId}
            onChange={(id) => { setAwsConnectionId(id); setDiscovered(null); setSelection({}); }}
            region={value.aws_region}
          />
          <div className="space-y-2 rounded-md border p-3">
            <Label className="text-xs font-semibold uppercase text-muted-foreground">Infrastructure source</Label>
            <div className="grid gap-2 sm:grid-cols-2">
              {([
                ["ai_created", "AI-created", "The agent designs new AWS resources for this project."],
                ["existing", "Existing", "Attach resources that already exist in your AWS account instead of creating duplicates."],
              ] as const).map(([key, title, blurb]) => (
                <button
                  type="button"
                  key={key}
                  onClick={() => setInfraSource(key)}
                  className={`rounded-md border p-2 text-left text-xs transition-colors ${infraSource === key ? "border-primary bg-primary/5" : "hover:bg-accent"}`}
                >
                  <div className="font-medium">{title}</div>
                  <div className="text-muted-foreground">{blurb}</div>
                </button>
              ))}
            </div>
            {infraSource === "existing" && (
              <div className="space-y-2 pt-1">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-[11px] text-muted-foreground">
                    Read-only lookup of your account in {value.aws_region}. Imported resources are kept (DeletionPolicy: Retain).
                  </p>
                  <Button size="sm" variant="outline" onClick={handleDiscover} disabled={discovering || !value.archetype || nothingToAttach}>
                    {discovering && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                    {discovered ? "Refresh" : "Find existing resources"}
                  </Button>
                </div>
                {nothingToAttach && (
                  <p className="rounded-md border border-dashed p-2 text-[11px] text-muted-foreground">
                    Nothing to attach: this app needs no database or cache. Turn on "Needs a database" or "Needs a cache" above first.
                  </p>
                )}
                {discoverError && <p className="text-xs text-destructive">{discoverError}</p>}
                {discovered && !nothingToAttach && (
                  <div className="grid gap-3 sm:grid-cols-2">
                    {visibleSlots.map(([slot, candidates]) => (
                      <ExistingResourceBrowser
                        key={slot} slot={slot} label={SLOT_LABELS[slot] ?? slot}
                        candidates={candidates} selectedId={selection[slot]} onSelect={(id) => pickExisting(slot, id)}
                      />
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
          <Button
            size="sm" onClick={handlePreviewInfrastructure}
            disabled={drafting || !value.archetype || (infraSource === "existing" && selectedCount === 0)}
          >
            {drafting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
            Ask the Infra Architect
          </Button>
          {!value.archetype && (
            <p className="text-[11px] text-muted-foreground">Waiting for a matched golden-path archetype from repo detection.</p>
          )}
          {draftError && <p className="text-xs text-destructive">{draftError}</p>}
        </div>
      )}

      {/* ── Stage A: chat ── */}
      {stage === "chat" && draft && (
        <InfraChatPanel
          messages={messages}
          onSend={handleSendChatMessage}
          busy={chatBusy || draft.status === "INFRA_DRAFTING"}
          actions={
            <>
              {/* Real bug found live: this used to be hidden entirely for a standard-only draft, but the
                  backend can now turn a genuine addition ("S3 bucket") into a real proposal even from
                  "nothing extra" (see infer_addition_kind_from_text) - the picker stays available so that
                  path is reachable, not just free text. */}
              <AddComponentDialog
                draftId={draft.draft_id}
                onApplied={handleComponentApplied}
                offerReplaceMode={hasExistingAdditions}
              />
              {!draft.infra_proposal?.no_additional_infrastructure && (
                <Button size="sm" variant="outline" onClick={() => setShowExistingBrowser((v) => !v)}>
                  {showExistingBrowser ? "Hide existing resources" : "Use existing resources"}
                </Button>
              )}
              {hasExistingAdditions && (
                <div className="flex items-center gap-1 rounded-md border p-0.5 text-xs">
                  <button
                    type="button"
                    onClick={() => setChatEditMode("add")}
                    className={`rounded-sm px-2 py-1 transition-colors ${chatEditMode === "add" ? "bg-primary/10 font-medium text-primary" : "text-muted-foreground hover:bg-accent"}`}
                    title="The next chat message extends the current proposal."
                  >
                    Add
                  </button>
                  <button
                    type="button"
                    onClick={() => setChatEditMode("replace")}
                    className={`rounded-sm px-2 py-1 transition-colors ${chatEditMode === "replace" ? "bg-destructive/10 font-medium text-destructive" : "text-muted-foreground hover:bg-accent"}`}
                    title="The next chat message deletes the current proposal and starts fresh with just that."
                  >
                    Replace
                  </button>
                </div>
              )}
              {chatEditMode === "replace" && (
                <span className="text-[11px] text-destructive">
                  Next message replaces the whole proposal — starts fresh with only what you describe.
                </span>
              )}
            </>
          }
          footer={
            <div className="space-y-2 border-t pt-3">
              {draft.infra_proposal && (
                <InfraTopologyGraph topology={draft.infra_proposal.topology} />
              )}
              {policyBlockers.length > 0 && (
                <p className="flex items-center gap-1.5 text-xs text-destructive">
                  <ShieldX className="h-3.5 w-3.5" /> Blocked by policy — describe a fix in the chat above before continuing.
                </p>
              )}
              <Button
                onClick={handleLooksGood}
                disabled={approving || policyBlockers.length > 0 || chatBusy}
                className="w-full"
              >
                {approving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
                {draft.infra_proposal?.no_additional_infrastructure ? "Use this infrastructure" : "Looks good — show me what this builds"}
              </Button>
            </div>
          }
        />
      )}
      {stage === "chat" && draft && showExistingBrowser && (
        <div className="rounded-md border p-3">
          <div className="mb-2 flex items-center justify-between">
            <Label className="text-xs">Existing resources in {value.aws_region}</Label>
            <Button size="sm" variant="outline" onClick={handleDiscover} disabled={discovering}>
              {discovering && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {discovered ? "Refresh" : "Find existing resources"}
            </Button>
          </div>
          {discoverError && <p className="text-xs text-destructive">{discoverError}</p>}
          {discovered && (
            <div className="grid gap-3 sm:grid-cols-2">
              {visibleSlots.map(([slot, candidates]) => (
                <ExistingResourceBrowser
                  key={slot} slot={slot} label={SLOT_LABELS[slot] ?? slot}
                  candidates={candidates} selectedId={selection[slot]}
                  onSelect={(id) => {
                    pickExisting(slot, id);
                    if (id) handleSendChatMessage(`Attach the existing ${SLOT_LABELS[slot] ?? slot} "${id}" instead of creating a new one.`);
                  }}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── Stage B: resource confirmation ── */}
      {stage === "confirm" && draft && (
        <InfraResourceConfirmation
          draft={draft}
          onDraftChange={setDraft}
          onExecuted={setDraft}
          onUseSuggestedEdit={(instruction) => { setDraft({ ...draft, status: "INFRA_PENDING_APPROVAL" }); handleSendChatMessage(instruction); }}
        />
      )}

      {/* ── Stage C: live build ── */}
      {stage === "building" && draft && (
        <div className="space-y-2">
          <p className="flex items-center gap-1.5 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Building your infrastructure…
          </p>
          <InfraBuildTimeline
            changes={draft.change_set_changes}
            resourceEvents={draft.resource_events ?? []}
            overallStatus={draft.status}
          />
        </div>
      )}

      {/* ── Done ── */}
      {stage === "done" && draft && (
        <div className="space-y-2">
          {draft.status === "INFRA_PROVISIONING_FAILED" ? (
            <>
              <p className="flex items-center gap-1.5 text-sm text-destructive">
                <ShieldX className="h-4 w-4" /> Provisioning failed: {draft.provisioning_error}
              </p>
              <InfraFailureAnalysisPanel draftId={draft.draft_id} onUseSuggestedEdit={(i) => handleSendChatMessage(i)} />
            </>
          ) : draft.infra_proposal?.no_additional_infrastructure ? (
            <p className="flex items-center gap-1.5 text-sm text-success">
              <CheckCircle2 className="h-4 w-4" /> Selected — nothing extra to build beyond the platform's standard setup.
            </p>
          ) : (
            <>
              <p className="flex items-center gap-1.5 text-sm text-success">
                <ShieldCheck className="h-4 w-4" /> Infrastructure provisioned successfully.
              </p>
              {Object.entries(draft.provisioning_outputs).map(([key, val]) => (
                <div key={key} className="flex justify-between text-xs text-muted-foreground">
                  <span>{key}</span><span className="text-code">{val}</span>
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}
