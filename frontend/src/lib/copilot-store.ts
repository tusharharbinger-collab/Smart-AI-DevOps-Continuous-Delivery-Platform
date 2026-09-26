/**
 * frontend/src/lib/copilot-store.ts
 *
 * In-memory state store for the AI DevOps Copilot & UI Guide.
 * CONTEXT IS STORED ONLY IN THIS ACTIVE CHAT SESSION:
 * - Never persisted to localStorage.
 * - Never persisted to PostgreSQL database tables.
 * - Instantly clearable via clearChat().
 */
import { create } from "zustand";
import { chatWithCopilot, type ChatMessage } from "@/api/copilot";

interface CopilotState {
  isOpen: boolean;
  messages: ChatMessage[];
  loading: boolean;
  error: string | null;
  projectId: string | null;
  wizardContext: Record<string, unknown> | null;
  suggestedActions: string[];

  // Actions
  openCopilot: (initialPrompt?: string, wizardContext?: Record<string, unknown>, projectId?: string) => void;
  closeCopilot: () => void;
  toggleCopilot: () => void;
  sendMessage: (content: string) => Promise<void>;
  clearChat: () => void;
  setWizardContext: (context: Record<string, unknown> | null) => void;
  setProjectId: (id: string | null) => void;
}

const DEFAULT_SUGGESTIONS = [
  "Which container port should I select for my app?",
  "How do I choose between Canary and Blue-Green?",
  "Explain what the UI status badges mean",
  "How does Path Prefix routing work on the ALB?",
];

export const useCopilotStore = create<CopilotState>((set, get) => ({
  isOpen: false,
  messages: [],
  loading: false,
  error: null,
  projectId: null,
  wizardContext: null,
  suggestedActions: DEFAULT_SUGGESTIONS,

  openCopilot: (initialPrompt, wizardContext, projectId) => {
    set((state) => ({
      isOpen: true,
      wizardContext: wizardContext ?? state.wizardContext,
      projectId: projectId ?? state.projectId,
    }));
    if (initialPrompt) {
      void get().sendMessage(initialPrompt);
    }
  },

  closeCopilot: () => set({ isOpen: false }),

  toggleCopilot: () => set((s) => ({ isOpen: !s.isOpen })),

  clearChat: () =>
    set({
      messages: [],
      error: null,
      suggestedActions: DEFAULT_SUGGESTIONS,
    }),

  setWizardContext: (context) => set({ wizardContext: context }),
  setProjectId: (id) => set({ projectId: id }),

  sendMessage: async (content: string) => {
    const text = content.trim();
    if (!text || get().loading) return;

    const newMessages: ChatMessage[] = [...get().messages, { role: "user", content: text }];
    set({ messages: newMessages, loading: true, error: null });

    try {
      const response = await chatWithCopilot({
        messages: newMessages,
        project_id: get().projectId ?? undefined,
        wizard_context: get().wizardContext ?? undefined,
      });

      set({
        messages: [
          ...newMessages,
          { role: "assistant", content: response.reply },
        ],
        suggestedActions: response.suggested_actions.length > 0
          ? response.suggested_actions
          : DEFAULT_SUGGESTIONS,
        loading: false,
      });
    } catch (err: unknown) {
      const errMsg = err instanceof Error ? err.message : "Failed to communicate with DevOps Copilot";
      set({
        messages: [
          ...newMessages,
          {
            role: "assistant",
            content: "⚠️ **Connection Error**: Unable to reach the DevOps Copilot service. Please verify network connectivity or check service health.",
          },
        ],
        error: errMsg,
        loading: false,
      });
    }
  },
}));
