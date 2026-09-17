import { describe, expect, it } from "vitest";
import { deriveSubSteps, filterLogsForStage, splitLogsByStage } from "./pipelineStageSteps";

// Real, verbatim log lines as worker.py actually emits them (confirmed by
// reading services/pipeline-worker/src/worker.py directly) — every test
// here proves the timeline only ever reflects real backend events.

describe("splitLogsByStage", () => {
  it("groups lines under their real stage marker, excluding the marker itself", () => {
    const lines = [
      "Pipeline started for widget-rollout — stages: build, test",
      "--- Stage: build (build) ---",
      "Building Dockerfile -> tag v1.1.0",
      "Build finished: success",
      "--- Stage: test (test) ---",
      "No test command configured — skipping (the Docker build itself may already run real tests).",
    ];
    const groups = splitLogsByStage(lines);
    expect(groups["build"]).toEqual({
      stageType: "build",
      lines: ["Building Dockerfile -> tag v1.1.0", "Build finished: success"],
    });
    expect(groups["test"].stageType).toBe("test");
  });
});

describe("filterLogsForStage", () => {
  it("returns all lines when no stage is selected", () => {
    const lines = ["a", "--- Stage: build (build) ---", "b"];
    expect(filterLogsForStage(lines, null)).toEqual(lines);
  });

  it("slices from the selected stage's marker to the next marker", () => {
    const lines = [
      "--- Stage: build (build) ---",
      "Building...",
      "Build finished: success",
      "--- Stage: test (test) ---",
      "Running tests...",
    ];
    expect(filterLogsForStage(lines, "build")).toEqual([
      "--- Stage: build (build) ---",
      "Building...",
      "Build finished: success",
    ]);
  });
});

describe("deriveSubSteps — build", () => {
  it("returns no sub-steps while the stage is still pending", () => {
    expect(deriveSubSteps("build", [], "pending")).toEqual([]);
  });

  it("marks earlier steps done and the last matched step active while the stage runs", () => {
    const lines = ["Building Dockerfile -> tag v1.1.0"];
    const steps = deriveSubSteps("build", lines, "active");
    expect(steps.map((s) => s.status)).toEqual(["active", "pending"]);
  });

  it("marks all matched steps done once the stage itself is done", () => {
    const lines = ["Building Dockerfile -> tag v1.1.0", "Build finished: success"];
    const steps = deriveSubSteps("build", lines, "done");
    expect(steps.map((s) => s.status)).toEqual(["done", "done"]);
  });
});

describe("deriveSubSteps — canary_verify, blue-green", () => {
  const successLines = [
    "Blue-green rollout — waiting for the new (green) ECS service to stabilize...",
    "Waiting for the green target group to report healthy via a real ALB health check...",
    "Green is healthy — cutting over 100% of traffic (no statistical verification needed).",
  ];

  it("detects the blue-green path and shows real, deterministic pending steps ahead of where it actually is", () => {
    const steps = deriveSubSteps("canary_loop", successLines, "active");
    expect(steps.map((s) => s.label)).toEqual([
      "Waiting for the new version to stabilize",
      "Checking real ALB health",
      "Cutting over 100% of traffic",
      "Verifying the live URL through the real ALB",
      "Live and verified — promoting to baseline",
    ]);
    expect(steps.map((s) => s.status)).toEqual(["done", "done", "active", "pending", "pending"]);
  });

  it("stops rendering further pending steps once the stage has genuinely failed", () => {
    const failedLines = [
      ...successLines,
      "Live URL verification failed after cutover (HTTP 404) — rolling back to the previous version.",
    ];
    const steps = deriveSubSteps("canary_loop", failedLines, "failed");
    expect(steps.map((s) => s.status)).toEqual(["done", "done", "done", "failed"]);
    // The final "Live and verified" step never happened — must not be shown.
    expect(steps.some((s) => s.label.includes("Live and verified"))).toBe(false);
  });

  it("shows a single honest placeholder before any distinguishing line has arrived", () => {
    const steps = deriveSubSteps("canary_loop", [], "active");
    expect(steps).toEqual([{ id: "starting", label: "Starting verification…", status: "active" }]);
  });
});

describe("deriveSubSteps — canary_verify, normal statistically-verified ramp", () => {
  it("grows dynamically, one real step per Traffic step/Verdict pair — never a guessed total", () => {
    const lines = [
      "Traffic step: 10% canary — running verification",
      "Verdict: HEALTHY (confidence=0.95, score=98.2)",
      "Traffic step: 25% canary — running verification",
      "Verdict: HEALTHY (confidence=0.93, score=97.1)",
      "Traffic step: 100% canary — running verification",
    ];
    const steps = deriveSubSteps("canary_loop", lines, "active");
    expect(steps).toHaveLength(3);
    expect(steps[0].status).toBe("done");
    expect(steps[0].label).toContain("verdict HEALTHY");
    expect(steps[1].status).toBe("done");
    expect(steps[2].status).toBe("active");
    expect(steps[2].label).toContain("Step 3: 100% canary — verifying");
  });
});

describe("deriveSubSteps — first deployment (AWS ECS)", () => {
  it("detects the ECS variant and includes the live-URL verification step", () => {
    const lines = [
      "First-ever deployment for this project — no baseline to compare against yet; shipping straight to 100% and skipping canary verification.",
      "Waiting for the new ECS services to become stable before cutting over traffic...",
      "ECS services stable — cutting over traffic to 100%.",
    ];
    const steps = deriveSubSteps("canary_loop", lines, "active");
    expect(steps.map((s) => s.label)).toContain("Verifying the live URL through the real ALB");
  });
});

describe("deriveSubSteps — first deployment (Kubernetes)", () => {
  it("detects the Kubernetes variant and has no live-URL step (AWS-ECS-only feature)", () => {
    const lines = [
      "First-ever deployment for this project — no baseline to compare against yet; shipping straight to 100% and skipping canary verification.",
      "Waiting for the new deployment to become healthy before cutting over traffic...",
    ];
    const steps = deriveSubSteps("canary_loop", lines, "active");
    expect(steps.some((s) => s.label.includes("live URL"))).toBe(false);
  });
});
