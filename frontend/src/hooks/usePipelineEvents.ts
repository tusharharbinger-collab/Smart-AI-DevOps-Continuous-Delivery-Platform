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
 */
import { useEffect, useRef, useState } from "react";
import type { WeightPoint } from "@/components/pipeline/TrafficWeightChart";

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
    const wsUrl = `${import.meta.env.VITE_WS_URL ?? "ws://localhost:8000/ws"}/pipelines/${pipelineRunId}`;
    const socket = new WebSocket(wsUrl);
    wsRef.current = socket;

    socket.onmessage = (event) => {
      try {
        const parsed = JSON.parse(event.data) as PipelineEventState;
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

    return () => socket.close();
  }, [pipelineRunId]);

  return {
    stages: state?.stages,
    currentStage: state?.current_stage,
    trafficWeight: state?.current_traffic_weight ?? 0,
    status: state?.status ?? "UNKNOWN",
    weightHistory,
  };
}
