/**
 * frontend/src/api/copilot.ts
 *
 * API client for the Smart AI DevOps Copilot & UI Guide.
 * Communicates with POST /api/v1/copilot/chat.
 * Stores zero persistent data on server — session context lives purely in the active chat.
 */
import { apiClient } from "./client";

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
}

export interface CopilotChatRequest {
  messages: ChatMessage[];
  project_id?: string;
  wizard_context?: Record<string, unknown>;
}

export interface CopilotChatResponse {
  reply: string;
  suggested_actions: string[];
}

export const chatWithCopilot = (data: CopilotChatRequest) =>
  apiClient.post<CopilotChatResponse>("/api/v1/copilot/chat", data);
