/**
 * frontend/src/components/wizard/InfraResourceConfirmation.tsx
 *
 * AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §7 (Phase F, Stage B) — "before anything gets built, show
 * exactly what will be used." Replaces the old bare `<ul>` + plain HTML checkbox with a dedicated screen:
 * the REAL CloudFormation change-set diff (createInfraChangeSet — a genuine, zero-risk AWS dry run, not a
 * guess), grouped by action, plus the real cost breakdown and policy checks already computed for this
 * draft, then an explicit confirmation dialog before the one real, billable action
 * (executeInfraChangeSet) fires.
 */
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { AlertTriangle, CheckCircle2, Loader2, ShieldCheck, ShieldX } from "lucide-react";
import {
  createInfraChangeSet, executeInfraChangeSet,
  type InfraDraft, type ResourceChange,
} from "@/api/infraDrafts";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import { InfraFailureAnalysisPanel } from "@/components/wizard/InfraFailureAnalysisPanel";

const ACTION_LABEL: Record<ResourceChange["action"], string> = {
  Add: "Create", Modify: "Modify", Remove: "Remove", Import: "Import (already exists)",
};
const ACTION_COLOR: Record<ResourceChange["action"], string> = {
  Add: "text-success", Modify: "text-warning", Remove: "text-destructive", Import: "text-muted-foreground",
};

export function InfraResourceConfirmation({
  draft, onDraftChange, onExecuted, onUseSuggestedEdit,
}: {
  draft: InfraDraft;
  onDraftChange: (next: InfraDraft) => void;
  onExecuted: (next: InfraDraft) => void;
  onUseSuggestedEdit: (instruction: string) => void;
}) {
  const [creatingChangeSet, setCreatingChangeSet] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmChecked, setConfirmChecked] = useState(false);
  const [executing, setExecuting] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);

  // Real, zero-risk AWS dry run — fires automatically the moment this screen is reached (once, from
  // INFRA_APPROVED), so the human never has to remember a separate "preview" click before seeing the
  // real resource list.
  useEffect(() => {
    if (draft.status !== "INFRA_APPROVED") return;
    let cancelled = false;
    setCreatingChangeSet(true);
    setError(null);
    createInfraChangeSet(draft.draft_id)
      .then((result) => { if (!cancelled) onDraftChange(result); })
      .catch((err) => { if (!cancelled) setError((err as Error).message); })
      .finally(() => { if (!cancelled) setCreatingChangeSet(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft.draft_id, draft.status]);

  async function handleConfirmAndBuild() {
    setExecuting(true);
    try {
      const result = await executeInfraChangeSet(draft.draft_id);
      setDialogOpen(false);
      onExecuted(result);
    } catch (err) {
      toast.error("Could not start provisioning", { description: (err as Error).message });
    } finally {
      setExecuting(false);
    }
  }

  if (creatingChangeSet || draft.status === "INFRA_CHANGE_SET_CREATING") {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> Computing the real AWS change set — a zero-risk dry run, nothing is created yet…
      </div>
    );
  }

  if (error) {
    return <p className="text-sm text-destructive">{error}</p>;
  }

  if (draft.status === "INFRA_CHANGE_SET_FAILED") {
    return (
      <div className="space-y-2">
        <p className="text-sm text-destructive">{draft.provisioning_error}</p>
        <InfraFailureAnalysisPanel draftId={draft.draft_id} onUseSuggestedEdit={onUseSuggestedEdit} />
      </div>
    );
  }

  if (draft.status !== "INFRA_CHANGE_SET_READY") {
    return <p className="text-sm text-muted-foreground">Waiting for the change set…</p>;
  }

  const changes = draft.change_set_changes;
  const grouped = {
    Add: changes.filter((c) => c.action === "Add"),
    Modify: changes.filter((c) => c.action === "Modify"),
    Import: changes.filter((c) => c.action === "Import"),
    Remove: changes.filter((c) => c.action === "Remove"),
  };
  const proposal = draft.infra_proposal;

  return (
    <div className="space-y-4" data-testid="infra-resource-confirmation">
      <div>
        <h3 className="text-sm font-semibold">Resources this will use</h3>
        <p className="text-xs text-muted-foreground">
          This is the real AWS CloudFormation change set for this proposal — exactly what will happen, not a preview guess.
        </p>
      </div>

      {changes.length === 0 ? (
        <p className="text-sm text-muted-foreground">No changes — this stack already matches the proposal.</p>
      ) : (
        <div className="space-y-3">
          {(["Add", "Modify", "Import", "Remove"] as const)
            .filter((action) => grouped[action].length > 0)
            .map((action) => (
              <div key={action} className="rounded-md border">
                <div className={`flex items-center gap-1.5 border-b bg-muted/30 px-3 py-1.5 text-xs font-medium ${ACTION_COLOR[action]}`}>
                  {ACTION_LABEL[action]}
                  <Badge variant="secondary" className="text-[10px]">{grouped[action].length}</Badge>
                </div>
                <ul className="divide-y">
                  {grouped[action].map((c) => (
                    <li key={c.logical_id} className="flex items-center justify-between px-3 py-1.5 text-xs">
                      <span className="font-medium">{c.logical_id}</span>
                      <span className="text-code text-muted-foreground">{c.resource_type}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
        </div>
      )}

      {proposal && !proposal.no_additional_infrastructure && (
        <div className="grid grid-cols-2 gap-3 text-xs">
          <div>
            <div className="mb-1 font-medium">Cost</div>
            <p className="text-stat text-lg">${proposal.estimated_monthly_cost_usd.toFixed(2)}/mo</p>
          </div>
          <div>
            <div className="mb-1 font-medium">Policy</div>
            {proposal.policy_evaluation?.allowed === false ? (
              <span className="flex items-center gap-1 text-destructive"><ShieldX className="h-3.5 w-3.5" /> Blocked</span>
            ) : (
              <span className="flex items-center gap-1 text-success"><ShieldCheck className="h-3.5 w-3.5" /> Passed</span>
            )}
          </div>
        </div>
      )}

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogTrigger asChild>
          <Button variant="destructive" disabled={changes.length === 0}>Confirm &amp; Build</Button>
        </DialogTrigger>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create {changes.length} real, billable AWS resource(s)?</DialogTitle>
          </DialogHeader>
          <div className="space-y-2 text-sm">
            <p className="text-muted-foreground">This will actually create/modify these resources in your AWS account:</p>
            <ul className="max-h-40 space-y-0.5 overflow-y-auto rounded-md border p-2 text-xs">
              {changes.map((c) => (
                <li key={c.logical_id} className="flex justify-between">
                  <span>{c.logical_id}</span>
                  <span className={ACTION_COLOR[c.action]}>{ACTION_LABEL[c.action]}</span>
                </li>
              ))}
            </ul>
            <label className="flex items-start gap-2 text-xs">
              <input
                type="checkbox"
                checked={confirmChecked}
                onChange={(e) => setConfirmChecked(e.target.checked)}
                className="mt-0.5 h-3.5 w-3.5 accent-destructive"
              />
              I understand this will create real, billable AWS resources.
            </label>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>Cancel</Button>
            <Button variant="destructive" onClick={handleConfirmAndBuild} disabled={!confirmChecked || executing}>
              {executing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
              Confirm &amp; Build
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {changes.length === 0 && (
        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <AlertTriangle className="h-3.5 w-3.5" /> Nothing to build — this stack already matches the proposal.
        </p>
      )}
    </div>
  );
}
