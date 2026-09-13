import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

export interface MannWhitneyResult {
  baseline_median: number;
  canary_median: number;
  p_value: number;
  effect_size_cles: number;
  is_significant?: boolean;
}

/**
 * Compares baseline vs. canary latency medians. This is a summary
 * comparison, not a true empirical CDF — the backend's /verify evidence
 * only ever returns the Mann-Whitney U test's summary statistics (medians,
 * p-value, effect size), not the raw per-request sample arrays a real ECDF
 * needs. A true ECDF chart is a backend evidence-payload change, not a
 * frontend one — see docs/roadmap/01-frontend-overhaul.md.
 */
export function LatencyComparisonChart({ metricName, result }: { metricName: string; result: MannWhitneyResult }) {
  const data = [
    { name: "Baseline", ms: Math.round(result.baseline_median * 1000) },
    { name: "Canary", ms: Math.round(result.canary_median * 1000) },
  ];
  const regressed = result.effect_size_cles >= 0.65 && result.is_significant !== false && result.p_value < 0.05;

  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between text-xs text-muted-foreground">
        <span className="font-medium text-foreground">{metricName}</span>
        <span>Mann-Whitney p={result.p_value.toFixed(4)}, CLES={result.effect_size_cles.toFixed(3)}</span>
      </div>
      <ResponsiveContainer width="100%" height={120}>
        <BarChart data={data} layout="vertical" margin={{ left: 8, right: 16 }}>
          <CartesianGrid strokeDasharray="3 3" horizontal={false} className="stroke-border" />
          <XAxis type="number" unit="ms" tick={{ fontSize: 10 }} />
          <YAxis type="category" dataKey="name" tick={{ fontSize: 11 }} width={56} />
          <Tooltip formatter={(v: number) => [`${v}ms`, "median"]} contentStyle={{ fontSize: 12, borderRadius: 6 }} />
          <Bar dataKey="ms" radius={[0, 4, 4, 0]}>
            {data.map((d, i) => (
              <Cell key={d.name} fill={i === 1 && regressed ? "hsl(var(--destructive))" : "hsl(var(--primary))"} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
