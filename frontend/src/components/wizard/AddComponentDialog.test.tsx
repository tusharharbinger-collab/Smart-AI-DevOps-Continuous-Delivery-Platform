/**
 * AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.2 (Phase C) — the "Add a component" picker. Covers:
 * catalog loading, running a compatibility check via the real endpoint, confirming applies the returned
 * instruction through the EXISTING edit endpoint (never a separate generation path), and that an
 * incompatible result disables "Add this component".
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { AddComponentDialog } from "./AddComponentDialog";

vi.mock("@/api/client", () => ({ apiClient: { get: vi.fn(), post: vi.fn() } }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const CATALOG = {
  catalog: [
    {
      resource_type: "ec2_instance", display_name: "EC2 instance", category: "Compute",
      description: "A standalone virtual machine.",
      params: [{ name: "instance_type", type: "string", description: "e.g. t3.micro", required: true }],
    },
    {
      resource_type: "efs_file_system", display_name: "EFS file system", category: "Storage",
      description: "A shared file system.", params: [],
    },
  ],
};

function renderDialog(onApplied = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <AddComponentDialog draftId="draft-1" onApplied={onApplied} />
    </QueryClientProvider>
  );
  return { onApplied };
}

beforeEach(() => {
  vi.mocked(apiClient.get).mockReset();
  vi.mocked(apiClient.post).mockReset();
});

describe("AddComponentDialog", () => {
  it("loads the real catalog only once opened", async () => {
    vi.mocked(apiClient.get).mockResolvedValue(CATALOG);
    renderDialog();

    expect(apiClient.get).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Add a component"));

    await waitFor(() => expect(apiClient.get).toHaveBeenCalledWith("/api/v1/projects/infra-drafts/component-catalog"));
  });

  it("confirming a compatible result applies the returned instruction via the existing edit endpoint", async () => {
    vi.mocked(apiClient.get).mockResolvedValue(CATALOG);
    vi.mocked(apiClient.post).mockImplementation(async (path: string) => {
      if (path.endsWith("/check-component")) {
        return { compatible: true, explanation: "Fine to add.", caveats: [], source: "rule_table", instruction: "Add an EFS file system." };
      }
      if (path.endsWith("/edit")) {
        return { draft_id: "draft-2" };
      }
      throw new Error(`unexpected path ${path}`);
    });
    const { onApplied } = renderDialog();

    fireEvent.click(screen.getByText("Add a component"));
    await screen.findByText("Resource type");

    // No native <select>, so drive the shadcn Select via its trigger + listbox item.
    fireEvent.click(screen.getByText("Choose a resource type…"));
    fireEvent.click(await screen.findByText("EFS file system"));

    fireEvent.click(screen.getByText("Check compatibility"));
    await screen.findByText("Can be added");

    fireEvent.click(screen.getByText("Add this component"));

    await waitFor(() =>
      expect(apiClient.post).toHaveBeenCalledWith("/api/v1/projects/infra-drafts/draft-1/edit", {
        instruction: "Add an EFS file system.",
        resource_type: "efs_file_system",
        mode: "add",
      })
    );
    await waitFor(() => expect(onApplied).toHaveBeenCalledWith({ draft_id: "draft-2" }));
  });

  it("disables 'Add this component' when the check result is incompatible", async () => {
    vi.mocked(apiClient.get).mockResolvedValue(CATALOG);
    vi.mocked(apiClient.post).mockResolvedValue({
      compatible: false, explanation: "Not compatible.", caveats: ["conflict"], source: "rule_table", instruction: "",
    });
    renderDialog();

    fireEvent.click(screen.getByText("Add a component"));
    await screen.findByText("Resource type");
    fireEvent.click(screen.getByText("Choose a resource type…"));
    fireEvent.click(await screen.findByText("EFS file system"));
    fireEvent.click(screen.getByText("Check compatibility"));

    await screen.findByText("Cannot be added");
    expect(screen.getByText("Add this component").closest("button")).toBeDisabled();
  });
});
