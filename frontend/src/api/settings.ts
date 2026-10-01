/**
 * frontend/src/api/settings.ts
 *
 * Platform LLM-provider settings (AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.5) - the Gemini -> Groq
 * -> OpenRouter -> Mistral failover chain. Thin proxy through api-gateway's settings_router.py to
 * explainability-service, which owns the real provider credentials.
 */
import { apiClient } from "@/api/client";

export interface LlmProvider {
  name: string;
  display_name: string;
  vendor_label: string;
  priority: number;
  active_model: string;
  endpoint: string;
  masked_credential: string;
  configured: boolean;
}

export interface ProviderTestResult {
  ok: boolean;
  latency_ms: number | null;
  error: string | null;
}

export const listLlmProviders = () => apiClient.get<{ providers: LlmProvider[] }>("/api/v1/settings/llm-providers");

export const testLlmProvider = (name: string) =>
  apiClient.post<ProviderTestResult>(`/api/v1/settings/llm-providers/${name}/test`);

export const setLlmProviderCredentials = (name: string, apiKey: string) =>
  apiClient.post<{ ok: boolean }>(`/api/v1/settings/llm-providers/${name}/credentials`, { api_key: apiKey });
