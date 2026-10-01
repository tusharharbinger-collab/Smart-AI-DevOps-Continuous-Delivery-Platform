/**
 * AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §7 (Phase F, Stage C) — the live build timeline. Every
 * status shown must trace to a real event in `resource_events`, never a fabricated progress percentage.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { InfraBuildTimeline } from "./InfraBuildTimeline";
import type { ResourceChange, ResourceEvent } from "@/api/infraDrafts";

const CHANGES: ResourceChange[] = [
  { action: "Add", logical_id: "OrdersDb", resource_type: "AWS::RDS::DBInstance" },
  { action: "Add", logical_id: "CacheCluster", resource_type: "AWS::ElastiCache::CacheCluster" },
  { action: "Add", logical_id: "AppBucket", resource_type: "AWS::S3::Bucket" },
];

describe("InfraBuildTimeline", () => {
  it("shows a resource with no event yet as pending", () => {
    render(<InfraBuildTimeline changes={CHANGES} resourceEvents={[]} overallStatus="INFRA_PROVISIONING" />);
    expect(screen.getByText("OrdersDb")).toBeInTheDocument();
    expect(screen.getByText("AWS::RDS::DBInstance")).toBeInTheDocument();
  });

  it("derives active/done status from the real latest event per resource", () => {
    const events: ResourceEvent[] = [
      { resource: "OrdersDb", type: "AWS::RDS::DBInstance", status: "CREATE_IN_PROGRESS", reason: null, timestamp: "1" },
      { resource: "CacheCluster", type: "AWS::ElastiCache::CacheCluster", status: "CREATE_COMPLETE", reason: null, timestamp: "2" },
    ];
    render(<InfraBuildTimeline changes={CHANGES} resourceEvents={events} overallStatus="INFRA_PROVISIONING" />);
    expect(screen.getByText("CREATE_IN_PROGRESS")).toBeInTheDocument();
    expect(screen.getByText("CREATE_COMPLETE")).toBeInTheDocument();
  });

  it("shows the real failure reason verbatim when a resource fails", () => {
    const events: ResourceEvent[] = [
      { resource: "AppBucket", type: "AWS::S3::Bucket", status: "CREATE_FAILED", reason: "Bucket already exists", timestamp: "1" },
    ];
    render(<InfraBuildTimeline changes={CHANGES} resourceEvents={events} overallStatus="INFRA_PROVISIONING" />);
    expect(screen.getByText("Bucket already exists")).toBeInTheDocument();
  });

  it("shows a no-changes message when the change set is empty", () => {
    render(<InfraBuildTimeline changes={[]} resourceEvents={[]} overallStatus="INFRA_PROVISIONING" />);
    expect(screen.getByText(/already matches the proposal/)).toBeInTheDocument();
  });
});
