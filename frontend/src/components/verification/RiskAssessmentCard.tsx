/**
 * frontend/src/components/verification/RiskAssessmentCard.tsx
 *
 * AI Deployment Risk Assessment — new, unique feature (2026-09-29). Real gap found while researching what
 * would actually help here (not a re-skin of ChatOps or Log Hygiene, both of which already existed): the
 * backend's predictive-risk scorer (services/explainability-service/src/predictive_risk_scorer.py) has
 * existed since an earlier phase — a real Groq-backed model that reads a commit diff and flags auth/
 * database/payment/infra touchpoints, scores risk 0-100, and recommends an adaptive canary ramp — but
 * NOTHING in the frontend ever called it, and no code anywhere fetched a real commit diff to feed it. It
 * was fully built and never exercised against real data.
 *
 * This closes that gap for one specific, already-shipped run: api/projects.ts's getRunRiskAssessment fetches
 * the REAL GitHub diff between this run's commit and the previously-deployed one
 * (github_router.compare_commits), then feeds it to the same unmodified scorer. Every number and file name
 * shown here is real GitHub/model output — an `available: false` response always carries an honest reason
 * (no repo connected, no commit recorded, first tracked deploy, same commit redeployed, or the scorer being
 * unreachable) rather than a fabricated score.
 */
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ExternalLink, ListChecks, Loader2, ShieldQuestion, Sparkles } from "lucide-react";
import { getRunRiskAssessment } from "@/api/projects";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

const LEVEL_STYLE: Record<string, { badge: string; ring: string }> = {
  LOW: { badge: "bg-success/15 text-success border-success/30", ring: "ring-success/20" },
  MEDIUM: { badge: "bg-warning/15 text-warning border-warning/30", ring: "ring-warning/20" },
  HIGH: { badge: "bg-destructive/15 text-destructive border-destructive/30", ring: "ring-destructive/20" },
  CRITICAL: { badge: "bg-destructive/25 text-destructive border-destructive/40", ring: "ring-destructive/30" },
};

const SEVERITY_DOT: Record<string, string> = {
  LOW: "bg-muted-foreground",
  MEDIUM: "bg-warning",
  HIGH: "bg-destructive",
};

export function RiskAssessmentCard({ projectId, runId }: { projectId: string; runId: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["run-risk-assessment", projectId, runId],
    queryFn: () => getRunRiskAssessment(projectId, runId),
    enabled: Boolean(projectId && runId),
    staleTime: Infinity,
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-primary" />
          AI Deployment Risk Assessment
        </CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Diffing this deploy against the previous one and
            scoring risk…
          </div>
        ) : !data || !data.available ? (
          <div className="flex items-start gap-2 text-sm text-muted-foreground">
            <ShieldQuestion className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground/60" />
            <p>{data?.message ?? "Risk assessment isn't available for this run."}</p>
          </div>
        ) : (
          <div className="space-y-4">
            <div className={`flex flex-wrap items-center gap-3 rounded-md border p-3 ring-1 ${LEVEL_STYLE[data.risk.risk_level]?.ring ?? "ring-border"}`}>
              <div className="text-stat text-3xl font-semibold">{data.risk.risk_score}<span className="text-sm text-muted-foreground">/100</span></div>
              <Badge variant="outline" className={LEVEL_STYLE[data.risk.risk_level]?.badge}>
                {data.risk.risk_level} RISK
              </Badge>
              <p className="min-w-[200px] flex-1 text-sm text-muted-foreground">{data.risk.summary}</p>
            </div>

            <div className="text-[11px] text-muted-foreground">
              Diffed <span className="text-code text-foreground">{data.base_sha.slice(0, 7)}</span> →{" "}
              <span className="text-code text-foreground">{data.head_sha.slice(0, 7)}</span> ·{" "}
              {data.stats.changed_files} file(s) ·{" "}
              <span className="text-success">+{data.stats.additions}</span> /{" "}
              <span className="text-destructive">-{data.stats.deletions}</span>
              {data.compare_url && (
                <a href={data.compare_url} target="_blank" rel="noreferrer" className="ml-1.5 inline-flex items-center gap-0.5 hover:text-primary hover:underline">
                  view diff on GitHub <ExternalLink className="h-2.5 w-2.5" />
                </a>
              )}
            </div>

            {data.risk.risk_factors.length > 0 && (
              <div>
                <p className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                  <AlertTriangle className="h-3 w-3" /> Risk factors
                </p>
                <ul className="space-y-1">
                  {data.risk.risk_factors.map((f, i) => (
                    <li key={i} className="flex items-start gap-2 text-sm">
                      <span className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${SEVERITY_DOT[f.severity] ?? "bg-muted-foreground"}`} />
                      <span><span className="font-medium">{f.category}:</span> {f.description}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {data.risk.prescriptive_pre_deploy_checks.length > 0 && (
              <div>
                <p className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                  <ListChecks className="h-3 w-3" /> Suggested checks before shipping something like this again
                </p>
                <ul className="list-inside list-disc space-y-0.5 text-sm text-muted-foreground">
                  {data.risk.prescriptive_pre_deploy_checks.map((c, i) => (
                    <li key={i}>{c}</li>
                  ))}
                </ul>
              </div>
            )}

            <details className="text-sm">
              <summary className="cursor-pointer text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                {data.commits.length} real commit(s) in this deploy
              </summary>
              <ul className="mt-1.5 space-y-1">
                {data.commits.map((c) => (
                  <li key={c.sha} className="flex items-center gap-2 text-xs">
                    <span className="text-code text-muted-foreground">{c.short_sha}</span>
                    <span className="truncate">{c.message}</span>
                    {c.author && <span className="shrink-0 text-muted-foreground">— {c.author}</span>}
                    {c.url && (
                      <a href={c.url} target="_blank" rel="noreferrer" className="shrink-0 text-muted-foreground hover:text-primary">
                        <ExternalLink className="h-2.5 w-2.5" />
                      </a>
                    )}
                  </li>
                ))}
              </ul>
            </details>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
