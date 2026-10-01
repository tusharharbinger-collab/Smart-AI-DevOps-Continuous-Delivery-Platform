/**
 * frontend/src/components/ProjectDeleteProgress.tsx
 *
 * Real gap found live (2026-09-30): deleting a project used to be one opaque spinner with no way to tell
 * what was actually happening — a CloudFormation/ECS teardown call can take several seconds each, and the
 * old UI gave no sign whether it was stuck or just slow, or whether AWS cost had actually stopped yet. This
 * polls the real per-step job status (`GET /projects/{id}/delete-status/{job_id}`) and renders it as a
 * timeline — same status-icon-per-row pattern as `InfraBuildTimeline.tsx` — plus an overall progress bar, so
 * the user watches real AWS/DB teardown happen step by step rather than guessing.
 */
import { useEffect, useState } from "react";
import { CheckCircle2, Circle, CircleDot, Loader2, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";
import { getDeleteJobStatus, type DeleteJobStatus, type DeleteStep } from "@/api/projects";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";

const ROW_ICON: Record<DeleteStep["status"], React.ReactNode> = {
  pending: <Circle className="h-4 w-4 text-muted-foreground/50" />,
  running: <CircleDot className="h-4 w-4 animate-pulse text-primary" />,
  done: <CheckCircle2 className="h-4 w-4 text-success" />,
  failed: <XCircle className="h-4 w-4 text-destructive" />,
};

const ROW_TEXT: Record<DeleteStep["status"], string> = {
  pending: "text-muted-foreground",
  running: "text-primary",
  done: "text-foreground",
  failed: "text-destructive",
};

export function ProjectDeleteProgress({
  projectId, jobId, projectName, onDone, onClose,
}: {
  projectId: string;
  jobId: string;
  projectName: string;
  /** Called ONCE, the moment the job reaches a terminal state (COMPLETED or FAILED) — side effects only
   * (toast, refetch). The dialog stays open after this so the user can actually see the final result. */
  onDone: (job: DeleteJobStatus) => void;
  /** Called when the user dismisses the dialog (only possible once terminal) — the caller unmounts this component here. */
  onClose: () => void;
}) {
  const [job, setJob] = useState<DeleteJobStatus | null>(null);
  const [open, setOpen] = useState(true);

  useEffect(() => {
    let cancelled = false;
    let doneCalled = false;
    const interval = setInterval(async () => {
      try {
        const result = await getDeleteJobStatus(projectId, jobId);
        if (cancelled) return;
        setJob(result);
        if (result.status !== "RUNNING" && !doneCalled) {
          doneCalled = true;
          clearInterval(interval);
          onDone(result);
        }
      } catch {
        // A transient poll failure isn't fatal - the next tick tries again.
      }
    }, 700);
    return () => { cancelled = true; clearInterval(interval); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, jobId]);

  const steps = job?.steps ?? [];
  const doneCount = steps.filter((s) => s.status === "done" || s.status === "failed").length;
  const progressPercent = steps.length === 0 ? 0 : Math.round((doneCount / steps.length) * 100);
  const isTerminal = job?.status === "COMPLETED" || job?.status === "FAILED";

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next && !isTerminal) return; setOpen(next); }}>
      <DialogContent
        className="sm:max-w-md"
        onInteractOutside={(e) => { if (!isTerminal) e.preventDefault(); }}
        onEscapeKeyDown={(e) => { if (!isTerminal) e.preventDefault(); }}
        hideClose
      >
        <DialogHeader>
          <DialogTitle>
            {isTerminal
              ? job?.status === "COMPLETED"
                ? `${projectName} deleted`
                : `${projectName}: deletion finished with errors`
              : `Deleting ${projectName}…`}
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
            <div
              className={cn("h-full transition-all duration-300", job?.status === "FAILED" ? "bg-destructive" : "bg-primary")}
              style={{ width: `${progressPercent}%` }}
            />
          </div>

          {steps.length === 0 ? (
            <p className="flex items-center gap-1.5 text-sm text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Starting…
            </p>
          ) : (
            <div className="space-y-0">
              {steps.map((s, i) => {
                const isLast = i === steps.length - 1;
                return (
                  <div key={s.key} className="flex gap-3">
                    <div className="flex flex-col items-center">
                      {ROW_ICON[s.status]}
                      {!isLast && (
                        <div className={cn("my-0.5 w-px flex-1 min-h-[1.25rem]", s.status === "done" ? "bg-success/40" : "bg-border")} />
                      )}
                    </div>
                    <div className={cn("pb-3", isLast && "pb-0")}>
                      <span className={cn("text-sm font-medium", ROW_TEXT[s.status])}>{s.label}</span>
                      {s.detail && <p className="mt-0.5 text-[11px] text-muted-foreground">{s.detail}</p>}
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {job?.pipeline_retained && (
            <p className="rounded-md border border-warning/40 bg-warning/5 p-2 text-xs text-warning">
              The pipeline record was kept — it still has other real run/audit history attached, so removing it
              would have destroyed that history. Everything else was deleted.
            </p>
          )}

          {isTerminal && (
            <Button size="sm" className="w-full" onClick={() => { setOpen(false); onClose(); }}>
              Close
            </Button>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
