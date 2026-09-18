/**
 * frontend/src/api/chatops.ts — ChatOps query interface (assignment bonus item:
 * "a query interface that still grounds its answer in the real comparison data").
 *
 * Backend: POST /api/v1/projects/{project_id}/ask (projects_router.py) — a thin,
 * auth-checked proxy to explainability-service's POST /chatops/ask, which
 * assembles real grounded context (recent runs, verdicts, audit actions, cost)
 * and calls Groq. See PROJECT_DEEP_DIVE.md §4/§12 for the full pattern this reuses.
 */
import { apiClient } from "./client";

export interface ChatOpsAnswer {
  answer: string;
  cited_run_ids: string[];
  confidence: "grounded" | "insufficient_data";
}

export const askProjectQuestion = (projectId: string, question: string) =>
  apiClient.post<ChatOpsAnswer>(`/api/v1/projects/${projectId}/ask`, { question });
