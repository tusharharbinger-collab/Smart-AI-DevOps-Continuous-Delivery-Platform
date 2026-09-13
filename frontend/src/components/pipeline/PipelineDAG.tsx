import { CheckCircle2, Circle, CircleDot, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";

export interface PipelineDAGProps {
  stages?: string[];
  currentStage?: string;
  /** Overall pipeline run status — only used to render the current stage as Failed (red) instead of Running. */
  status?: string;
  /** Currently log-filtered stage, if any — highlights the selected stage distinctly from current/done/pending. */
  selectedStage?: string | null;
  /** Clicking a stage filters the Live execution log panel to just that stage's output; clicking it again clears the filter. */
  onSelectStage?: (stage: string | null) => void;
}

export function PipelineDAG({ stages, currentStage, status, selectedStage, onSelectStage }: PipelineDAGProps) {
  if (!stages || stages.length === 0) {
    return <p className="text-sm text-muted-foreground">Waiting for pipeline DAG…</p>;
  }

  const currentIndex = stages.indexOf(currentStage ?? "");

  return (
    <div className="flex flex-wrap items-center gap-1">
      {stages.map((stage, i) => {
        const isCurrent = stage === currentStage;
        const isDone = currentIndex >= 0 && i < currentIndex;
        const isFailed = isCurrent && status === "FAILED";
        const isSelected = selectedStage === stage;
        const clickable = Boolean(onSelectStage);

        return (
          <div key={stage} className="flex items-center gap-1">
            <button
              type="button"
              disabled={!clickable}
              onClick={() => onSelectStage?.(isSelected ? null : stage)}
              title={clickable ? "Filter the live log to this stage" : undefined}
              className={cn(
                "flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                clickable && "cursor-pointer hover:opacity-80",
                !clickable && "cursor-default",
                isFailed && "bg-destructive/15 text-destructive",
                !isFailed && isCurrent && "bg-primary text-primary-foreground",
                !isFailed && isDone && "bg-success/15 text-success",
                !isFailed && !isCurrent && !isDone && "bg-muted text-muted-foreground",
                isSelected && "ring-2 ring-offset-1 ring-ring"
              )}
            >
              {isFailed ? (
                <XCircle className="h-3.5 w-3.5" />
              ) : isDone ? (
                <CheckCircle2 className="h-3.5 w-3.5" />
              ) : isCurrent ? (
                <CircleDot className="h-3.5 w-3.5 animate-pulse" />
              ) : (
                <Circle className="h-3.5 w-3.5" />
              )}
              {stage}
            </button>
            {i < stages.length - 1 && <span className="text-muted-foreground">→</span>}
          </div>
        );
      })}
    </div>
  );
}
