/**
 * Renders per-metric evidence from a verdict. Routes each metric to a real
 * chart when its shape matches a known statistical test (Mann-Whitney,
 * Wald SPRT); falls back to a readable key/value card for anything else
 * (business-metric contingency tables, saturation CUSUM/BOCPD) rather than
 * a raw JSON dump.
 */
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { LatencyComparisonChart, type MannWhitneyResult } from "@/components/charts/LatencyComparisonChart";
import { SprtBoundaryChart, type SprtResult } from "@/components/charts/SprtBoundaryChart";

function isMannWhitneyEvidence(v: unknown): v is { mann_whitney: MannWhitneyResult } {
  return typeof v === "object" && v !== null && "mann_whitney" in v;
}

function isSprtEvidence(v: unknown): v is SprtResult {
  return typeof v === "object" && v !== null && (v as { test?: string }).test === "Wald SPRT";
}

function GenericMetricCard({ metricName, result }: { metricName: string; result: unknown }) {
  if (typeof result !== "object" || result === null) {
    return (
      <Card>
        <CardContent className="p-3 text-xs">
          <span className="font-medium">{metricName}</span>: {String(result)}
        </CardContent>
      </Card>
    );
  }
  const entries = Object.entries(result as Record<string, unknown>).filter(
    ([k]) => !["test", "contingency_table"].includes(k)
  );
  const isBreach = (result as { is_actionable_regression?: boolean }).is_actionable_regression;

  return (
    <Card>
      <CardContent className="p-3">
        <div className="mb-1.5 flex items-center justify-between">
          <span className="text-xs font-medium">{metricName}</span>
          {isBreach !== undefined && (
            <Badge variant={isBreach ? "destructive" : "success"}>{isBreach ? "regression" : "healthy"}</Badge>
          )}
        </div>
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          {entries.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="truncate">{k}</dt>
              <dd className="truncate text-right font-mono text-foreground">
                {typeof v === "number" ? v.toFixed(4) : typeof v === "object" ? JSON.stringify(v) : String(v)}
              </dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  );
}

export function EvidencePanel({ evidence }: { evidence: Record<string, unknown> }) {
  const entries = Object.entries(evidence ?? {});
  if (entries.length === 0) {
    return <p className="text-sm text-muted-foreground">No metric evidence recorded for this run.</p>;
  }

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-semibold">Metric Evidence</h3>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {entries.map(([metricName, result]) => {
          if (isMannWhitneyEvidence(result)) {
            return (
              <Card key={metricName}>
                <CardContent className="p-3">
                  <LatencyComparisonChart metricName={metricName} result={result.mann_whitney} />
                </CardContent>
              </Card>
            );
          }
          if (isSprtEvidence(result)) {
            return (
              <Card key={metricName}>
                <CardContent className="p-3">
                  <SprtBoundaryChart metricName={metricName} result={result} />
                </CardContent>
              </Card>
            );
          }
          return <GenericMetricCard key={metricName} metricName={metricName} result={result} />;
        })}
      </div>
    </div>
  );
}
