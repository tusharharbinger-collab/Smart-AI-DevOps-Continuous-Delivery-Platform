/**
 * Real bug found live: this card called a bare `fetch("/api/v1/...")` (a relative path, so it hit the Vite dev
 * server instead of the api-gateway, with no auth header either) and expected response fields
 * (`file`, per-issue `estimated_monthly_cost_usd`, `total_log_statements_found`) that explainability-service's
 * real LogHygieneReport schema never sends - every scan either 404'd silently or crashed the render the moment
 * a real report set state. These tests exercise the fixed contract against the REAL backend field shapes
 * (log_hygiene_analyzer.py: file_path, no per-issue cost, no total_log_statements_found).
 */
import type { ComponentProps } from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { LogHygieneCard, type LogHygieneReport } from "./LogHygieneCard";

vi.mock("@/api/client", () => ({ apiClient: { post: vi.fn() } }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

// The scan result is now kept in react-query's cache (keyed by projectId) so it survives a tab
// unmount/remount, rather than plain useState — every render below needs a real QueryClient in the tree.
function renderCard(props: ComponentProps<typeof LogHygieneCard>) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LogHygieneCard {...props} />
    </QueryClientProvider>
  );
}

const REAL_SHAPE_REPORT: LogHygieneReport = {
  summary: "The audit uncovered two console.log statements.",
  detected_issues: [
    {
      file_path: "src/index.js",
      line_number: 3,
      statement: "console.log('App starting on port ' + process.env.PORT);",
      issue_type: "DEBUG_NOISE",
      reason: "Generic startup log emitted via console.log.",
      suggested_fix: "Replace with logger.info('App starting', { port: process.env.PORT }).",
    },
    {
      file_path: "src/index.js",
      line_number: 5,
      statement: "console.log('Health check received', req.ip);",
      issue_type: "SENSITIVE_LEAK_RISK",
      reason: "Logs client IP, potential PII.",
      suggested_fix: "Mask or omit req.ip before logging, e.g. logger.info('Health check received').",
    },
  ],
  estimated_monthly_savings_usd: 8,
  recommended_best_practices: ["Enable ESLint rule \"no-console\"."],
  suggested_patch: "--- a/src/index.js\n+++ b/src/index.js\n@@\n-console.log('x');\n+logger.info('x');\n",
};

beforeEach(() => {
  vi.mocked(apiClient.post).mockReset();
});

describe("LogHygieneCard", () => {
  it("calls apiClient.post (real base URL + auth), never a bare relative fetch", async () => {
    vi.mocked(apiClient.post).mockResolvedValue(REAL_SHAPE_REPORT);
    renderCard({ projectId: "proj-1" });
    fireEvent.click(screen.getByText("Run Hygiene Scan Now"));
    await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith(
      "/api/v1/projects/proj-1/log-hygiene",
      { code_files: {}, cloudwatch_logs: [] },
    ));
  });

  it("renders a real-shaped report without crashing, using file_path and no per-issue cost", async () => {
    vi.mocked(apiClient.post).mockResolvedValue(REAL_SHAPE_REPORT);
    renderCard({ projectId: "proj-1" });
    fireEvent.click(screen.getByText("Run Hygiene Scan Now"));

    expect(await screen.findByText(/src\/index\.js:3/)).toBeInTheDocument();
    expect(screen.getByText(/src\/index\.js:5/)).toBeInTheDocument();
    expect(screen.getByText("$8.00/mo")).toBeInTheDocument();
    // Log Statements KPI is derived from detected_issues.length (the real backend has no total_log_statements_found).
    expect(screen.getByText("Detected Statements (2)")).toBeInTheDocument();
    expect(screen.getByText(REAL_SHAPE_REPORT.summary)).toBeInTheDocument();
  });

  it("counts SENSITIVE_LEAK_RISK issues as security leaks", async () => {
    vi.mocked(apiClient.post).mockResolvedValue(REAL_SHAPE_REPORT);
    renderCard({ projectId: "proj-1" });
    fireEvent.click(screen.getByText("Run Hygiene Scan Now"));
    await screen.findByText(/src\/index\.js:3/);
    expect(screen.getByText("Security Warning: Sensitive Data Logged to CloudWatch")).toBeInTheDocument();
  });

  it("shows an error toast and stays in the empty state when the API call fails", async () => {
    vi.mocked(apiClient.post).mockRejectedValue(new Error("API POST /api/v1/projects/proj-1/log-hygiene failed: 502"));
    renderCard({ projectId: "proj-1" });
    fireEvent.click(screen.getByText("Run Hygiene Scan Now"));
    await waitFor(() => expect(screen.getByText("No Hygiene Scan Run Yet")).toBeInTheDocument());
  });

  it("shows a concrete, per-issue suggested fix for every detected issue, not just the combined patch", async () => {
    vi.mocked(apiClient.post).mockResolvedValue(REAL_SHAPE_REPORT);
    renderCard({ projectId: "proj-1" });
    fireEvent.click(screen.getByText("Run Hygiene Scan Now"));

    expect(await screen.findByText(/Replace with logger\.info\('App starting'/)).toBeInTheDocument();
    expect(screen.getByText(/Mask or omit req\.ip before logging/)).toBeInTheDocument();
  });

  it("renders without crashing when an issue has no suggested_fix (an older/fallback report shape)", async () => {
    vi.mocked(apiClient.post).mockResolvedValue({
      ...REAL_SHAPE_REPORT,
      detected_issues: [{ ...REAL_SHAPE_REPORT.detected_issues[0], suggested_fix: undefined }],
    });
    renderCard({ projectId: "proj-1" });
    fireEvent.click(screen.getByText("Run Hygiene Scan Now"));
    expect(await screen.findByText(/src\/index\.js:3/)).toBeInTheDocument();
    expect(screen.queryByText("Suggested fix:")).not.toBeInTheDocument();
  });

  it("handles a null line_number without crashing (a leak with no exact source line)", async () => {
    vi.mocked(apiClient.post).mockResolvedValue({
      ...REAL_SHAPE_REPORT,
      detected_issues: [{ ...REAL_SHAPE_REPORT.detected_issues[0], line_number: null }],
    });
    renderCard({ projectId: "proj-1" });
    fireEvent.click(screen.getByText("Run Hygiene Scan Now"));
    expect(await screen.findByText("src/index.js")).toBeInTheDocument();
  });
});
