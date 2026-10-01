/**
 * AI Deployment Risk Assessment (2026-09-29) — the new feature wired into a real, previously-unused backend
 * scorer (predictive_risk_scorer.py). These tests cover the frontend contract against the real
 * `RunRiskAssessment` shape api/projects.ts declares: an honest `available: false` + reason/message for
 * every case where a real diff genuinely can't be computed, and the full risk report render when it can.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { RiskAssessmentCard } from "./RiskAssessmentCard";
import type { RunRiskAssessment } from "@/api/projects";

vi.mock("@/api/client", () => ({ apiClient: { get: vi.fn() } }));

function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

const AVAILABLE_RESULT: RunRiskAssessment = {
  available: true,
  base_sha: "prevsha0000000",
  head_sha: "headsha1234567",
  compare_url: "https://github.com/acme/widget/compare/prevsha0000000...headsha1234567",
  stats: { additions: 40, deletions: 2, changed_files: 1 },
  commits: [
    {
      sha: "headsha1234567",
      short_sha: "headsha",
      message: "Add payment retry logic",
      author: "Jane",
      date: "2026-09-29T00:00:00Z",
      url: "https://github.com/acme/widget/commit/headsha1234567",
    },
  ],
  risk: {
    risk_score: 62,
    risk_level: "MEDIUM",
    summary: "Payment retry logic touched — moderate risk.",
    risk_factors: [{ category: "Payments", description: "Payment retry logic modified", severity: "MEDIUM" }],
    recommended_canary_steps: [{ weight: 10, min_duration_seconds: 120 }],
    prescriptive_pre_deploy_checks: ["Verify idempotency of retried payment calls."],
  },
};

describe("RiskAssessmentCard", () => {
  it("renders the real risk score, level, factors and diff stats when available", async () => {
    vi.mocked(apiClient.get).mockResolvedValue(AVAILABLE_RESULT);
    renderWithQuery(<RiskAssessmentCard projectId="proj-1" runId="run-1" />);

    expect(apiClient.get).toHaveBeenCalledWith("/api/v1/projects/proj-1/runs/run-1/risk-assessment");
    expect(await screen.findByText("62")).toBeInTheDocument();
    expect(screen.getByText("MEDIUM RISK")).toBeInTheDocument();
    expect(screen.getByText(/Payment retry logic touched/)).toBeInTheDocument();
    expect(screen.getByText(/Payment retry logic modified/)).toBeInTheDocument();
    expect(screen.getByText(/Verify idempotency of retried payment calls\./)).toBeInTheDocument();
    expect(screen.getByText(/1 file\(s\)/)).toBeInTheDocument();
  });

  it("shows the real, honest reason instead of a fabricated score when unavailable", async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      available: false,
      reason: "first_tracked_deploy",
      message: "This is the earliest tracked deployment with a recorded commit for this project — there's no prior version to diff it against.",
    } satisfies RunRiskAssessment);
    renderWithQuery(<RiskAssessmentCard projectId="proj-1" runId="run-1" />);

    expect(await screen.findByText(/earliest tracked deployment/)).toBeInTheDocument();
    expect(screen.queryByText("MEDIUM RISK")).not.toBeInTheDocument();
  });
});
