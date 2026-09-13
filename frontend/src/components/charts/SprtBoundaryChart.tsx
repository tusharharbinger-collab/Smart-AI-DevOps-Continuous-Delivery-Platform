export interface SprtResult {
  log_likelihood_ratio: number;
  thresholds: [number, number]; // [A (reject/upper), B (accept/lower)]
  decision: "REJECT_H0" | "ACCEPT_H0" | "CONTINUE";
  total_requests: number;
  total_errors: number;
}

/**
 * Shows where the SPRT's final log-likelihood ratio landed relative to its
 * accept (B) / reject (A) boundaries. This is a snapshot of the final LLR,
 * not the step-by-step trajectory a true "likelihood curve" would plot —
 * the backend's /verify evidence only returns the final LLR value, not the
 * observation-by-observation history. Plotting the real trajectory needs
 * the backend to return that series — a follow-up, not a frontend-only change.
 */
export function SprtBoundaryChart({ metricName, result }: { metricName: string; result: SprtResult }) {
  const [A, B] = result.thresholds;
  const span = A - B;
  const clamped = Math.max(B, Math.min(A, result.log_likelihood_ratio));
  const percent = ((clamped - B) / span) * 100;

  const color =
    result.decision === "REJECT_H0" ? "bg-destructive" : result.decision === "ACCEPT_H0" ? "bg-success" : "bg-primary";

  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between text-xs text-muted-foreground">
        <span className="font-medium text-foreground">{metricName}</span>
        <span>
          {result.total_errors}/{result.total_requests} errors, LLR={result.log_likelihood_ratio.toFixed(2)}
        </span>
      </div>
      <div className="relative h-8 rounded-md bg-muted">
        <div className="absolute inset-y-0 left-0 flex items-center pl-1.5 text-[10px] text-muted-foreground">
          B={B.toFixed(2)}
        </div>
        <div className="absolute inset-y-0 right-0 flex items-center pr-1.5 text-[10px] text-muted-foreground">
          A={A.toFixed(2)}
        </div>
        <div
          className={`absolute inset-y-0 w-1 rounded-full ${color} transition-all duration-500`}
          style={{ left: `calc(${percent}% - 2px)` }}
          title={`LLR = ${result.log_likelihood_ratio.toFixed(2)}`}
        />
      </div>
      <p className="mt-1 text-[11px] text-muted-foreground">
        {result.decision === "REJECT_H0" && "LLR crossed the reject boundary — statistically significant regression."}
        {result.decision === "ACCEPT_H0" && "LLR crossed the accept boundary — statistically confirmed healthy."}
        {result.decision === "CONTINUE" && "Within bounds — not enough evidence yet to decide either way."}
      </p>
    </div>
  );
}
