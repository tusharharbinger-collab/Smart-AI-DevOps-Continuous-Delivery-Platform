/**
 * frontend/src/pages/ProjectWorkspace.tsx — Phase 8, deliverable 8.6.
 *
 * The isolated per-project shell. This is a LAYOUT route: it resolves the
 * project to its `pipeline_id` + selected run and hands the nested routes
 * the exact same `{pipelineId, pipelineRunId, tenantId}` outlet context
 * AppLayout already provides — which is why Pipeline View, Verification
 * Inspector, Policy & Gates and Audit Ledger render here UNCHANGED, scoped
 * to this project's own pipeline and run. That reuse is the whole point of
 * a project wrapping an existing pipeline rather than duplicating one (see
 * docs/roadmap/08-project-workspaces.md).
 */
import { useEffect } from "react";
import { NavLink, Outlet, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle, ArrowLeft, ExternalLink, GitBranch, FolderGit2, Pause, Play, Rocket,
} from "lucide-react";
import {
  getProject, getRunStages, listProjectRuns, rollbackRun, triggerRollout,
} from "@/api/projects";
import { pausePipeline, resumePipeline } from "@/api/pipeline";
import { useAuthStore } from "@/lib/auth-store";
import type { AppContext } from "@/types/app-context";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";

const TABS = [
  { to: "pipeline", label: "Pipeline View" },
  { to: "verification", label: "Verification Inspector" },
  { to: "policy", label: "Policy & Gates" },
  { to: "audit", label: "Audit Ledger" },
];

const STAGE_LABELS: Record<string, string> = {
  build: "Build",
  test: "Test",
  canary_verify: "Progressive Canary",
};

function StageStepper({ projectId, runId }: { projectId: string; runId: string }) {
  const { data } = useQuery({
    queryKey: ["project-stages", projectId, runId],
    queryFn: () => getRunStages(projectId, runId),
    refetchInterval: 3000,
    enabled: Boolean(runId),
  });

  if (!data) return null;

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {data.stages.map((stage, i) => {
        const isFailed = stage.status === "FAILED";
        const isDone = stage.status === "SUCCESS";
        const isRunning = stage.status === "RUNNING";
        return (
          <span key={stage.name} className="flex items-center gap-1.5">
            <span
              className={`inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-code text-[11px] font-medium ${
                isFailed
                  ? "bg-destructive/15 text-destructive"
                  : isDone
                  ? "bg-success/15 text-success"
                  : isRunning
                  ? "bg-primary/15 text-primary"
                  : "bg-muted text-muted-foreground"
              }`}
            >
              <span className="opacity-60">{i + 1}.</span>
              {STAGE_LABELS[stage.name] ?? stage.name}
              <span className="opacity-70">
                {stage.name === "canary_verify" && data.current_traffic_weight != null && isRunning
                  ? `${data.current_traffic_weight}%`
                  : stage.status.toLowerCase()}
              </span>
            </span>
            {i < data.stages.length - 1 && <span className="text-muted-foreground">→</span>}
          </span>
        );
      })}
    </div>
  );
}

export function ProjectWorkspace() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const session = useAuthStore((s) => s.session);
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedRunId = searchParams.get("run") ?? "";

  const { data: project, isLoading } = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => getProject(projectId),
    refetchInterval: 5000,
    enabled: Boolean(projectId),
  });

  const { data: runsData } = useQuery({
    queryKey: ["project-runs", projectId],
    queryFn: () => listProjectRuns(projectId),
    refetchInterval: 5000,
    enabled: Boolean(projectId),
  });

  const runs = runsData?.runs ?? [];

  // Auto-select the latest run when the URL doesn't name one, or names one
  // that doesn't belong to this project (e.g. a stale link from another
  // project's workspace).
  useEffect(() => {
    if (runs.length === 0) return;
    if (selectedRunId && runs.some((r) => r.pipeline_run_id === selectedRunId)) return;
    const next = new URLSearchParams(searchParams);
    next.set("run", runs[0].pipeline_run_id);
    setSearchParams(next, { replace: true });
  }, [runs, selectedRunId, searchParams, setSearchParams]);

  async function handleTrigger() {
    try {
      const res = await triggerRollout(projectId);
      const next = new URLSearchParams(searchParams);
      next.set("run", res.pipeline_run_id);
      setSearchParams(next, { replace: true });
      queryClient.invalidateQueries({ queryKey: ["project-runs", projectId] });
      toast.success("Rollout triggered", { description: res.pipeline_run_id });
    } catch (err) {
      toast.error("Trigger failed", { description: (err as Error).message });
    }
  }

  async function handlePause() {
    try {
      await pausePipeline(selectedRunId);
      toast.success("Rollout paused");
    } catch (err) {
      toast.error("Pause failed", { description: (err as Error).message });
    }
  }

  async function handleResume() {
    try {
      await resumePipeline(selectedRunId);
      toast.success("Rollout resumed");
    } catch (err) {
      toast.error("Resume failed", { description: (err as Error).message });
    }
  }

  async function handleRollback() {
    try {
      await rollbackRun(projectId, selectedRunId);
      toast.success("Emergency rollback requested");
      queryClient.invalidateQueries({ queryKey: ["project-runs", projectId] });
    } catch (err) {
      toast.error("Rollback failed", { description: (err as Error).message });
    }
  }

  if (isLoading) return <Skeleton className="h-72" />;
  if (!project) return <p className="text-sm text-destructive">Project not found.</p>;

  const repoLabel = project.repo_url?.replace(/^https?:\/\//, "") ?? null;

  return (
    <div className="space-y-4">
      {/* Sub-header */}
      <div className="space-y-3 border-b pb-3">
        <Button variant="ghost" size="sm" onClick={() => navigate("/projects")} className="-ml-2">
          <ArrowLeft className="h-3.5 w-3.5" /> Overview
        </Button>

        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-lg font-semibold tracking-tight">{project.name}</h1>
              <Badge variant="secondary" className="text-code">{project.active_production_tag}</Badge>
              {project.canary_tag && (
                <Badge variant="outline" className="text-code">canary {project.canary_tag}</Badge>
              )}
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-code text-[11px] text-muted-foreground">
              {repoLabel ? (
                <a
                  href={project.repo_url ?? undefined}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 hover:text-primary hover:underline"
                >
                  <FolderGit2 className="h-3 w-3" /> {repoLabel} <ExternalLink className="h-2.5 w-2.5" />
                </a>
              ) : (
                <span className="inline-flex items-center gap-1 italic">
                  <FolderGit2 className="h-3 w-3" /> No repository connected
                </span>
              )}
              <span className="inline-flex items-center gap-1 rounded border px-1.5">
                <GitBranch className="h-2.5 w-2.5" /> {project.branch}
              </span>
              {project.container_image && <span>image: {project.container_image}</span>}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="success" onClick={handleTrigger}>
              <Rocket className="h-3.5 w-3.5" /> Trigger New Rollout
            </Button>
            <Button size="sm" variant="outline" onClick={handlePause} disabled={!selectedRunId}>
              <Pause className="h-3.5 w-3.5" /> Pause
            </Button>
            <Button size="sm" variant="outline" onClick={handleResume} disabled={!selectedRunId}>
              <Play className="h-3.5 w-3.5" /> Resume
            </Button>
            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button size="sm" variant="destructive" disabled={!selectedRunId}>
                  <AlertTriangle className="h-3.5 w-3.5" /> Emergency Rollback
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>Roll back {project.name}?</AlertDialogTitle>
                  <AlertDialogDescription>
                    This requests an immediate traffic cut to 0% for this run. It still goes through HMAC
                    verification and OPA policy evaluation before anything is actuated — exactly like an
                    autonomous rollback — but it cannot be undone once policy authorizes it.
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

        <div className="flex flex-wrap items-center justify-between gap-3">
          {selectedRunId ? (
            <StageStepper projectId={projectId} runId={selectedRunId} />
          ) : (
            <span className="text-xs italic text-muted-foreground">
              No runs yet — trigger one to start the build → test → canary sequence.
            </span>
          )}

          {runs.length > 0 && (
            <div className="flex items-center gap-1.5">
              <span className="text-code text-[11px] text-muted-foreground">Run:</span>
              <Select
                value={selectedRunId}
                onValueChange={(v) => {
                  const next = new URLSearchParams(searchParams);
                  next.set("run", v);
                  setSearchParams(next, { replace: true });
                }}
              >
                <SelectTrigger className="h-7 w-72 text-code text-[11px]"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {runs.map((r) => (
                    <SelectItem key={r.pipeline_run_id} value={r.pipeline_run_id}>
                      {r.pipeline_run_id.slice(0, 8)}… — {r.status}
                      {r.commit_sha ? ` — git:${r.commit_sha.slice(0, 7)}` : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
        </div>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1 overflow-x-auto border-b pb-1">
        {TABS.map((tab) => (
          <NavLink
            key={tab.to}
            to={{ pathname: `/projects/${projectId}/${tab.to}`, search: searchParams.toString() }}
            className={({ isActive }) =>
              `shrink-0 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                isActive ? "bg-primary/15 text-primary" : "text-muted-foreground hover:bg-accent hover:text-foreground"
              }`
            }
          >
            {tab.label}
          </NavLink>
        ))}
      </div>

      <Outlet
        context={
          {
            pipelineId: project.pipeline_id ?? "",
            pipelineRunId: selectedRunId,
            tenantId: session?.tenant_id ?? "",
            hideRunControls: true,
            projectId,
          } satisfies AppContext
        }
      />
    </div>
  );
}
