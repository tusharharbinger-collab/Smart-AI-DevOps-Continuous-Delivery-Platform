/**
 * frontend/src/components/wizard/ExistingResourceBrowser.tsx
 *
 * AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.3 (Phase A) — replaces the plain `<select>` dropdown
 * RequirementsForm.tsx used to render for "attach an existing resource" with a real, status-visible
 * browser: one card per candidate, showing its real engine/version/size and a live status badge sourced
 * directly from `details.status` (shared/provisioning/aws_discovery.py's real `describe_db_instances` /
 * `describe_cache_clusters` call — never a guess). A stopped/deleting resource is visibly flagged as
 * unsafe to attach rather than silently offered next to a healthy one.
 */
import { CheckCircle2, Circle, Database, HardDrive } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ExistingResourceCandidate } from "@/api/infraDrafts";

type StatusTone = "good" | "warn" | "bad" | "neutral";

const GOOD_STATUSES = new Set(["available", "active"]);
const BAD_STATUSES = new Set(["deleting", "deleted", "failed", "stopped", "incompatible-parameters"]);

function statusTone(status: unknown): StatusTone {
  const s = String(status ?? "").toLowerCase();
  if (!s) return "neutral";
  if (GOOD_STATUSES.has(s)) return "good";
  if (BAD_STATUSES.has(s)) return "bad";
  return "warn"; // creating, modifying, backing-up, snapshotting, rebooting, etc. — real, just not settled yet
}

const TONE_CLASSES: Record<StatusTone, string> = {
  good: "border-success/40 bg-success/10 text-success",
  warn: "border-warning/40 bg-warning/10 text-warning",
  bad: "border-destructive/40 bg-destructive/10 text-destructive",
  neutral: "border-border bg-muted text-muted-foreground",
};

function detailLine(candidate: ExistingResourceCandidate): string {
  const d = candidate.details;
  const parts: string[] = [];
  if (d.engine) parts.push(`${d.engine}${d.engine_version ? ` ${d.engine_version}` : ""}`);
  if (d.instance_class) parts.push(String(d.instance_class));
  if (d.node_type) parts.push(String(d.node_type));
  if (d.num_nodes != null) parts.push(`${d.num_nodes} node(s)`);
  if (d.multi_az) parts.push("multi-AZ");
  return parts.join(" · ");
}

export function ExistingResourceBrowser({
  slot, label, candidates, selectedId, onSelect,
}: {
  slot: string;
  label: string;
  candidates: ExistingResourceCandidate[];
  selectedId: string | undefined;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="space-y-1.5" data-testid={`resource-browser-${slot}`}>
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <div className="space-y-1.5">
        <button
          type="button"
          onClick={() => onSelect("")}
          className={cn(
            "flex w-full items-center gap-2 rounded-md border p-2 text-left text-xs transition-colors",
            !selectedId ? "border-primary bg-primary/5" : "hover:bg-accent"
          )}
        >
          {!selectedId ? <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-primary" /> : <Circle className="h-3.5 w-3.5 shrink-0 text-muted-foreground/40" />}
          <span>
            {candidates.length === 0
              ? "None found — the agent will create one"
              : "Create new (don't attach an existing one)"}
          </span>
        </button>

        {candidates.map((candidate) => {
          const tone = statusTone(candidate.details.status);
          const isSelected = selectedId === candidate.id;
          const isUnsafe = tone === "bad";
          return (
            <button
              key={candidate.id}
              type="button"
              onClick={() => onSelect(candidate.id)}
              className={cn(
                "flex w-full items-start gap-2 rounded-md border p-2 text-left text-xs transition-colors",
                isSelected ? "border-primary bg-primary/5" : "hover:bg-accent"
              )}
              title={isUnsafe ? "This resource is not currently running — attaching it is not recommended" : undefined}
            >
              {isSelected ? <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" /> : <Circle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground/40" />}
              {slot === "cache" ? <HardDrive className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" /> : <Database className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
              <span className="flex-1 space-y-0.5">
                <span className="flex items-center gap-1.5">
                  <span className="font-medium text-code">{candidate.label}</span>
                  {candidate.details.status != null && (
                    <span className={cn("rounded border px-1.5 py-0 text-[10px] font-medium uppercase", TONE_CLASSES[tone])}>
                      {String(candidate.details.status)}
                    </span>
                  )}
                </span>
                {detailLine(candidate) && <span className="block text-muted-foreground">{detailLine(candidate)}</span>}
                {isUnsafe && (
                  <span className="block text-destructive">Not running — attaching this is not recommended.</span>
                )}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
