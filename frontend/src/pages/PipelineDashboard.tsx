/**
 * frontend/src/pages/PipelineDashboard.tsx — Screen 1.
 *
 * WebSocket (usePipelineEvents) drives structured stage/traffic-weight state;
 * SSE (useLiveLogs) drives the raw build/test/deploy log stream.
 */
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { AlertTriangle, Pause, Play, X, Terminal, Radio, Lightbulb } from "lucide-react";
import { usePipelineEvents } from "@/hooks/usePipelineEvents";
import { useLiveLogs } from "@/hooks/useLiveLogs";
import { useAppContext } from "@/hooks/useAppContext";
import { PipelineDAG } from "@/components/pipeline/PipelineDAG";
import { TrafficGauge } from "@/components/pipeline/TrafficGauge";
import { TrafficWeightChart } from "@/components/pipeline/TrafficWeightChart";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { pausePipeline, resumePipeline, triggerRollback } from "@/api/pipeline";
import { getRunFailureAnalysis, type StageFailureAnalysis } from "@/api/projects";

// Matches worker.py's `_log(run_id, f"--- Stage: {stage_name} ({stage_type}) ---")`
// marker convention — the only structure the raw log stream carries today,
// used here purely client-side so filtering a stage's output needs no
// backend change (the log lines themselves stay plain strings).
const STAGE_MARKER_RE = /^--- Stage: (.+?) \(.+?\) ---$/;

function filterLogsForStage(logLines: string[], selectedStage: string | null): string[] {
  if (!selectedStage) return logLines;
  const startIndex = logLines.findIndex((line) => STAGE_MARKER_RE.exec(line)?.[1] === selectedStage);
  if (startIndex === -1) return [];
  const endIndex = logLines.findIndex((line, i) => i > startIndex && STAGE_MARKER_RE.test(line));
  return logLines.slice(startIndex, endIndex === -1 ? undefined : endIndex);
}

export function PipelineDashboard() {
  const { pipelineRunId, hideRunControls, projectId } = useAppContext();
  const { stages, currentStage, trafficWeight, status, weightHistory } = usePipelineEvents(pipelineRunId);
  const { logLines } = useLiveLogs(pipelineRunId);
  const [selectedStage, setSelectedStage] = useState<string | null>(null);
  const visibleLogLines = useMemo(() => filterLogsForStage(logLines, selectedStage), [logLines, selectedStage]);
  const [failureAnalysis, setFailureAnalysis] = useState<StageFailureAnalysis | null>(null);

  // Grounded stage-failure RCA (see worker.py::_request_stage_failure_rca) —
  // fetched only once a run has actually failed, matching how the rest of
  // this screen treats FAILED as a terminal state to react to.
  useEffect(() => {
    if (status !== "FAILED" || !projectId || !pipelineRunId) {
      setFailureAnalysis(null);
      return;
    }
    getRunFailureAnalysis(projectId, pipelineRunId)
      .then((res) => setFailureAnalysis(res.failure_analysis))
      .catch(() => setFailureAnalysis(null));
  }, [status, projectId, pipelineRunId]);

  if (!pipelineRunId) {
    return (
      <Card className="mx-auto mt-12 max-w-xl">
        <CardHeader>
          <CardTitle>No pipeline run selected</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Pick a pipeline in the header, or click <strong>Trigger New Rollout</strong> to start a live rollout.
        </CardContent>
      </Card>
    );
  }

  if (status === "UNKNOWN" && !stages) {
    return (
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Skeleton className="h-64 md:col-span-2" />
        <Skeleton className="h-64" />
      </div>
    );
  }

  async function handlePause() {
    try {
      await pausePipeline(pipelineRunId);
      toast.success("Pipeline paused");
    } catch (err) {
      toast.error("Pause failed", { description: (err as Error).message });
    }
  }

  async function handleResume() {
    try {
      await resumePipeline(pipelineRunId);
      toast.success("Pipeline resumed");
    } catch (err) {
      toast.error("Resume failed", { description: (err as Error).message });
    }
  }

  async function handleRollback() {
    try {
      await triggerRollback(pipelineRunId);
      toast.success("Emergency rollback requested");
    } catch (err) {
      toast.error("Rollback failed", { description: (err as Error).message });
    }
  }

  return (
    <div className="space-y-4">
      <div
        className={`flex flex-wrap items-center justify-between gap-3 ${
          hideRunControls ? "" : "border-b pb-3"
        }`}
      >
        <div className="flex items-center gap-2">
          <span className="text-code text-[11px] text-muted-foreground">Run</span>
          <Badge variant={status === "FAILED" || status === "ROLLED_BACK" ? "destructive" : status === "COMPLETED" ? "success" : "default"}>
            {status}
          </Badge>
          <span className="text-code text-[11px] text-muted-foreground">{pipelineRunId.slice(0, 8)}…</span>
        </div>
        <div className={`flex items-center gap-2 ${hideRunControls ? "hidden" : ""}`}>
          {/* Real gap found live (2026-09-15): these buttons used to render
              enabled regardless of the run's actual status — clicking
              Resume on an already-FAILED run used to silently fake a
              "RUNNING" display with zero real work behind it (see
              actuation_router.py's `_set_status` fix). The backend now
              correctly rejects an invalid transition with a 409, but the
              button itself still invited the click. Disabled state here
              mirrors the backend's own required-current-status rule
              exactly — pause only from RUNNING, resume only from PAUSED,
              rollback never on an already-terminal run. */}
          <Button variant="outline" size="sm" onClick={handlePause} disabled={status !== "RUNNING"}>
            <Pause className="h-3.5 w-3.5" /> Pause
          </Button>
          <Button variant="outline" size="sm" onClick={handleResume} disabled={status !== "PAUSED"}>
            <Play className="h-3.5 w-3.5" /> Resume
          </Button>
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button
                variant="destructive"
                size="sm"
                disabled={status === "FAILED" || status === "COMPLETED" || status === "ROLLED_BACK"}
              >
                <AlertTriangle className="h-3.5 w-3.5" /> Emergency Rollback
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Roll back this rollout?</AlertDialogTitle>
                <AlertDialogDescription>
                  This immediately requests a rollback for run <code>{pipelineRunId.slice(0, 8)}…</code>.
                  It still goes through HMAC verification and OPA before anything is actuated, same as an
                  autonomous rollback — but it's requested manually and can't be undone once policy authorizes it.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction onClick={handleRollback}>Roll back</AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Card className="md:col-span-2">
          <CardHeader>
            <CardTitle>Rollout progress</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <PipelineDAG
              stages={stages}
              currentStage={currentStage}
              status={status}
              selectedStage={selectedStage}
              onSelectStage={setSelectedStage}
            />
            <TrafficGauge weight={trafficWeight} status={status} />
            <TrafficWeightChart history={weightHistory} />
          </CardContent>
        </Card>

        <Card className="overflow-hidden">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 border-b bg-muted/30">
            <CardTitle className="flex items-center gap-1.5">
              <Terminal className="h-3.5 w-3.5 text-muted-foreground" />
              Live execution log
            </CardTitle>
            <div className="flex items-center gap-2">
              {logLines.length > 0 && (
                <span className="flex items-center gap-1 text-code text-[10px] text-success">
                  <Radio className="h-2.5 w-2.5 animate-pulse" /> streaming
                </span>
              )}
              {selectedStage && (
                <Button variant="ghost" size="sm" className="h-6 gap-1 text-xs" onClick={() => setSelectedStage(null)}>
                  <X className="h-3 w-3" /> {selectedStage}
                </Button>
              )}
            </div>
          </CardHeader>
          <CardContent className="p-0">
            <div className="h-72 overflow-y-auto bg-slate-950 p-3 text-code text-xs leading-relaxed text-green-400">
              {visibleLogLines.length === 0 ? (
                <span className="text-slate-500">
                  {selectedStage ? `No log lines yet for stage "${selectedStage}".` : "Waiting for log output…"}
                </span>
              ) : (
                visibleLogLines.map((line, i) => <div key={i}>{line}</div>)
              )}
            </div>
          </CardContent>
        </Card>
      </div>

      {failureAnalysis && (
        <Card className="border-destructive/40">
          <CardHeader className="border-b bg-destructive/5">
            <CardTitle className="flex items-center gap-1.5 text-destructive">
              <Lightbulb className="h-3.5 w-3.5" />
              Why this run failed
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 pt-4 text-sm">
            <p>{failureAnalysis.likely_cause}</p>
            {failureAnalysis.evidence.length > 0 && (
              <ul className="space-y-1 text-code text-xs">
                {failureAnalysis.evidence.map((line, i) => (
                  <li key={i} className="rounded bg-muted px-2 py-1">{line}</li>
                ))}
              </ul>
            )}
            <p className="text-muted-foreground">
              <strong className="text-foreground">Suggested fix:</strong> {failureAnalysis.suggested_fix}
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
