/**
 * AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.5 - the LLM Providers settings page. Covers: rendering
 * real provider status (priority, model, masked credential, configured/not), the Test Connection button
 * calling the real endpoint and showing its real result, and Edit Credentials submitting through the real
 * apiClient call rather than a bare fetch.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { SettingsPage } from "./SettingsPage";
import type { LlmProvider } from "@/api/settings";

vi.mock("@/api/client", () => ({ apiClient: { get: vi.fn(), post: vi.fn() } }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const PROVIDERS: LlmProvider[] = [
  {
    name: "gemini", display_name: "Gemini", vendor_label: "Google Gemini", priority: 1,
    active_model: "gemini-2.0-flash", endpoint: "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    masked_credential: "", configured: false,
  },
  {
    name: "groq", display_name: "Groq", vendor_label: "Groq LPU Accelerator", priority: 2,
    active_model: "openai/gpt-oss-120b", endpoint: "https://api.groq.com/openai/v1/chat/completions",
    masked_credential: "gsk_****jrC", configured: true,
  },
];

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SettingsPage />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.mocked(apiClient.get).mockReset();
  vi.mocked(apiClient.post).mockReset();
});

describe("SettingsPage", () => {
  it("renders every provider's real status via apiClient, never a bare fetch", async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ providers: PROVIDERS });
    renderPage();

    expect(apiClient.get).toHaveBeenCalledWith("/api/v1/settings/llm-providers");
    expect(await screen.findByText("Gemini")).toBeInTheDocument();
    expect(screen.getByText("Groq")).toBeInTheDocument();
    expect(screen.getByText("gsk_****jrC")).toBeInTheDocument();
    expect(screen.getByText("not configured")).toBeInTheDocument();
    expect(screen.getByText("openai/gpt-oss-120b")).toBeInTheDocument();
  });

  it("Test Connection calls the real endpoint and shows the real result", async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ providers: PROVIDERS });
    vi.mocked(apiClient.post).mockResolvedValue({ ok: true, latency_ms: 850, error: null });
    renderPage();

    await screen.findByText("Groq");
    const buttons = screen.getAllByText("Test Connection");
    // Second card is Groq (configured=true, so its button isn't disabled).
    fireEvent.click(buttons[1]);

    await waitFor(() =>
      expect(apiClient.post).toHaveBeenCalledWith("/api/v1/settings/llm-providers/groq/test")
    );
  });

  it("disables Test Connection for an unconfigured provider", async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ providers: PROVIDERS });
    renderPage();

    await screen.findByText("Gemini");
    const buttons = screen.getAllByText("Test Connection");
    expect(buttons[0].closest("button")).toBeDisabled();
  });

  it("Edit Credentials submits the new key through apiClient.post", async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ providers: PROVIDERS });
    vi.mocked(apiClient.post).mockResolvedValue({ ok: true });
    renderPage();

    await screen.findByText("Gemini");
    fireEvent.click(screen.getAllByText("Edit Credentials")[0]);

    const input = await screen.findByPlaceholderText(/Paste the new key/);
    fireEvent.change(input, { target: { value: "new-gemini-key" } });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() =>
      expect(apiClient.post).toHaveBeenCalledWith("/api/v1/settings/llm-providers/gemini/credentials", {
        api_key: "new-gemini-key",
      })
    );
  });
});
