/**
 * frontend/src/pages/CostTab.tsx — Cost tab (P0, 2026-09-16).
 *
 * Real gap this closes: cost_tracker.py/cost_tracker_ecs.py have recorded
 * genuine per-run cost deltas into `cost_analysis` since Module 8 — zero
 * frontend code ever read them. Reuses that data via the new
 * GET /{project_id}/cost-history endpoint (the one new backend piece this
 * work added), never a second cost computation.
 *
 * Right-sizing: ECS projects get a recommendation built from real CloudWatch utilization of the
 * baseline service (cost_tracker_ecs.py::compute_ecs_rightsizing), shown here with the evidence behind it
 * (samples, window, measured p95) and a caveat. It only appears once there are enough datapoints - below
 * that, an honest "not enough usage data yet" state is rendered instead of an estimated number.
 */
import { useQuery } from "@tanstack/react-query";
import { DollarSign, Gauge, TrendingDown, TrendingUp } from "lucide-react";
import { useAppContext } from "@/hooks/useAppContext";
import { getCostHistory } from "@/api/reports";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableHeader, TableBody, TableHead, TableRow, TableCell } from "@/components/ui/table";

function StatCard({ label, value, icon: Icon }: { label: string; value: string; icon: typeof DollarSign }) {
  return (
    <Card>
      <CardContent className="flex items-center justify-between p-4">
        <div>
          <span className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</span>
          <div className="text-stat text-xl">{value}</div>
        </div>
        <Icon className="h-6 w-6 text-muted-foreground/50" />
      </CardContent>
    </Card>
  );
}

export function CostTab() {
  const { projectId } = useAppContext();
  const { data, isLoading } = useQuery({
    queryKey: ["cost-history", projectId],
    queryFn: () => getCostHistory(projectId ?? ""),
    enabled: Boolean(projectId),
  });

  if (isLoading) return <Skeleton className="h-96" />;

  const history = data?.cost_history ?? [];
  const delta =
    data && data.total_baseline_cost > 0
      ? ((data.total_canary_cost - data.total_baseline_cost) / data.total_baseline_cost) * 100
      : 0;
  const latestRec = history.find((h) => h.rightsizing_rec)?.rightsizing_rec ?? null;
  const latestPerf = history.find((h) => h.performance_correlation)?.performance_correlation ?? null;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
        <StatCard label="Baseline cost (sum, $/hr)" value={`$${(data?.total_baseline_cost ?? 0).toFixed(4)}`} icon={DollarSign} />
        <StatCard label="Canary cost (sum, $/hr)" value={`$${(data?.total_canary_cost ?? 0).toFixed(4)}`} icon={DollarSign} />
        <StatCard
          label="Net delta"
          value={`${delta > 0 ? "+" : ""}${delta.toFixed(1)}%`}
          icon={delta > 0 ? TrendingUp : TrendingDown}
        />
        <StatCard
          label="Value (Latency Impact)"
          value={
            latestPerf?.latency_delta_percent != null
              ? `${latestPerf.latency_delta_percent > 0 ? "+" : ""}${latestPerf.latency_delta_percent}%`
              : latestPerf?.status === "insufficient_samples"
              ? "Awaiting traffic"
              : "—"
          }
          icon={
            latestPerf?.latency_delta_percent != null && latestPerf.latency_delta_percent < 0
              ? TrendingDown
              : TrendingUp
          }
        />
      </div>

      <Card>
        <CardContent className="p-4">
          <div className="mb-2 flex items-center gap-2">
            <Gauge className="h-4 w-4 text-primary" />
            <h2 className="text-sm font-semibold">Right-sizing recommendation</h2>
          </div>
          {latestRec ? (
            <div className="space-y-1 text-sm">
              <div className="flex items-center gap-2">
                <Badge variant={latestRec.is_overprovisioned ? "destructive" : "success"}>
                  {latestRec.is_overprovisioned ? "Over-provisioned" : "Right-sized"}
                </Badge>
                <span className="text-code text-xs text-muted-foreground">
                  CPU efficiency {(latestRec.efficiency_cpu * 100).toFixed(0)}% · Mem efficiency{" "}
                  {(latestRec.efficiency_mem * 100).toFixed(0)}%
                </span>
              </div>
              {latestRec.is_overprovisioned && (
                <p className="text-xs text-muted-foreground">
                  Suggested:{" "}
                  {latestRec.recommended_fargate_size
                    ? `${latestRec.recommended_fargate_size.cpu_vcpu} vCPU / ${latestRec.recommended_fargate_size.mem_gib} GiB (a real Fargate size)`
                    : `${latestRec.recommended_cpu_vcpu} vCPU / ${latestRec.recommended_mem_gib} GiB`}
                  {latestRec.requested_cpu_vcpu != null &&
                    ` instead of ${latestRec.requested_cpu_vcpu} vCPU / ${latestRec.requested_mem_gib} GiB`}
                  {" "}— requires a <span className="font-medium">{latestRec.requires_approval_role}</span> approval to
                  apply (never auto-applied).
                </p>
              )}
              {latestRec.source === "cloudwatch" && (
                <p className="text-[11px] text-muted-foreground">
                  Based on {latestRec.sample_count} one-minute CloudWatch samples over{" "}
                  {Math.round((latestRec.window_seconds ?? 0) / 60)} min of <span className="text-code">{latestRec.measured_service}</span>:
                  p95 CPU {latestRec.observed_cpu_p95_percent}% · p95 memory {latestRec.observed_mem_p95_percent}%.
                </p>
              )}
              {latestRec.caveat && <p className="text-[11px] text-warning">{latestRec.caveat}</p>}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              Not enough usage data yet — a recommendation needs at least 10 one-minute CloudWatch samples of the
              running service (it appears after a service has been up and reporting for a few minutes). No number is
              shown rather than an estimated one.
            </p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-4">
          <h2 className="mb-3 text-sm font-semibold">Cost History</h2>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Computed</TableHead>
                <TableHead>Run</TableHead>
                <TableHead>Trigger</TableHead>
                <TableHead>Baseline</TableHead>
                <TableHead>Canary</TableHead>
                <TableHead>Delta</TableHead>
                <TableHead>Value Metric</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {history.map((h) => (
                <TableRow key={h.cost_id}>
                  <TableCell className="text-xs">{new Date(h.computed_at).toLocaleString()}</TableCell>
                  <TableCell className="text-code text-xs">{h.pipeline_run_id.slice(0, 8)}…</TableCell>
                  <TableCell className="text-xs">{h.trigger_type ?? "—"}</TableCell>
                  <TableCell className="text-code text-xs">${h.baseline_cost.toFixed(4)}</TableCell>
                  <TableCell className="text-code text-xs">${h.canary_cost.toFixed(4)}</TableCell>
                  <TableCell>
                    <Badge
                      variant={h.delta_percent > 15 ? "destructive" : "secondary"}
                      className="text-[10px]"
                      title={h.delta_percent === 0 ? "Identical task allocations (both 0.25 vCPU / 0.5 GiB RAM)" : undefined}
                    >
                      {h.delta_percent > 0 ? "+" : ""}
                      {h.delta_percent.toFixed(1)}%
                    </Badge>
                  </TableCell>
                  <TableCell>
                    {h.performance_correlation ? (
                      h.performance_correlation.latency_delta_percent != null ? (
                        <div className="flex flex-col text-[10px]">
                          <span className={h.performance_correlation.latency_delta_percent < 0 ? "text-emerald-500 font-medium" : "text-destructive font-medium"}>
                            Latency {h.performance_correlation.latency_delta_percent > 0 ? "+" : ""}{h.performance_correlation.latency_delta_percent}%
                          </span>
                          {h.delta_percent > 0 && h.performance_correlation.latency_delta_percent < 0 && (
                            <span className="text-muted-foreground mt-0.5">(Worth the cost)</span>
                          )}
                        </div>
                      ) : (
                        <Badge
                          variant="outline"
                          className="text-[10px] text-muted-foreground font-normal border-dashed"
                          title="Insufficient request volume during verification window (needs ≥2 samples for Mann-Whitney U test)"
                        >
                          Awaiting traffic (N &lt; 2)
                        </Badge>
                      )
                    ) : (
                      "—"
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {history.length === 0 && (
            <p className="mt-4 text-center text-sm text-muted-foreground">No cost data recorded yet for this project.</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
