/**
 * frontend/src/pages/VerificationInspector.tsx — Screen 2.
 */
import { AlertCircle, Rocket, Sparkles } from "lucide-react";
import { useVerificationResult } from "@/hooks/useVerificationResult";
import { usePipelineEvents } from "@/hooks/usePipelineEvents";
import { useAppContext } from "@/hooks/useAppContext";
import { VerdictBadge } from "@/components/verification/VerdictBadge";
import { EvidencePanel } from "@/components/verification/EvidencePanel";
import { ConfidenceGauge } from "@/components/charts/ConfidenceGauge";
import { TrafficGauge } from "@/components/pipeline/TrafficGauge";
import { TrafficWeightChart } from "@/components/pipeline/TrafficWeightChart";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { HelpTooltip } from "@/components/help-tooltip";

export function VerificationInspector() {
  const { pipelineRunId } = useAppContext();
  const { verdict, metrics, rcaReport, rcaPending, isLoading, error } = useVerificationResult(pipelineRunId);
  // Phase 4: the canary ramp is only meaningful WHILE progressive_verify is
  // the active stage — same live WebSocket state Pipeline View already
  // renders, just surfaced here too so the Inspector doesn't need a trip
  // back to the other screen to see what traffic level this verdict
  // actually corresponds to.
  const { currentStage, trafficWeight, weightHistory, status: pipelineStatus } = usePipelineEvents(pipelineRunId);

  if (!pipelineRunId) {
    return <p className="text-sm text-muted-foreground">Select a pipeline run to inspect its verdict.</p>;
  }
  if (isLoading) {
    return (
      <div className="grid gap-4 md:grid-cols-2">
        <Skeleton className="h-24" />
        <Skeleton className="h-24" />
        <Skeleton className="h-40 md:col-span-2" />
      </div>
    );
  }
  if (error) {
    if (error.status === 404) {
      // Real gap found live (2026-09-15): a project's genuinely first-ever
      // deployment intentionally never produces a verdict at all — there's
      // no prior baseline to compare against yet, so canary_loop ships
      // straight to 100% and skips statistical verification entirely (see
      // worker.py). Before this, a run that had actually COMPLETED
      // successfully showed the exact same "hasn't completed (or hasn't
      // started)" message as one still genuinely in progress — confusing
      // for the one case where "no verdict" is the CORRECT, expected
      // outcome rather than something still pending.
      if (pipelineStatus === "COMPLETED") {
        return (
          <Card className="mx-auto mt-8 max-w-lg text-center">
            <CardContent className="flex flex-col items-center gap-2 p-8 text-sm text-muted-foreground">
              <Rocket className="h-8 w-8 text-muted-foreground/60" />
              <p className="font-medium text-foreground">First deployment — no comparison to run yet</p>
              <p>
                This was the project's first-ever deployment, so there was no prior baseline version to compare
                it against. It shipped directly to 100% traffic with no canary step. Future deployments will run
                the full statistical verification and progressive traffic ramp.
              </p>
            </CardContent>
          </Card>
        );
      }
      return (
        <Card className="mx-auto mt-8 max-w-lg text-center">
          <CardContent className="flex flex-col items-center gap-2 p-8 text-sm text-muted-foreground">
            <AlertCircle className="h-8 w-8 text-muted-foreground/60" />
            No verdict published yet for this run — verification hasn't completed (or hasn't started).
          </CardContent>
        </Card>
      );
    }
    return <p className="text-sm text-destructive">Error: {error.message}</p>;
  }
  if (!verdict) return <p className="text-sm text-muted-foreground">No verification run yet.</p>;

  return (
    <div className="space-y-4">
      {currentStage === "progressive_verify" && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">
              Canary traffic ramp
              <HelpTooltip>The live traffic-weight progression for this run — this verdict was produced at the weight shown here.</HelpTooltip>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <TrafficGauge weight={trafficWeight} status={pipelineStatus} />
            <TrafficWeightChart history={weightHistory} />
          </CardContent>
        </Card>
      )}

      <Card className="overflow-hidden">
        <div className="flex flex-col gap-4 border-b bg-muted/20 p-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex items-center gap-5">
            <ConfidenceGauge confidence={verdict.confidence} />
            <div>
              <div className="flex items-center gap-2">
                <span className="text-stat text-2xl">{verdict.composite_score.toFixed(1)}</span>
                <span className="text-stat text-sm text-muted-foreground">/ 100.0</span>
                <VerdictBadge status={verdict.status} />
              </div>
              <p className="mt-1 max-w-md text-sm text-muted-foreground">
                Composite score
                <HelpTooltip>Weighted average of non-critical metrics' health (0–100). Critical-tier breaches (e.g. error rate) override this entirely with a hard FAILED — they're never diluted into an average.</HelpTooltip>{" "}
                combines all evaluated metrics; confidence
                <HelpTooltip>How much this verdict should be trusted — combines sample size vs. the N≥100 floor, variance stability between cohorts, and whether the minimum observation window has actually elapsed.</HelpTooltip>{" "}
                reflects how much this verdict should be trusted.
              </p>
              {verdict.tier1_breaches.length > 0 && (
                <p className="mt-1 text-sm font-medium text-destructive">
                  Tier-1 breaches
                  <HelpTooltip>A "tier-1" (critical) metric — like error rate — showed a statistically significant regression. This alone forces a FAILED verdict regardless of every other metric.</HelpTooltip>
                  : {verdict.tier1_breaches.join(", ")}
                </p>
              )}
            </div>
          </div>

          <div className="grid grid-cols-3 gap-4 border-t pt-3 text-code lg:border-l lg:border-t-0 lg:pl-5 lg:pt-0">
            <div>
              <span className="block text-[11px] text-muted-foreground">Verdict ID</span>
              <span className="text-xs font-semibold">{verdict.verdict_id.slice(0, 12)}…</span>
            </div>
            <div>
              <span className="block text-[11px] text-muted-foreground">Metrics evaluated</span>
              <span className="text-xs font-semibold">{Object.keys(verdict.evidence).length}</span>
            </div>
            <div>
              <span className="block text-[11px] text-muted-foreground">Sample floor</span>
              <span className="text-xs font-semibold text-primary">N ≥ 100</span>
            </div>
          </div>
        </div>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Evidence</CardTitle>
        </CardHeader>
        <CardContent>
          <EvidencePanel evidence={metrics} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-primary" />
            AI Root Cause Analysis
            <HelpTooltip>
              Generated after the platform has already acted on this verdict — this explains WHY, it never
              delays or gates the actual promote/rollback decision.
            </HelpTooltip>
          </CardTitle>
        </CardHeader>
        <CardContent>
          {rcaReport ? (
            <p className="text-sm leading-relaxed">{rcaReport}</p>
          ) : rcaPending ? (
            <p className="text-sm text-muted-foreground">Generating explanation…</p>
          ) : (
            <p className="text-sm text-muted-foreground">No explanation available for this run.</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
