/**
 * frontend/src/pages/ProjectsOverview.tsx — Phase 8, deliverable 8.4.
 *
 * The Render-style multi-project dashboard. Every number on a card comes
 * from a real backend aggregate; a project with no finished runs in the
 * 7-day window shows "—" rather than a flattering placeholder, and there is
 * no cost-delta stat because nothing computes one for these runs yet.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { useState } from "react";
import { toast } from "sonner";
import { GitBranch, FolderGit2, Plus, Rocket, Search, Timer, TrendingUp } from "lucide-react";
import { listProjects, triggerRollout, type ProjectSummary } from "@/api/projects";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";

function StatusBadge({ project }: { project: ProjectSummary }) {
  const run = project.latest_run_status;
  const weight = project.latest_traffic_weight ?? 0;

  if (run === "RUNNING" || run === "PENDING") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-primary/30 bg-primary/10 px-2.5 py-0.5 text-code text-[11px] font-medium text-primary">
        <span className="relative flex h-1.5 w-1.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary opacity-75" />
          <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-primary" />
        </span>
        CANARY RUNNING · {weight}%
      </span>
    );
  }
  if (run === "FAILED" || run === "ROLLED_BACK") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-destructive/30 bg-destructive/10 px-2.5 py-0.5 text-code text-[11px] font-medium text-destructive">
        <span className="h-1.5 w-1.5 rounded-full bg-destructive" />
        {run === "ROLLED_BACK" ? "ROLLED BACK" : "FAILED"}
      </span>
    );
  }
  if (run === "COMPLETED") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-success/30 bg-success/10 px-2.5 py-0.5 text-code text-[11px] font-medium text-success">
        <span className="h-1.5 w-1.5 rounded-full bg-success" />
        HEALTHY · {project.active_production_tag}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border bg-muted px-2.5 py-0.5 text-code text-[11px] font-medium text-muted-foreground">
      <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/60" />
      NO DEPLOYS YET
    </span>
  );
}

function relativeTime(iso: string | null): string {
  if (!iso) return "never";
  const deltaMs = Date.now() - new Date(iso).getTime();
  const minutes = Math.floor(deltaMs / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function ProjectCard({ project, onTrigger }: { project: ProjectSummary; onTrigger: (p: ProjectSummary) => void }) {
  // Adopted pre-project pipelines have no repository behind them — say so
  // rather than showing an empty badge or an invented URL.
  const repoLabel = project.repo_url
    ? project.repo_url.replace(/^https?:\/\//, "")
    : "No repository connected";
  return (
    <Card className="flex flex-col transition-colors hover:border-primary/40">
      <CardContent className="flex flex-1 flex-col gap-3 p-4">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <Link
              to={`/projects/${project.project_id}`}
              className="block truncate text-sm font-semibold hover:text-primary hover:underline"
            >
              {project.name}
            </Link>
            <div className="mt-1 flex flex-wrap items-center gap-1.5 text-code text-[11px] text-muted-foreground">
              <span className="inline-flex items-center gap-1 truncate">
                <FolderGit2 className="h-3 w-3 shrink-0" />
                {repoLabel}
              </span>
              <span className="inline-flex items-center gap-1 rounded border px-1.5">
                <GitBranch className="h-2.5 w-2.5" />
                {project.branch}
              </span>
            </div>
          </div>
          <StatusBadge project={project} />
        </div>

        <div className="grid grid-cols-2 gap-2 border-y py-2.5">
          <div>
            <span className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-muted-foreground">
              <TrendingUp className="h-2.5 w-2.5" /> Success rate (7d)
            </span>
            <span className="text-stat text-sm">
              {project.success_rate_7d === null ? "—" : `${project.success_rate_7d}%`}
            </span>
            <span className="ml-1.5 text-code text-[10px] text-muted-foreground">
              {project.runs_7d} run{project.runs_7d === 1 ? "" : "s"}
            </span>
          </div>
          <div>
            <span className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-muted-foreground">
              <Timer className="h-2.5 w-2.5" /> Mean time to verify
            </span>
            <span className="text-stat text-sm">
              {project.mttv_seconds === null ? "—" : `${Math.round(project.mttv_seconds)}s`}
            </span>
          </div>
        </div>

        <div className="mt-auto flex items-center justify-between gap-2">
          <span className="truncate text-code text-[11px] text-muted-foreground">
            {project.latest_started_at
              ? `Deployed ${relativeTime(project.latest_started_at)}${
                  project.latest_commit_sha ? ` · git:${project.latest_commit_sha.slice(0, 7)}` : ""
                }`
              : "Not deployed yet"}
          </span>
          <Button size="sm" variant="outline" onClick={() => onTrigger(project)}>
            <Rocket className="h-3 w-3" /> Trigger Rollout
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export function ProjectsOverview() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");

  const { data, isLoading } = useQuery({
    queryKey: ["projects"],
    queryFn: listProjects,
    refetchInterval: 5000,
  });

  const projects = (data?.projects ?? []).filter((p) =>
    search.trim()
      ? `${p.name} ${p.repo_url ?? ""} ${p.branch}`.toLowerCase().includes(search.toLowerCase())
      : true
  );

  async function handleTrigger(project: ProjectSummary) {
    try {
      const res = await triggerRollout(project.project_id);
      toast.success(`Rollout started for ${project.name}`, { description: res.pipeline_run_id });
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      navigate(`/projects/${project.project_id}`);
    } catch (err) {
      toast.error("Could not start rollout", { description: (err as Error).message });
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Overview</h1>
          <p className="text-sm text-muted-foreground">
            Manage, verify, and monitor your continuous delivery services.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Filter services…"
              className="h-8 w-56 pl-8 text-xs"
            />
          </div>
          <Button size="sm" onClick={() => navigate("/projects/new")}>
            <Plus className="h-3.5 w-3.5" /> New Service
          </Button>
        </div>
      </div>

      {isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <Skeleton className="h-44" />
          <Skeleton className="h-44" />
          <Skeleton className="h-44" />
        </div>
      ) : projects.length === 0 ? (
        <div className="grid gap-4 sm:grid-cols-2">
          <Card className="border-dashed">
            <CardContent className="flex flex-col items-start gap-2 p-6">
              <div className="flex h-9 w-9 items-center justify-center rounded-md bg-primary/10 text-primary">
                <FolderGit2 className="h-5 w-5" />
              </div>
              <h3 className="text-sm font-semibold">Deploy a Web Service</h3>
              <p className="text-xs text-muted-foreground">
                Connect a GitHub repository, configure its build and test commands, and let statistical
                verification decide whether each release is promoted or rolled back.
              </p>
              <Button size="sm" className="mt-2" onClick={() => navigate("/projects/new")}>
                <Plus className="h-3.5 w-3.5" /> Create your first service
              </Button>
            </CardContent>
          </Card>
          <Card className="border-dashed">
            <CardContent className="flex flex-col items-start gap-2 p-6">
              <div className="flex h-9 w-9 items-center justify-center rounded-md bg-success/10 text-success">
                <Rocket className="h-5 w-5" />
              </div>
              <h3 className="text-sm font-semibold">Registered a pipeline through the API?</h3>
              <p className="text-xs text-muted-foreground">
                It shows up here automatically — every pipeline gets a workspace, whether it came from this
                wizard or from <span className="text-code">POST /api/v1/pipelines</span>. If this list is
                empty, no pipeline exists for your tenant yet.
              </p>
            </CardContent>
          </Card>
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {projects.map((project) => (
            <ProjectCard key={project.project_id} project={project} onTrigger={handleTrigger} />
          ))}
        </div>
      )}
    </div>
  );
}
