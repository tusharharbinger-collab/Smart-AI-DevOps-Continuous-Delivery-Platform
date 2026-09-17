/**
 * frontend/src/pages/ReportsTab.tsx — Reports tab (P0, 2026-09-16).
 *
 * Real gap this closes: `GET /reports/digest/{tenant_id}` (rollback rate,
 * MTTV, cost trend) and `GET /reports/deployment/{run_id}` (per-run
 * baseline-vs-canary comparison, evidence, RCA) already existed and
 * worked — zero frontend code referenced either one. The digest is
 * intentionally tenant-wide (matches digest_generator.py's own query,
 * which has no project_id filter) — an account-level health summary shown
 * in project context, same as Render/Vercel's own dashboards — while the
 * per-run report picker below is scoped to this project's own runs.
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, Clock, DollarSign, Sparkles, TrendingDown, TrendingUp, Minus } from "lucide-react";
import { useAppContext } from "@/hooks/useAppContext";
import { getDeliveryHealthDigest, getDeploymentReport } from "@/api/reports";
import { listProjectRuns } from "@/api/projects";
import { VerdictBadge } from "@/components/verification/VerdictBadge";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

const TREND_STYLE: Record<string, { icon: typeof TrendingUp; className: string; label: string }> = {
  improving: { icon: TrendingUp, className: "text-success", label: "Improving" },
  stable: { icon: Minus, className: "text-muted-foreground", label: "Stable" },
  degrading: { icon: TrendingDown, className: "text-destructive", label: "Degrading" },
};

function StatCard({ label, value, sub, icon: Icon }: { label: string; value: string; sub?: string; icon: typeof Activity }) {
  return (
    <Card>
      <CardContent className="flex items-center justify-between p-4">
        <div>
          <span className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</span>
          <div className="text-stat text-xl">{value}</div>
          {sub && <span className="text-code text-[11px] text-muted-foreground">{sub}</span>}
        </div>
        <Icon className="h-6 w-6 text-muted-foreground/50" />
      </CardContent>
    </Card>
  );
}

function DeploymentReportView({ runId }: { runId: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["deployment-report", runId],
    queryFn: () => getDeploymentReport(runId),
  });

  if (isLoading) return <Skeleton className="h-48" />;
  if (!data) return <p className="text-sm text-muted-foreground">No verification record for this run yet.</p>;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <VerdictBadge status={data.final_verdict} confidence={data.confidence} />
        <span className="text-code text-xs text-muted-foreground">
          composite score {data.composite_score.toFixed(3)} · {new Date(data.timestamp_utc).toLocaleString()}
        </span>
      </div>

      {data.rca_summary && (
        <Card>
          <CardContent className="space-y-1 p-3">
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Root cause</span>
            <p className="text-sm">{data.rca_summary}</p>
          </CardContent>
        </Card>
      )}

      {data.cost_analysis && (
        <Card>
          <CardContent className="p-3">
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Cost delta for this run</span>
            <div className="mt-1 flex items-center gap-4 text-sm">
              <span>Baseline: <span className="text-code">${data.cost_analysis.baseline_cost.toFixed(4)}/hr</span></span>
              <span>Canary: <span className="text-code">${data.cost_analysis.canary_cost.toFixed(4)}/hr</span></span>
              <Badge variant={data.cost_analysis.delta_percent > 15 ? "destructive" : "secondary"}>
                {data.cost_analysis.delta_percent > 0 ? "+" : ""}
                {data.cost_analysis.delta_percent.toFixed(1)}%
              </Badge>
            </div>
          </CardContent>
        </Card>
      )}

      {data.tier1_breaches && data.tier1_breaches.length > 0 && (
        <Card>
          <CardContent className="p-3">
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Critical-tier breaches</span>
            <ul className="mt-1 list-inside list-disc text-sm">
              {data.tier1_breaches.map((b) => (
                <li key={b}>{b}</li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      {data.metric_evidence && (
        <Card>
          <CardContent className="p-3">
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Metric evidence</span>
            <pre className="mt-1 overflow-x-auto rounded-md border bg-background p-2 text-code text-[11px] leading-relaxed">
              {JSON.stringify(data.metric_evidence, null, 2)}
            </pre>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

export function ReportsTab() {
  const { tenantId, projectId } = useAppContext();
  const { data: digest, isLoading: digestLoading } = useQuery({
    queryKey: ["delivery-health-digest", tenantId],
    queryFn: () => getDeliveryHealthDigest(tenantId),
    enabled: Boolean(tenantId),
  });
  const { data: runsData, isLoading: runsLoading } = useQuery({
    queryKey: ["project-runs-for-reports", projectId],
    queryFn: () => listProjectRuns(projectId ?? ""),
    enabled: Boolean(projectId),
  });
  const [selectedRunId, setSelectedRunId] = useState<string | undefined>();

  const runs = runsData?.runs ?? [];
  const activeRunId = selectedRunId ?? runs[0]?.pipeline_run_id;
  const trend = digest?.ai_summary ? TREND_STYLE[digest.ai_summary.notable_trend] : undefined;
  const TrendIcon = trend?.icon ?? Minus;

  if (digestLoading) return <Skeleton className="h-96" />;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label="Success rate (7d)"
          value={digest ? `${digest.pipeline_success_rate_percent}%` : "—"}
          sub={digest ? `${digest.total_deployments} deployment(s)` : undefined}
          icon={Activity}
        />
        <StatCard
          label="Rollback rate"
          value={digest ? `${digest.rollback_rate_percent}%` : "—"}
          sub={digest ? `${digest.rollback_count} rollback(s)` : undefined}
          icon={TrendingDown}
        />
        <StatCard
          label="Mean time to verify"
          value={digest?.mean_time_to_verify_seconds != null ? `${Math.round(digest.mean_time_to_verify_seconds)}s` : "—"}
          icon={Clock}
        />
        <StatCard
          label="Avg cost delta"
          value={digest ? `${digest.avg_cost_delta_percent > 0 ? "+" : ""}${digest.avg_cost_delta_percent}%` : "—"}
          icon={DollarSign}
        />
      </div>

      <Card>
        <CardContent className="p-4">
          <div className="mb-2 flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-primary" />
            <h2 className="text-sm font-semibold">AI Summary</h2>
            {trend && (
              <Badge variant="secondary" className={trend.className}>
                <TrendIcon className="mr-1 h-3 w-3" />
                {trend.label}
              </Badge>
            )}
          </div>
          {digest?.ai_summary ? (
            <div className="space-y-2">
              <p className="text-sm">{digest.ai_summary.summary}</p>
              {digest.ai_summary.talking_points.length > 0 && (
                <ul className="list-inside list-disc text-xs text-muted-foreground">
                  {digest.ai_summary.talking_points.map((p) => (
                    <li key={p}>{p}</li>
                  ))}
                </ul>
              )}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No digest data available yet for this tenant.</p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold">Deployment Report</h2>
            {runs.length > 0 && (
              <Select value={activeRunId} onValueChange={setSelectedRunId}>
                <SelectTrigger className="w-64">
                  <SelectValue placeholder="Select a run" />
                </SelectTrigger>
                <SelectContent>
                  {runs.map((r) => (
                    <SelectItem key={r.pipeline_run_id} value={r.pipeline_run_id}>
                      {r.pipeline_run_id.slice(0, 8)}… — {r.commit_message ?? r.trigger_type ?? "run"}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
          {runsLoading ? (
            <Skeleton className="h-32" />
          ) : activeRunId ? (
            <DeploymentReportView runId={activeRunId} />
          ) : (
            <p className="text-sm text-muted-foreground">No runs yet for this project.</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
