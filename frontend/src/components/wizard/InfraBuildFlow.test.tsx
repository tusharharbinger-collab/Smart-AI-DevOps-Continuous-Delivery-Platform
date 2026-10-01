/**
 * AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §7 (Phase F) — the replacement chat -> confirm -> build
 * flow. Covers the real stage transitions off real InfraDraft status values: setup -> chat (after a real
 * createInfraDraft), a chat edit updating the conversation, and the standard-only ("nothing extra to
 * build") shortcut that skips straight to done without ever touching change-set/execute (matching the
 * real backend behavior where those 409 for a no_additional_infrastructure draft).
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { InfraBuildFlow } from "./InfraBuildFlow";
import * as infraDraftsApi from "@/api/infraDrafts";
import type { InfraDraft, InfraProposal } from "@/api/infraDrafts";
import type { IntentSpec } from "@/api/intentSpec";

function renderFlow(value: IntentSpec) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <InfraBuildFlow value={value} />
    </QueryClientProvider>
  );
}

vi.mock("@/api/awsConnections", () => ({
  listAwsConnections: vi.fn().mockResolvedValue([]),
  createAwsConnection: vi.fn(),
  verifyAwsConnection: vi.fn(),
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const VALUE: IntentSpec = {
  environment_tier: "dev", expected_rps: null, min_instances: null, max_instances: null,
  needs_database: false, database_type: null, multi_az: false, needs_cache: false, needs_object_storage: false,
  public_facing: true, private_subnet_only: false, encryption_at_rest_required: false,
  monthly_budget_usd: null, aws_region: "us-east-1", archetype: "stateless_web_service",
  detected_database_hint: null, detected_cache_hint: null, detected_storage_hint: null, detected_confidence: null,
} as IntentSpec;

function baseDraft(overrides: Partial<InfraDraft> = {}): InfraDraft {
  const proposal: InfraProposal = {
    name: "p", topology: { nodes: [{ id: "svc", type: "AWS::ECS::Service", label: "Service" }], edges: [] },
    iac_terraform: "", estimated_monthly_cost_usd: 42, cost_breakdown: [], policy_checks: [],
    needs_summary: "This app needs a stateless web service.",
  };
  return {
    draft_id: "draft-1", project_id: null, status: "INFRA_PENDING_APPROVAL", intent_spec: VALUE,
    archetype: "stateless_web_service", infra_proposal: proposal, readiness_outcome: "warn", readiness_reasons: [],
    error_message: null, cloud_provider: "aws", change_set_id: null, stack_name: null, stack_arn: null,
    change_set_changes: [], provisioning_error: null, provisioning_outputs: {}, source: "ai_created",
    existing_resources: {}, parent_draft_id: null, aws_connection_id: null, created_at: null, updated_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("InfraBuildFlow", () => {
  it("starts on the setup stage and moves to the chat stage after a real proposal is created", async () => {
    const draft = baseDraft();
    vi.spyOn(infraDraftsApi, "createInfraDraft").mockResolvedValue(draft);

    renderFlow(VALUE);
    expect(screen.getByText("Ask the Infra Architect")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Ask the Infra Architect"));

    await waitFor(() => expect(screen.getByTestId("infra-chat-panel")).toBeInTheDocument());
    expect(screen.getByText(/This app needs a stateless web service\./)).toBeInTheDocument();
    expect(screen.getByText(/\$42\.00\/mo/)).toBeInTheDocument();
  });

  it("a chat edit calls editInfraDraft and appends both the user and assistant turns", async () => {
    const draft = baseDraft();
    vi.spyOn(infraDraftsApi, "createInfraDraft").mockResolvedValue(draft);
    const edited = baseDraft({
      infra_proposal: {
        ...draft.infra_proposal!,
        topology: { nodes: [...draft.infra_proposal!.topology.nodes, { id: "cache", type: "AWS::ElastiCache::CacheCluster", label: "Cache" }], edges: [] },
        estimated_monthly_cost_usd: 55,
      },
    });
    vi.spyOn(infraDraftsApi, "editInfraDraft").mockResolvedValue(edited);

    renderFlow(VALUE);
    fireEvent.click(screen.getByText("Ask the Infra Architect"));
    await screen.findByTestId("infra-chat-panel");

    const input = screen.getByPlaceholderText(/add a Redis cache/);
    fireEvent.change(input, { target: { value: "add a redis cache" } });
    fireEvent.submit(input.closest("form")!);

    expect(await screen.findByText("add a redis cache")).toBeInTheDocument();
    await waitFor(() => expect(infraDraftsApi.editInfraDraft).toHaveBeenCalledWith("draft-1", "add a redis cache", { mode: "add" }));
    expect(await screen.findByText(/Added: Cache\./)).toBeInTheDocument();
    expect(await screen.findByText(/\$42\.00\/mo -> \$55\.00\/mo/)).toBeInTheDocument();
  });

  it("a standard-only draft skips straight to done after approval, never touching the change-set flow", async () => {
    const standardOnly = baseDraft({
      infra_proposal: {
        name: "p", topology: { nodes: [], edges: [] }, iac_terraform: "", estimated_monthly_cost_usd: 0,
        cost_breakdown: [], policy_checks: [], no_additional_infrastructure: true,
        needs_summary: "Nothing beyond the platform's standard build.",
      },
    });
    vi.spyOn(infraDraftsApi, "createInfraDraft").mockResolvedValue(standardOnly);
    const approved = { ...standardOnly, status: "INFRA_APPROVED" as const };
    vi.spyOn(infraDraftsApi, "approveInfraDraft").mockResolvedValue(approved);
    const createChangeSetSpy = vi.spyOn(infraDraftsApi, "createInfraChangeSet");

    renderFlow(VALUE);
    fireEvent.click(screen.getByText("Ask the Infra Architect"));
    await screen.findByTestId("infra-chat-panel");

    fireEvent.click(screen.getByText("Use this infrastructure"));

    await waitFor(() => expect(screen.getByText(/nothing extra to build/)).toBeInTheDocument());
    expect(createChangeSetSpy).not.toHaveBeenCalled();
  });

  it("real bug fix: asking to add an S3 bucket on a standard-only draft now works instead of dead-ending", async () => {
    const standardOnly = baseDraft({
      infra_proposal: {
        name: "p", topology: { nodes: [], edges: [] }, iac_terraform: "", estimated_monthly_cost_usd: 0,
        cost_breakdown: [], policy_checks: [], no_additional_infrastructure: true,
        needs_summary: "Nothing beyond the platform's standard build.",
      },
    });
    vi.spyOn(infraDraftsApi, "createInfraDraft").mockResolvedValue(standardOnly);
    const withBucket = baseDraft({
      infra_proposal: {
        name: "p", topology: { nodes: [{ id: "bucket", type: "AWS::S3::Bucket", label: "S3 bucket" }], edges: [] },
        iac_terraform: "", estimated_monthly_cost_usd: 5, cost_breakdown: [], policy_checks: [],
        no_additional_infrastructure: false,
      },
    });
    vi.spyOn(infraDraftsApi, "editInfraDraft").mockResolvedValue(withBucket);

    renderFlow(VALUE);
    fireEvent.click(screen.getByText("Ask the Infra Architect"));
    await screen.findByTestId("infra-chat-panel");

    const input = screen.getByPlaceholderText(/add a Redis cache/);
    fireEvent.change(input, { target: { value: "add S3 bucket" } });
    fireEvent.submit(input.closest("form")!);

    await waitFor(() => expect(infraDraftsApi.editInfraDraft).toHaveBeenCalledWith("draft-1", "add S3 bucket", { mode: "add" }));
    expect(await screen.findByText(/Added: S3 bucket\./)).toBeInTheDocument();
    expect(screen.queryByText(/I couldn't apply that/)).not.toBeInTheDocument();
  });

  it("feature: a draft that already has real additions offers an explicit Add/Replace choice for the next chat message", async () => {
    // User-reported gap: chat edits always extended the current proposal with no way to instead
    // "delete and start over" - the Add/Replace toggle only appears once there's something to replace.
    const withCache = baseDraft({
      infra_proposal: {
        name: "p", topology: { nodes: [{ id: "cache", type: "AWS::ElastiCache::CacheCluster", label: "Cache" }], edges: [] },
        iac_terraform: "", estimated_monthly_cost_usd: 15, cost_breakdown: [], policy_checks: [],
        no_additional_infrastructure: false, additions: [{ kind: "cache", reason: "add a redis cache" }],
      },
    });
    vi.spyOn(infraDraftsApi, "createInfraDraft").mockResolvedValue(withCache);
    const withBucketToo = baseDraft({
      infra_proposal: {
        name: "p", topology: { nodes: [{ id: "bucket", type: "AWS::S3::Bucket", label: "S3 bucket" }], edges: [] },
        iac_terraform: "", estimated_monthly_cost_usd: 5, cost_breakdown: [], policy_checks: [],
        no_additional_infrastructure: false, additions: [{ kind: "object_storage", reason: "add an S3 bucket" }],
      },
    });
    vi.spyOn(infraDraftsApi, "editInfraDraft").mockResolvedValue(withBucketToo);

    renderFlow(VALUE);
    fireEvent.click(screen.getByText("Ask the Infra Architect"));
    await screen.findByTestId("infra-chat-panel");

    // No addition yet on the FIRST draft this session created - the toggle only shows once a real
    // proposal with additions exists, which it does here (withCache already has one).
    expect(await screen.findByText("Replace")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Replace"));
    expect(await screen.findByText(/Next message replaces the whole proposal/)).toBeInTheDocument();

    const input = screen.getByPlaceholderText(/add a Redis cache/);
    fireEvent.change(input, { target: { value: "add an S3 bucket instead" } });
    fireEvent.submit(input.closest("form")!);

    await waitFor(() =>
      expect(infraDraftsApi.editInfraDraft).toHaveBeenCalledWith("draft-1", "add an S3 bucket instead", { mode: "replace" })
    );
    // Mode resets back to "add" after a replace completes, so the destructive choice never sticks silently.
    await waitFor(() => expect(screen.queryByText(/Next message replaces the whole proposal/)).not.toBeInTheDocument());
  });
});
