/**
 * frontend/src/components/pipeline/StageTimeline.tsx
 *
 * Replaces PipelineDashboard.tsx's PipelineDAG + TrafficGauge +
 * TrafficWeightChart block with one connected, graphical timeline: every
 * stage (build/test/canary_deploy/canary_verify), each with its real
 * sub-steps shown live as they happen — so a user watching a rollout can
 * see exactly what's happening, not just a bare "canary_deploy success"
 * chip. Every sub-step comes from real log lines (see
 * lib/pipelineStageSteps.ts's module docstring) — nothing here is
 * fabricated progress.
 */
import type { ReactNode } from "react";
import { CheckCircle2, Circle, CircleDot, Loader2, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";
import { deriveSubSteps, splitLogsByStage, type StageRowStatus, type SubStep } from "@/lib/pipelineStageSteps";
import { TrafficWeightChart, type WeightPoint } from "@/components/pipeline/TrafficWeightChart";
import { Badge } from "@/components/ui/badge";

export interface StageTimelineProps {
  stages?: string[];
  currentStage?: string;
  status?: string;
  logLines: string[];
  trafficWeight: number;
  weightHistory: WeightPoint[];
  /** Currently log-filtered stage, if any — mirrors PipelineDAG's original
   * click-to-filter-the-log-panel interaction (preserved here rather than
   * dropped, since StageTimeline replaces PipelineDAG in PipelineDashboard.tsx). */
  selectedStage?: string | null;
  onSelectStage?: (stage: string | null) => void;
}

const STAGE_ROW_ICON: Record<StageRowStatus, ReactNode> = {
  pending: <Circle className="h-4 w-4 text-muted-foreground" />,
  active: <CircleDot className="h-4 w-4 animate-pulse text-primary" />,
  done: <CheckCircle2 className="h-4 w-4 text-success" />,
  failed: <XCircle className="h-4 w-4 text-destructive" />,
};

const SUB_STEP_ICON: Record<SubStep["status"], ReactNode> = {
  pending: <Circle className="h-3 w-3 text-muted-foreground/50" />,
  active: <Loader2 className="h-3 w-3 animate-spin text-primary" />,
  done: <CheckCircle2 className="h-3 w-3 text-success" />,
  failed: <XCircle className="h-3 w-3 text-destructive" />,
};

const ROW_TEXT_CLASS: Record<StageRowStatus, string> = {
  pending: "text-muted-foreground",
  active: "text-primary",
  done: "text-foreground",
  failed: "text-destructive",
};

export function StageTimeline({
  stages, currentStage, status, logLines, trafficWeight, weightHistory, selectedStage, onSelectStage,
}: StageTimelineProps) {
  if (!stages || stages.length === 0) {
    return <p className="text-sm text-muted-foreground">Waiting for pipeline DAG…</p>;
  }

  const logsByStage = splitLogsByStage(logLines);
  const currentIndex = stages.indexOf(currentStage ?? "");

  return (
    <div className="space-y-0">
      {stages.map((stageName, i) => {
        const isDone = currentIndex >= 0 && i < currentIndex;
        const isCurrent = stageName === currentStage;
        const isFailed = isCurrent && status === "FAILED";
        const rowStatus: StageRowStatus = isFailed ? "failed" : isCurrent ? "active" : isDone ? "done" : "pending";
        const group = logsByStage[stageName];
        const subSteps = deriveSubSteps(group?.stageType ?? "", group?.lines ?? [], rowStatus);
        const isLast = i === stages.length - 1;
        const isCanaryVerify = group?.stageType === "canary_loop";
        const showWeightChart = isCanaryVerify && (isCurrent || isDone) && weightHistory.length >= 2;

        return (
          <div key={stageName} className="flex gap-3">
            <div className="flex flex-col items-center">
              {STAGE_ROW_ICON[rowStatus]}
              {!isLast && (
                <div
                  className={cn(
                    "my-0.5 w-px flex-1 min-h-[1.5rem]",
                    isDone ? "bg-success/40" : "bg-border"
                  )}
                />
              )}
            </div>
            <div className={cn("pb-4", isLast && "pb-0")}>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  disabled={!onSelectStage}
                  onClick={() => onSelectStage?.(selectedStage === stageName ? null : stageName)}
                  title={onSelectStage ? "Filter the live log to this stage" : undefined}
                  className={cn(
                    "text-sm font-medium",
                    ROW_TEXT_CLASS[rowStatus],
                    onSelectStage && "cursor-pointer hover:underline",
                    selectedStage === stageName && "underline"
                  )}
                >
                  {stageName}
                </button>
                {isCanaryVerify && (isCurrent || isDone) && (
                  <Badge variant={isFailed ? "destructive" : "secondary"} className="text-[10px]">
                    {trafficWeight}% traffic
                  </Badge>
                )}
              </div>
              {subSteps.length > 0 && (
                <ul className="mt-1.5 space-y-1">
                  {subSteps.map((step) => (
                    <li key={step.id} className="flex items-start gap-1.5 text-xs">
                      <span className="mt-0.5 shrink-0">{SUB_STEP_ICON[step.status]}</span>
                      <span
                        className={cn(
                          step.status === "pending" && "text-muted-foreground/60",
                          step.status === "failed" && "text-destructive",
                          step.status === "active" && "text-foreground",
                          step.status === "done" && "text-muted-foreground"
                        )}
                      >
                        {step.label}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              {showWeightChart && (
                <div className="mt-2 max-w-md">
                  <TrafficWeightChart history={weightHistory} />
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
