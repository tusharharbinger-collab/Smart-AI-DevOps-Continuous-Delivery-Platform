/**
 * frontend/src/hooks/usePipelineEvents.ts
 *
 * WebSocket hook for structured pipeline state events (stage transitions,
 * traffic weight changes) — consumes api-gateway's
 * src/websocket/event_stream.py at /ws/pipelines/{pipeline_run_id}.
 * Distinct from useLiveLogs, which is SSE-backed for raw log lines.
 *
 * `weightHistory` is accumulated client-side from each live push — the
 * backend only ever reports the current weight, not a time series, so this
 * is a per-viewing-session chart, not a durable history. Good enough to
 * visualize a rollout's progression while you're watching it.
 *
 * Real gap found live: this used to be WebSocket-only, seeding `status` from
 * nothing until the first live push arrived. An already-terminal run
 * (COMPLETED/FAILED/ROLLED_BACK) never produces another event, so opening
 * its dashboard left `status` stuck at "UNKNOWN" forever — which
 * PipelineDashboard.tsx's Emergency Rollback disabled-check doesn't
 * recognize as terminal, so the button stayed wrongly enabled for a run
 * that was already finished. Seeding an initial snapshot via `getRun` (the
 * same Redis-then-Postgres-fallback endpoint Screen 1 already used
 * elsewhere) closes that gap; a live WS push, once one arrives, still wins.
 */
import { useEffect, useRef, useState } from "react";
import type { WeightPoint } from "@/components/pipeline/TrafficWeightChart";
import { getRun } from "@/api/pipeline";

export interface PipelineEventState {
  pipeline_run_id?: string;
  status?: string;
  current_stage?: string;
  current_traffic_weight?: number;
  stages?: string[];
}

export function usePipelineEvents(pipelineRunId: string) {
  const [state, setState] = useState<PipelineEventState | null>(null);
  const [weightHistory, setWeightHistory] = useState<WeightPoint[]>([]);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    setState(null);
    setWeightHistory([]);
    if (!pipelineRunId) return;
    let cancelled = false;
    let receivedLiveEvent = false;

    getRun(pipelineRunId)
      .then((run) => {
        // Only seed from the snapshot if no live WS push has already landed
        // — a live event is always fresher than this point-in-time fetch,
        // and must never be overwritten by a slower-resolving snapshot.
        if (cancelled || receivedLiveEvent) return;
        setState((prev) => ({ ...prev, ...run }));
      })
      .catch(() => {
        // No snapshot available (e.g. a brand-new run not committed yet) —
        // the WS stream is still the primary source and unaffected.
      });

    const wsUrl = `${import.meta.env.VITE_WS_URL ?? "ws://localhost:8000/ws"}/pipelines/${pipelineRunId}`;
    const socket = new WebSocket(wsUrl);
    wsRef.current = socket;

    socket.onmessage = (event) => {
      try {
        const parsed = JSON.parse(event.data) as PipelineEventState;
        receivedLiveEvent = true;
        setState((prev) => ({ ...prev, ...parsed }));
        if (typeof parsed.current_traffic_weight === "number") {
          setWeightHistory((prev) => [
            ...prev.slice(-99),
            { time: new Date().toLocaleTimeString(), weight: parsed.current_traffic_weight! },
          ]);
        }
      } catch {
        // Ignore malformed frames rather than crashing the dashboard.
      }
    };
    socket.onerror = () => socket.close();

    return () => {
      cancelled = true;
      socket.close();
    };
  }, [pipelineRunId]);

  return {
    stages: state?.stages,
    currentStage: state?.current_stage,
    trafficWeight: state?.current_traffic_weight ?? 0,
    status: state?.status ?? "UNKNOWN",
    weightHistory,
  };
}
