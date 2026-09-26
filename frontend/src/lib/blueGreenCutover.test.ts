import { describe, expect, it } from "vitest";
import { deriveBlueGreenCutover } from "@/lib/blueGreenCutover";

// The exact lines services/pipeline-worker/src/worker.py emits in its blue-green branch, in order.
const L = {
  started: "Blue-green rollout — waiting for the new (green) ECS service to stabilize...",
  health: "Waiting for the green target group to report healthy via a real ALB health check...",
  switchStart: "Green is healthy — cutting over 100% of traffic (no statistical verification needed).",
  switched:
    "Cutover complete — 100% of traffic is now on green; the previous (blue) version is still running, so rollback is instant.",
  verifyFailed: "Live URL verification failed after cutover (HTTP 404) — rolling back to the previous version.",
  rolledBack: "Rolled back — 100% of traffic is back on the previous (blue) version.",
  verified: "Live URL verified — graduating: promoting the new image onto baseline.",
  graduated:
    "Graduation complete — baseline now runs the new version, traffic is back on baseline, and green has been scaled to zero.",
  complete: "Blue-green rollout complete — live and verified.",
};

const statuses = (lines: string[], run?: string) => deriveBlueGreenCutover(lines, run).phases.map((p) => p.status);

describe("deriveBlueGreenCutover", () => {
  it("is waiting, all pending, with blue carrying 100%, before any blue-green log line arrives", () => {
    const s = deriveBlueGreenCutover([]);
    expect(s.outcome).toBe("waiting");
    expect(s.phases.map((p) => p.status)).toEqual(["pending", "pending", "pending", "pending", "pending"]);
    expect(s.traffic).toEqual({ blue: 100, green: 0 });
  });

  it("shows green stabilizing as the active phase", () => {
    expect(statuses([L.started])).toEqual(["active", "pending", "pending", "pending", "pending"]);
    expect(deriveBlueGreenCutover([L.started]).outcome).toBe("in_progress");
  });

  it("moves the active phase through the health check and the switch", () => {
    expect(statuses([L.started, L.health])).toEqual(["done", "active", "pending", "pending", "pending"]);
    expect(statuses([L.started, L.health, L.switchStart])).toEqual(["done", "done", "active", "pending", "pending"]);
  });

  it("keeps 100% on blue until the cutover has actually completed - the switch phase alone does not move traffic", () => {
    expect(deriveBlueGreenCutover([L.started, L.health, L.switchStart]).traffic).toEqual({ blue: 100, green: 0 });
  });

  it("moves 100% of traffic to green once the cutover completes, with blue still running the previous version", () => {
    const s = deriveBlueGreenCutover([L.started, L.health, L.switchStart, L.switched]);
    expect(s.traffic).toEqual({ blue: 0, green: 100 });
    expect(s.blueRuns).toBe("previous");
    expect(s.phases.map((p) => p.status)).toEqual(["done", "done", "done", "active", "pending"]);
  });

  it("shows graduation as the active phase while it runs (it used to be invisible until the very end)", () => {
    const s = deriveBlueGreenCutover([L.started, L.health, L.switchStart, L.switched, L.verified]);
    expect(s.phases.map((p) => p.status)).toEqual(["done", "done", "done", "done", "active"]);
    expect(s.outcome).toBe("in_progress");
  });

  it("finishes live: traffic back on baseline, which now runs the new version", () => {
    const s = deriveBlueGreenCutover([L.started, L.health, L.switchStart, L.switched, L.verified, L.graduated, L.complete]);
    expect(s.outcome).toBe("live");
    expect(s.phases.every((p) => p.status === "done")).toBe(true);
    expect(s.traffic).toEqual({ blue: 100, green: 0 });
    expect(s.blueRuns).toBe("new");
  });

  it("treats a failed live-URL check as its own rolled-back outcome and skips graduation", () => {
    const lines = [L.started, L.health, L.switchStart, L.switched, L.verifyFailed, L.rolledBack];
    const s = deriveBlueGreenCutover(lines, "FAILED");
    expect(s.outcome).toBe("rolled_back");
    expect(s.phases.map((p) => p.status)).toEqual(["done", "done", "done", "failed", "skipped"]);
    expect(s.traffic).toEqual({ blue: 100, green: 0 });
    expect(s.blueRuns).toBe("previous");
  });

  it("is already rolled back (traffic on blue) the moment the check fails, before the rollback line arrives", () => {
    const s = deriveBlueGreenCutover([L.started, L.health, L.switchStart, L.switched, L.verifyFailed]);
    expect(s.outcome).toBe("rolled_back");
    expect(s.traffic).toEqual({ blue: 100, green: 0 });
  });

  it("marks an in-flight phase failed - never spinning forever - when the run fails for another reason", () => {
    const s = deriveBlueGreenCutover([L.started, L.health], "FAILED");
    expect(s.outcome).toBe("failed");
    expect(s.phases.map((p) => p.status)).toEqual(["done", "failed", "pending", "pending", "pending"]);
  });

  it("does not turn a completed run into failed just because its final status is COMPLETED", () => {
    const lines = [L.started, L.health, L.switchStart, L.switched, L.verified, L.graduated, L.complete];
    expect(deriveBlueGreenCutover(lines, "COMPLETED").outcome).toBe("live");
  });

  it("ignores unrelated log lines", () => {
    expect(deriveBlueGreenCutover(["--- Stage: build (build) ---", "Build finished: ok"]).outcome).toBe("waiting");
  });
});
