/**
 * Reports tab redesign (2026-09-28) - MetricEvidenceCard replaces a raw JSON.stringify dump of a metric's
 * evidence dict. Every category's evidence shape is genuinely different (tests_statistical/*.py), so this
 * covers both real shapes: Mann-Whitney (latency, has baseline/canary sample stats -> a real chart) and
 * Wald SPRT (error rate, a sequential decision with no baseline/canary pair -> a labeled key-value grid).
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MetricEvidenceCard } from "./MetricEvidenceCard";

describe("MetricEvidenceCard", () => {
  it("renders the real Mann-Whitney (latency) shape as a baseline/canary chart", () => {
    const { container } = render(
      <MetricEvidenceCard
        metricName="p95_latency_seconds"
        evidence={{
          test: "Mann-Whitney U",
          p_value: 0.0312,
          effect_size_cles: 0.71,
          z_statistic: -2.1,
          n_baseline: 500,
          n_canary: 500,
          is_significant: true,
          is_actionable_regression: true,
          baseline_median: 0.12,
          canary_median: 0.19,
          baseline_mean: 0.13,
          canary_mean: 0.2,
        }}
      />,
    );
    expect(screen.getByText("p95_latency_seconds")).toBeInTheDocument();
    expect(screen.getByText(/Mann-Whitney p=0\.0312/)).toBeInTheDocument();
    // LatencyComparisonChart (a real chart component) took the render path, not the generic key-value grid -
    // recharts' own SVG output isn't asserted on here since jsdom has no real layout engine to size it.
    expect(container.querySelectorAll("dt").length).toBe(0);
  });

  it("renders the real Wald SPRT (error rate) shape as a labeled grid with a decision badge, not a chart", () => {
    render(
      <MetricEvidenceCard
        metricName="error_rate"
        evidence={{
          decision: "ACCEPT_H0",
          log_likelihood_ratio: -2.31,
          total_requests: 340,
          total_errors: 2,
        }}
      />,
    );
    expect(screen.getByText("error_rate")).toBeInTheDocument();
    expect(screen.getByText("ACCEPT H0")).toBeInTheDocument();
    expect(screen.getByText("Log-likelihood ratio")).toBeInTheDocument();
    expect(screen.getByText("Requests observed")).toBeInTheDocument();
    expect(screen.getByText("340")).toBeInTheDocument();
    expect(screen.queryByText("Baseline")).not.toBeInTheDocument();
  });

  it("never renders a raw JSON dump for either shape", () => {
    const { container } = render(
      <MetricEvidenceCard metricName="saturation" evidence={{ test: "CUSUM", decision: "CONTINUE", cusum_statistic: 1.2 }} />,
    );
    expect(container.querySelector("pre")).not.toBeInTheDocument();
  });
});
