/**
 * frontend/src/hooks/useLiveLogs.ts
 *
 * SSE hook for raw build/test/deploy log lines — consumes api-gateway's
 * src/routers/logs_router.py at /api/v1/pipelines/{run_id}/logs/stream.
 * Spec §11.1 explicitly calls out SSE (not WebSocket) for this feed.
 *
 * Real bug found live (Phase 8): this used the browser's `EventSource`,
 * which CANNOT send custom headers — so it never sent the `Authorization:
 * Bearer …` header that tenant_context_middleware requires, and every log
 * stream 401'd before a single line was delivered. The "Waiting for log
 * output…" placeholder made it look like a quiet pipeline rather than a
 * rejected request, in both the classic console and the project workspace.
 *
 * Fixed by streaming over `fetch` instead, which does support headers, and
 * parsing the SSE frames here. The deliberate alternative — passing the
 * token as a `?access_token=` query param so EventSource could be kept —
 * was rejected because query strings land in server access logs, browser
 * history and `Referer` headers, which is exactly where a bearer token
 * should never be.
 */
import { useEffect, useState } from "react";
import { API_BASE_URL, authHeaders } from "@/api/client";

/**
 * Splits a raw SSE chunk into its `data:` payload lines.
 *
 * sse_starlette terminates every line with CRLF, so frames are separated by
 * `\r\n\r\n`, not `\n\n`. Normalizing first matters: splitting the raw text
 * on `\n\n` finds no match at all against CRLF output, so the buffer grows
 * forever and not one line is ever emitted.
 */
function parseSseFrames(buffer: string): { lines: string[]; rest: string } {
  const frames = buffer.replace(/\r\n/g, "\n").split("\n\n");
  const rest = frames.pop() ?? "";
  const lines: string[] = [];
  for (const frame of frames) {
    for (const rawLine of frame.split("\n")) {
      if (rawLine.startsWith("data:")) lines.push(rawLine.slice(5).trimStart());
    }
  }
  return { lines, rest };
}

export function useLiveLogs(pipelineRunId: string) {
  const [logLines, setLogLines] = useState<string[]>([]);

  useEffect(() => {
    setLogLines([]);
    if (!pipelineRunId) return;

    const controller = new AbortController();
    // Per-stage filtering is done client-side in PipelineDashboard so that
    // changing or clearing the filter is instant and never re-opens the
    // stream (which would re-replay the whole log list).
    const url = `${API_BASE_URL}/api/v1/pipelines/${pipelineRunId}/logs/stream`;

    (async () => {
      try {
        const res = await fetch(url, {
          headers: { ...authHeaders(), Accept: "text/event-stream" },
          signal: controller.signal,
        });
        if (!res.ok || !res.body) return;

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const { lines, rest } = parseSseFrames(buffer);
          buffer = rest;
          if (lines.length > 0) {
            setLogLines((prev) => [...prev, ...lines].slice(-500)); // cap at 500 lines
          }
        }
      } catch {
        // Aborted on unmount, or the connection dropped — either way there is
        // nothing to recover here; the panel keeps whatever it already has.
      }
    })();

    return () => controller.abort();
  }, [pipelineRunId]);

  return { logLines };
}
