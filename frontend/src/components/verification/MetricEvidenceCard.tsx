/**
 * frontend/src/components/verification/MetricEvidenceCard.tsx
 *
 * Reports tab redesign (2026-09-28) — real gap: a metric's evidence dict was dumped as raw JSON
 * (`<pre>{JSON.stringify(...)}</pre>`), which is correct data but unreadable to anyone who isn't the
 * engineer who wrote the statistical test. Every category's evidence dict has DIFFERENT field names by
 * design (tests_statistical/*.py each return their own test's real output, never forced into one shape) —
 * so this renders generically: reuse the existing LatencyComparisonChart when a metric has real
 * baseline/canary sample stats (Mann-Whitney), and a clean, labeled key-value grid otherwise (SPRT's
 * sequential decision, CUSUM/BOCPD change-point state, business-metric contingency stats) — never invents
 * a chart shape the backend didn't actually produce.
 */
import { CheckCircle2, MinusCircle, XCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { LatencyComparisonChart, type MannWhitneyResult } from "@/components/charts/LatencyComparisonChart";
import type { MetricEvidenceEntry } from "@/api/reports";

const LABEL_OVERRIDES: Record<string, string> = {
  p_value: "p-value",
  n_baseline: "Baseline samples",
  n_canary: "Canary samples",
  is_significant: "Statistically significant",
  is_actionable_regression: "Actionable regression",
  effect_size_cles: "Effect size (CLES)",
  z_statistic: "Z-statistic",
  log_likelihood_ratio: "Log-likelihood ratio",
  total_requests: "Requests observed",
  total_errors: "Errors observed",
  baseline_mean: "Baseline mean",
  canary_mean: "Canary mean",
  baseline_median: "Baseline median",
  canary_median: "Canary median",
};

function humanizeKey(key: string): string {
  return LABEL_OVERRIDES[key] ?? key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatValue(value: unknown): string {
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return `${value.length} point(s)`;
  return String(value);
}

const DECISION_BADGE: Record<string, { icon: typeof CheckCircle2; className: string }> = {
  ACCEPT_H0: { icon: CheckCircle2, className: "text-success border-success/40 bg-success/10" },
  REJECT_H0: { icon: XCircle, className: "text-destructive border-destructive/40 bg-destructive/10" },
  CONTINUE: { icon: MinusCircle, className: "text-muted-foreground border-border" },
};

function isMannWhitneyShape(e: MetricEvidenceEntry): e is MetricEvidenceEntry & MannWhitneyResult {
  return typeof e.baseline_median === "number" && typeof e.canary_median === "number" && typeof e.p_value === "number";
}

export function MetricEvidenceCard({ metricName, evidence }: { metricName: string; evidence: MetricEvidenceEntry }) {
  if (isMannWhitneyShape(evidence)) {
    return (
      <div className="rounded-md border p-3">
        <LatencyComparisonChart metricName={metricName} result={evidence} />
      </div>
    );
  }

  const decision = typeof evidence.decision === "string" ? evidence.decision : null;
  const testName = typeof evidence.test === "string" ? evidence.test : null;
  const entries = Object.entries(evidence).filter(
    ([k, v]) => k !== "test" && k !== "decision" && v !== null && v !== undefined && !Array.isArray(v),
  );

  return (
    <div className="rounded-md border p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="text-sm font-medium">{metricName}</span>
        <div className="flex items-center gap-2">
          {testName && <span className="text-[11px] text-muted-foreground">{testName}</span>}
          {decision &&
            (() => {
              const style = DECISION_BADGE[decision] ?? DECISION_BADGE.CONTINUE;
              const Icon = style.icon;
              return (
                <Badge variant="outline" className={`gap-1 text-[10px] ${style.className}`}>
                  <Icon className="h-3 w-3" />
                  {decision.replace(/_/g, " ")}
                </Badge>
              );
            })()}
        </div>
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3">
        {entries.map(([key, value]) => (
          <div key={key}>
            <dt className="text-[10px] uppercase tracking-wide text-muted-foreground">{humanizeKey(key)}</dt>
            <dd className="text-code text-xs">{formatValue(value)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
