/**
 * frontend/src/components/wizard/InfraBuildTimeline.tsx
 *
 * AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §7 (Phase F, Stage C) — the live "infrastructure is being
 * built" view. Visually modeled on StageTimeline.tsx's pattern (status icon per item, connecting line) but
 * driven by REAL per-resource CloudFormation events (`resource_events`, §7.4.1) instead of log regex —
 * there's no log stream for infra provisioning, only periodic real `describe_stack_events` snapshots.
 *
 * One row per resource in the real change-set (`changes`) — never a fabricated list. A resource with no
 * event yet is "pending"; one whose latest real event ends in `_IN_PROGRESS` is "active"; `_COMPLETE` is
 * "done"; `_FAILED` or `ROLLBACK_*` is "failed". The latest matching event's own reason (if any) is shown
 * as the sub-detail line, exactly as AWS reported it — never paraphrased.
 */
import { CheckCircle2, Circle, CircleDot, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ResourceChange, ResourceEvent } from "@/api/infraDrafts";

type ResourceRowStatus = "pending" | "active" | "done" | "failed";

function statusFor(events: ResourceEvent[], logicalId: string): { status: ResourceRowStatus; detail: string | null } {
  const matches = events.filter((e) => e.resource === logicalId);
  if (matches.length === 0) return { status: "pending", detail: null };
  const latest = matches[matches.length - 1];
  const s = (latest.status ?? "").toUpperCase();
  if (s.endsWith("_FAILED") || s.includes("ROLLBACK")) return { status: "failed", detail: latest.reason ?? latest.status };
  if (s.endsWith("_COMPLETE")) return { status: "done", detail: latest.status };
  if (s.endsWith("_IN_PROGRESS")) return { status: "active", detail: latest.status };
  return { status: "pending", detail: latest.status };
}

const ROW_ICON: Record<ResourceRowStatus, React.ReactNode> = {
  pending: <Circle className="h-4 w-4 text-muted-foreground/50" />,
  active: <CircleDot className="h-4 w-4 animate-pulse text-primary" />,
  done: <CheckCircle2 className="h-4 w-4 text-success" />,
  failed: <XCircle className="h-4 w-4 text-destructive" />,
};

const ROW_TEXT: Record<ResourceRowStatus, string> = {
  pending: "text-muted-foreground",
  active: "text-primary",
  done: "text-foreground",
  failed: "text-destructive",
};

export function InfraBuildTimeline({
  changes, resourceEvents, overallStatus,
}: {
  changes: ResourceChange[];
  resourceEvents: ResourceEvent[];
  overallStatus: string;
}) {
  if (changes.length === 0) {
    return <p className="text-sm text-muted-foreground">No resources to provision — this stack already matches the proposal.</p>;
  }

  return (
    <div className="space-y-0" data-testid="infra-build-timeline">
      {changes.map((c, i) => {
        const { status, detail } = statusFor(resourceEvents, c.logical_id);
        const isLast = i === changes.length - 1;
        const failed = status === "pending" && overallStatus === "INFRA_PROVISIONING_FAILED" ? "failed" : status;
        return (
          <div key={c.logical_id} className="flex gap-3">
            <div className="flex flex-col items-center">
              {ROW_ICON[failed]}
              {!isLast && <div className={cn("my-0.5 w-px flex-1 min-h-[1.25rem]", failed === "done" ? "bg-success/40" : "bg-border")} />}
            </div>
            <div className={cn("pb-3", isLast && "pb-0")}>
              <div className="flex items-center gap-2">
                <span className={cn("text-sm font-medium", ROW_TEXT[failed])}>{c.logical_id}</span>
                <span className="text-code text-[11px] text-muted-foreground">{c.resource_type}</span>
                <span className="rounded bg-muted px-1 text-[10px] uppercase text-muted-foreground">{c.action}</span>
              </div>
              {detail && <p className="mt-0.5 text-[11px] text-muted-foreground">{detail}</p>}
            </div>
          </div>
        );
      })}
    </div>
  );
}
