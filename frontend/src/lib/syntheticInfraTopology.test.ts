/**
 * Real gap found live: an AI-generated infra draft's topology only ever contains the EXTRAS the app needs
 * beyond the platform's standard setup (a collapsed "app" placeholder plus e.g. an S3 bucket) —
 * PipelineDashboard.tsx used to show that topology ALONE for a project with a linked draft, so the real
 * ALB/baseline/canary services it's actually running on vanished from the graph, leaving only "Your app" +
 * "S3 Bucket". mergeInfraTopologies grafts the AI extras onto the always-real platform topology instead.
 */
import { describe, expect, it } from "vitest";
import { mergeInfraTopologies, synthesizeRealInfraTopology } from "./syntheticInfraTopology";
import type { InfraTopology } from "@/api/infraDrafts";

describe("synthesizeRealInfraTopology", () => {
  it("builds the real ALB + baseline/canary ECS shape for aws_ecs", () => {
    const t = synthesizeRealInfraTopology("My App", "aws_ecs", "v1.0.0", "v1.1.0");
    expect(t.nodes.map((n) => n.id)).toEqual(["alb", "ecs_baseline", "ecs_canary"]);
    expect(t.nodes.find((n) => n.id === "ecs_baseline")?.label).toBe("my-app-baseline (v1.0.0)");
  });

  it("builds the real HTTPRoute + baseline/canary Deployment shape for kubernetes", () => {
    const t = synthesizeRealInfraTopology("My App", "kubernetes", "v1.0.0", "v1.1.0");
    expect(t.nodes.map((n) => n.id)).toEqual(["httproute", "deploy_baseline", "deploy_canary"]);
  });
});

describe("mergeInfraTopologies", () => {
  const platform: InfraTopology = synthesizeRealInfraTopology("testing-2", "aws_ecs", "v1.0.0", "v1.1.0");

  it("returns the platform topology unchanged when there is no linked AI draft", () => {
    expect(mergeInfraTopologies(platform, null)).toEqual(platform);
  });

  it("returns the platform topology unchanged when the AI draft has no real extras (standard-only)", () => {
    const aiTopology: InfraTopology = { nodes: [{ id: "app", type: "app", label: "Your app" }], edges: [] };
    expect(mergeInfraTopologies(platform, aiTopology)).toEqual(platform);
  });

  it("grafts the AI draft's real extra resources onto the platform's real baseline node, not a placeholder", () => {
    const aiTopology: InfraTopology = {
      nodes: [
        { id: "app", type: "app", label: "Your app (platform-provided services)" },
        { id: "s3bucket", type: "aws_s3_bucket", label: "S3 Bucket" },
      ],
      edges: [{ source: "app", target: "s3bucket" }],
    };

    const merged = mergeInfraTopologies(platform, aiTopology);

    // The real platform infra (ALB, baseline, canary) must still be present - this is the exact bug found
    // live: a linked AI draft used to make these vanish, leaving only "Your app" + "S3 Bucket".
    expect(merged.nodes.map((n) => n.id)).toEqual(["alb", "ecs_baseline", "ecs_canary", "s3bucket"]);
    // The AI's own "app" placeholder is dropped (the real baseline node already represents it).
    expect(merged.nodes.find((n) => n.id === "app")).toBeUndefined();
    // The S3 bucket now hangs off the REAL baseline service, not the AI's placeholder.
    expect(merged.edges).toContainEqual({ source: "ecs_baseline", target: "s3bucket" });
    expect(merged.edges).toContainEqual({ source: "alb", target: "ecs_baseline" });
    expect(merged.edges).toContainEqual({ source: "alb", target: "ecs_canary" });
  });

  it("preserves edges between two non-placeholder AI extras unchanged", () => {
    const aiTopology: InfraTopology = {
      nodes: [
        { id: "app", type: "app", label: "Your app" },
        { id: "db", type: "aws_rds_instance", label: "Postgres DB" },
        { id: "sg", type: "aws_ec2_securitygroup", label: "DB Security Group" },
      ],
      edges: [
        { source: "app", target: "db" },
        { source: "sg", target: "db" },
      ],
    };

    const merged = mergeInfraTopologies(platform, aiTopology);

    expect(merged.edges).toContainEqual({ source: "ecs_baseline", target: "db" });
    expect(merged.edges).toContainEqual({ source: "sg", target: "db" });
  });
});
