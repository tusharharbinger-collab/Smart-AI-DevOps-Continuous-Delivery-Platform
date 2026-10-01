/**
 * frontend/src/pages/ReportsTab.tsx — Reports tab (P0, 2026-09-16; redesigned 2026-09-28).
 *
 * Real gap this closes: `GET /reports/digest/{tenant_id}` (rollback rate,
 * MTTV, cost trend) and `GET /reports/deployment/{run_id}` (per-run
 * baseline-vs-canary comparison, evidence, RCA) already existed and
 * worked — zero frontend code referenced either one. The digest is
 * intentionally tenant-wide (matches digest_generator.py's own query,
 * which has no project_id filter) — an account-level health summary shown
 * in project context, same as Render/Vercel's own dashboards — while the
 * per-run report picker below is scoped to this project's own runs.
 *
 * 2026-09-28 redesign: the original version was four flat numeric tiles, an AI summary in a plain card, and a
 * per-run report that dumped `metric_evidence` as raw JSON — real data, presented with no hierarchy. Reworked
 * around three ideas confirmed as current dashboard practice (sparklines that show trend without a full chart;
 * color communicating status rather than decorating it; an AI summary treated as the headline insight, not a
 * footnote): stat tiles are now color-graded against real thresholds and carry a real cost-delta sparkline
 * (existing GET /cost-history data, not a new endpoint), the AI summary is an elevated insight banner, and the
 * per-run report renders evidence as labeled cards/charts (MetricEvidenceCard) instead of a JSON blob. No new
 * data is fabricated anywhere — every number already existed in an endpoint this file just didn't use well.
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity, AlertTriangle, Clock, DollarSign, GitCommit, Sparkles, TrendingDown, TrendingUp, Minus,
} from "lucide-react";
import { useAppContext } from "@/hooks/useAppContext";
import { getDeliveryHealthDigest, getDeploymentReport } from "@/api/reports";
import { getCostHistory } from "@/api/reports";
import { listProjectRuns } from "@/api/projects";
import { VerdictBadge } from "@/components/verification/VerdictBadge";
import { ConfidenceGauge } from "@/components/charts/ConfidenceGauge";
import { MetricEvidenceCard } from "@/components/verification/MetricEvidenceCard";
import { StatTile, type Health } from "@/components/dashboard/StatTile";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { RcaDiffViewer, StructuredRca } from "@/components/verification/RcaDiffViewer";
import { cn } from "@/lib/utils";

const TREND_STYLE: Record<string, { icon: typeof TrendingUp; className: string; border: string; label: string }> = {
  improving: { icon: TrendingUp, className: "text-success", border: "border-l-success", label: "Improving" },
  stable: { icon: Minus, className: "text-muted-foreground", border: "border-l-muted-foreground/40", label: "Stable" },
  degrading: { icon: TrendingDown, className: "text-destructive", border: "border-l-destructive", label: "Degrading" },
};

function successRateHealth(pct: number): Health {
  return pct >= 95 ? "good" : pct >= 80 ? "warn" : "bad";
}
function rollbackRateHealth(pct: number): Health {
  return pct === 0 ? "good" : pct < 10 ? "warn" : "bad";
}
function costDeltaHealth(pct: number): Health {
  return pct <= 0 ? "good" : pct <= 15 ? "warn" : "bad";
}

function DeploymentReportView({ runId, deployMode }: { runId: string; deployMode?: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["deployment-report", runId],
    queryFn: () => getDeploymentReport(runId),
  });

  if (isLoading) return <Skeleton className="h-48" />;
  if (!data) {
    // Real gap found live: blue-green deliberately never runs statistical verification at all (it gates the
    // cutover on real infrastructure health instead — see worker.py's blue-green branch) — this run genuinely
    // has no verification record BY DESIGN, not because anything is broken. The old generic message read as a
    // bug to anyone who didn't already know that.
    if (deployMode === "blue_green") {
      return (
        <div className="flex items-start gap-2 rounded-md border border-dashed p-3 text-sm text-muted-foreground">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <p>
            This project uses <span className="font-medium text-foreground">blue-green</span> deployment, which is gated
            on real infrastructure health and a live-URL check, not statistical verification — so this run has no
            verification report by design, not because anything failed.
          </p>
        </div>
      );
    }
    return <p className="text-sm text-muted-foreground">No verification record for this run yet.</p>;
  }

  let structuredRca: StructuredRca | null = null;
  let summaryText = data.rca_summary;
  if (data.rca_summary) {
    try {
      if (data.rca_summary.trim().startsWith("{")) {
        const parsed = JSON.parse(data.rca_summary);
        if (parsed && typeof parsed === "object" && parsed.executive_summary) {
          structuredRca = parsed;
          summaryText = parsed.executive_summary;
        }
      }
    } catch {
      // fallback to plain text
    }
  }

  const evidenceEntries = data.metric_evidence ? Object.entries(data.metric_evidence) : [];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-4 rounded-md border bg-muted/20 p-3">
        <ConfidenceGauge confidence={data.composite_score / 100} />
        <div className="min-w-[180px] flex-1 space-y-1">
          <VerdictBadge status={data.final_verdict} confidence={data.confidence} />
          <p className="text-[11px] text-muted-foreground">
            Composite score {data.composite_score.toFixed(1)}/100 · {new Date(data.timestamp_utc).toLocaleString()}
          </p>
        </div>
      </div>

      {structuredRca ? (
        <RcaDiffViewer rca={structuredRca} />
      ) : summaryText ? (
        <Card>
          <CardContent className="space-y-1 p-3">
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Root cause</span>
            <p className="text-sm">{summaryText}</p>
          </CardContent>
        </Card>
      ) : null}

      {data.cost_analysis && (
        <Card>
          <CardContent className="p-3">
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Cost delta for this run</span>
            <div className="mt-1 flex items-center gap-4 text-sm">
              <span>Baseline: <span className="text-code">${data.cost_analysis.baseline_cost.toFixed(4)}/hr</span></span>
              <span>{deployMode === "blue_green" ? "Green" : "Canary"}: <span className="text-code">${data.cost_analysis.canary_cost.toFixed(4)}/hr</span></span>
              <Badge variant={data.cost_analysis.delta_percent > 15 ? "destructive" : "secondary"}>
                {data.cost_analysis.delta_percent > 0 ? "+" : ""}
                {data.cost_analysis.delta_percent.toFixed(1)}%
              </Badge>
            </div>
          </CardContent>
        </Card>
      )}

      {data.tier1_breaches && data.tier1_breaches.length > 0 && (
        <Card className="border-destructive/40">
          <CardContent className="p-3">
            <span className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-destructive">
              <AlertTriangle className="h-3.5 w-3.5" /> Critical-tier breaches
            </span>
            <ul className="mt-1 list-inside list-disc text-sm">
              {data.tier1_breaches.map((b) => (
                <li key={b}>{b}</li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      {evidenceEntries.length > 0 && (
        <div className="space-y-2">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            Metric evidence ({evidenceEntries.length})
          </span>
          <div className="grid gap-2 lg:grid-cols-2">
            {evidenceEntries.map(([name, entry]) => (
              <MetricEvidenceCard key={name} metricName={name} evidence={entry} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export function ReportsTab() {
  const { tenantId, projectId, deployMode } = useAppContext();
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
  const { data: costData } = useQuery({
    queryKey: ["cost-history-for-reports-sparkline", projectId],
    queryFn: () => getCostHistory(projectId ?? "", 12),
    enabled: Boolean(projectId),
  });
  const [selectedRunId, setSelectedRunId] = useState<string | undefined>();

  const runs = runsData?.runs ?? [];
  const activeRunId = selectedRunId ?? runs[0]?.pipeline_run_id;
  const trend = digest?.ai_summary ? TREND_STYLE[digest.ai_summary.notable_trend] : undefined;
  const TrendIcon = trend?.icon ?? Minus;
  // Oldest-to-newest for a left-to-right sparkline; cost-history itself comes back newest-first.
  const costDeltaSpark = costData ? [...costData.cost_history].reverse().map((c) => c.delta_percent) : undefined;

  if (digestLoading) return <Skeleton className="h-96" />;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="Success rate (7d)"
          value={digest ? `${digest.pipeline_success_rate_percent}%` : "—"}
          sub={digest ? `${digest.total_deployments} deployment(s)` : undefined}
          icon={Activity}
          health={digest ? successRateHealth(digest.pipeline_success_rate_percent) : "neutral"}
        />
        <StatTile
          label="Rollback rate"
          value={digest ? `${digest.rollback_rate_percent}%` : "—"}
          sub={digest ? `${digest.rollback_count} rollback(s)` : undefined}
          icon={TrendingDown}
          health={digest ? rollbackRateHealth(digest.rollback_rate_percent) : "neutral"}
        />
        <StatTile
          label="Mean time to verify"
          value={digest?.mean_time_to_verify_seconds != null ? `${Math.round(digest.mean_time_to_verify_seconds)}s` : "—"}
          icon={Clock}
        />
        <StatTile
          label="Avg cost delta"
          value={digest ? `${digest.avg_cost_delta_percent > 0 ? "+" : ""}${digest.avg_cost_delta_percent}%` : "—"}
          sub={costDeltaSpark && costDeltaSpark.length >= 2 ? `last ${costDeltaSpark.length} runs` : undefined}
          icon={DollarSign}
          health={digest ? costDeltaHealth(digest.avg_cost_delta_percent) : "neutral"}
          sparkline={costDeltaSpark}
        />
      </div>

      <Card className={cn("border-l-4", trend?.border ?? "border-l-border")}>
        <CardContent className="p-4">
          <div className="mb-2 flex items-center gap-2">
            <div className="rounded-md bg-primary/10 p-1.5">
              <Sparkles className="h-4 w-4 text-primary" />
            </div>
            <h2 className="text-sm font-semibold">AI Delivery Summary</h2>
            {trend && (
              <Badge variant="secondary" className={trend.className}>
                <TrendIcon className="mr-1 h-3 w-3" />
                {trend.label}
              </Badge>
            )}
          </div>
          {digest?.ai_summary ? (
            <div className="space-y-2">
              <p className="text-sm leading-relaxed">{digest.ai_summary.summary}</p>
              {digest.ai_summary.talking_points.length > 0 && (
                <ul className="grid gap-1 sm:grid-cols-2">
                  {digest.ai_summary.talking_points.map((p) => (
                    <li key={p} className="flex items-start gap-1.5 text-xs text-muted-foreground">
                      <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-muted-foreground/60" />
                      {p}
                    </li>
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
                <SelectTrigger className="w-72">
                  <SelectValue placeholder="Select a run" />
                </SelectTrigger>
                <SelectContent>
                  {runs.map((r) => (
                    <SelectItem key={r.pipeline_run_id} value={r.pipeline_run_id}>
                      <span className="flex items-center gap-1.5">
                        <GitCommit className="h-3 w-3 shrink-0 text-muted-foreground" />
                        {r.pipeline_run_id.slice(0, 8)}… — {r.commit_message ?? r.trigger_type ?? "run"}
                      </span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
          {runsLoading ? (
            <Skeleton className="h-32" />
          ) : activeRunId ? (
            <DeploymentReportView runId={activeRunId} deployMode={deployMode} />
          ) : (
            <p className="text-sm text-muted-foreground">No runs yet for this project.</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
