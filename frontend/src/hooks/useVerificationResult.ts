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

export function useVerificationResult(pipelineRunId: string) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["verification", pipelineRunId],
    queryFn: () => apiClient.get<Verdict>(`/api/v1/verification/${pipelineRunId}`),
    enabled: Boolean(pipelineRunId),
    refetchInterval: 5000,
    retry: (failureCount, err) => (err instanceof ApiError && err.status === 404 ? false : failureCount < 3),
  });

  const { data: history } = useQuery({
    queryKey: ["verification-history", pipelineRunId],
    queryFn: () =>
      apiClient.get<{ records: VerificationHistoryRecord[] }>(`/api/v1/verification/${pipelineRunId}/history`),
    enabled: Boolean(pipelineRunId) && Boolean(data),
    // RCA generation is async and can take a few seconds (Groq call, or its
    // fallback) after the verdict itself already exists — keep polling
    // until a summary shows up, same cadence as the verdict poll above.
    refetchInterval: (query) => {
      const latest = query.state.data?.records?.[0];
      return latest?.rca_summary ? false : 5000;
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
