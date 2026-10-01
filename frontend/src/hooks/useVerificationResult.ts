/**
 * frontend/src/hooks/useVerificationResult.ts
 *
 * Polls api-gateway's verification_router for the latest verdict + evidence
 * for Screen 2 (Verification Detail View).
 *
 * `rcaReport` (the plain-language "why") is fetched from the durable
 * `/history` endpoint, not the live verdict poll above — the RCA is
 * generated asynchronously by policy-controller AFTER it finishes acting
 * on the verdict (explainability-service's own docs are explicit that RCA
 * generation must never delay the actual safety decision), so it only
 * exists in Postgres a moment after the verdict itself does. Before this,
 * `rcaReport` was hard-coded to `undefined` — nothing ever fetched it, even
 * though the backend could generate one — found live when a user asked how
 * they'd ever find out what happened to their code.
 */
import { useQuery } from "@tanstack/react-query";
import { apiClient, ApiError } from "@/api/client";

export interface VerdictEvidence {
  [metricName: string]: unknown;
}

export interface Verdict {
  verdict_id: string;
  status: "HEALTHY" | "DEGRADED" | "FAILED" | "UNVERIFIABLE";
  composite_score: number;
  confidence: number;
  evidence: VerdictEvidence;
  tier1_breaches: string[];
}

interface VerificationHistoryRecord {
  verdict_id: string;
  rca_summary: string | null;
  timestamp_utc: string;
}

// Real bug found live: `refetchInterval: 5000` with no stop condition polled
// FOREVER for as long as the tab stayed open — including a blue-green run,
// which never produces a verdict at all (a permanent 404, see
// VerificationInspector.tsx's dedicated branch for it) and a long-finished
// canary run whose verdict will never change again. `retry` only bounds
// retries WITHIN one fetch attempt; it does nothing to stop `refetchInterval`
// from firing a brand new attempt every 5s indefinitely. Visible as the
// Verification Inspector tab continuously re-rendering/flickering the longer
// it stayed open — capped here at a definitive error (never changes without
// switching runs) or ~2.5 minutes of unchanging data (long enough to watch a
// live rollout settle, not long enough to hammer the backend for a tab left
// open on an old run overnight).
const MAX_POLL_ATTEMPTS = 30;

export function useVerificationResult(pipelineRunId: string) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["verification", pipelineRunId],
    queryFn: () => apiClient.get<Verdict>(`/api/v1/verification/${pipelineRunId}`),
    enabled: Boolean(pipelineRunId),
    refetchInterval: (query) =>
      query.state.status === "error" || query.state.dataUpdateCount >= MAX_POLL_ATTEMPTS ? false : 5000,
    retry: (failureCount, err) => (err instanceof ApiError && err.status === 404 ? false : failureCount < 3),
  });

  const { data: history } = useQuery({
    queryKey: ["verification-history", pipelineRunId],
    queryFn: () =>
      apiClient.get<{ records: VerificationHistoryRecord[] }>(`/api/v1/verification/${pipelineRunId}/history`),
    enabled: Boolean(pipelineRunId) && Boolean(data),
    // RCA generation is async and can take a few seconds (Groq call, or its
    // fallback) after the verdict itself already exists — keep polling
    // until a summary shows up, same cadence as the verdict poll above, but
    // give up after the same bound rather than polling forever if
    // explainability-service never produces one (e.g. it's down).
    refetchInterval: (query) => {
      const latest = query.state.data?.records?.[0];
      if (latest?.rca_summary) return false;
      if (query.state.dataUpdateCount >= MAX_POLL_ATTEMPTS) return false;
      return 5000;
    },
  });

  const latestRecord = history?.records?.[0];

  return {
    verdict: data,
    metrics: data?.evidence ?? {},
    rcaReport: latestRecord?.rca_summary ?? undefined,
    rcaPending: Boolean(data) && !latestRecord?.rca_summary,
    isLoading,
    error: error as ApiError | null,
  };
}
