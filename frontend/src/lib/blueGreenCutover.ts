/**
 * frontend/src/lib/blueGreenCutover.ts
 *
 * Backlog #5 - the blue-green cutover view. Derives the cutover's phases and the blue/green traffic split
 * from the SAME real log lines the "Live execution log" streams (useLiveLogs) - nothing here is a guess or
 * a fake progress number. Every regex below matches a line services/pipeline-worker/src/worker.py really
 * emits in its blue-green branch (three of them - cutover complete, graduation complete, rolled back -
 * were added for this view; keep the wording in sync).
 *
 * Vocabulary: BLUE is the baseline ECS service (the previously-live version); GREEN is the canary ECS
 * service (the new version). The cutover flips traffic 100/0 -> 0/100 while blue keeps running (so rollback
 * is instant); graduation redeploys baseline with the new image, returns traffic to baseline, and scales
 * green to zero (aws/ecs_deploy_task.py::graduate_blue_green_ecs).
 */

export type BlueGreenPhaseId = "stabilize" | "health" | "switch" | "verify" | "graduate";
export type BlueGreenPhaseStatus = "pending" | "active" | "done" | "failed" | "skipped";
export type BlueGreenOutcome = "waiting" | "in_progress" | "live" | "rolled_back" | "failed";

export interface BlueGreenPhase {
  id: BlueGreenPhaseId;
  label: string;
  /** What this phase means, in plain words - shown under the label. */
  detail: string;
  status: BlueGreenPhaseStatus;
}

export interface BlueGreenCutoverState {
  outcome: BlueGreenOutcome;
  phases: BlueGreenPhase[];
  /** Percent of traffic on each side right now (only ever 100/0 or 0/100 - the cutover is atomic). */
  traffic: { blue: number; green: number };
  /** Which version blue is running right now: "previous" until graduation, "new" after it. */
  blueRuns: "previous" | "new";
}

const MARKERS = {
  started: /Blue-green rollout —/,
  health: /Waiting for the green target group to report healthy/,
  switchStart: /Green is healthy — cutting over/,
  switched: /Cutover complete —/,
  verified: /Live URL verified — graduating/,
  verifyFailed: /Live URL verification failed after cutover/,
  rolledBack: /Rolled back —/,
  graduated: /Graduation complete —/,
  complete: /Blue-green rollout complete/,
} as const;

const PHASE_COPY: Record<BlueGreenPhaseId, { label: string; detail: string }> = {
  stabilize: { label: "Green starting", detail: "The new version's ECS tasks start and stabilize" },
  health: { label: "Health check", detail: "Real ALB health check against green's target group" },
  switch: { label: "Traffic switch", detail: "Atomic 100% cutover to green - blue stays running" },
  verify: { label: "Live URL check", detail: "A real request through the ALB must succeed, or it rolls back" },
  graduate: { label: "Graduation", detail: "Baseline takes the new version; green is scaled to zero" },
};

const ORDER: BlueGreenPhaseId[] = ["stabilize", "health", "switch", "verify", "graduate"];

export function deriveBlueGreenCutover(logLines: string[], runStatus?: string): BlueGreenCutoverState {
  const seen = Object.fromEntries(
    (Object.keys(MARKERS) as (keyof typeof MARKERS)[]).map((k) => [k, logLines.some((l) => MARKERS[k].test(l))])
  ) as Record<keyof typeof MARKERS, boolean>;

  const runEndedBadly = runStatus === "FAILED" || runStatus === "ROLLED_BACK";
  const rolledBack = seen.rolledBack || seen.verifyFailed;
  const finished = seen.complete || seen.graduated;

  const status: Record<BlueGreenPhaseId, BlueGreenPhaseStatus> = {
    stabilize: !seen.started ? "pending" : seen.health || seen.switchStart || seen.switched ? "done" : "active",
    health: !seen.health && !seen.switchStart ? "pending" : seen.switchStart || seen.switched ? "done" : "active",
    switch: !seen.switchStart ? "pending" : seen.switched ? "done" : "active",
    verify: !seen.switched
      ? "pending"
      : seen.verifyFailed
        ? "failed"
        : seen.verified || finished
          ? "done"
          : "active",
    graduate: rolledBack ? "skipped" : !seen.verified && !finished ? "pending" : finished ? "done" : "active",
  };

  // A run that ended badly leaves whatever was in flight failed - never spinning forever.
  if (runEndedBadly) {
    for (const id of ORDER) if (status[id] === "active") status[id] = "failed";
  }

  const phases: BlueGreenPhase[] = ORDER.map((id) => ({ id, ...PHASE_COPY[id], status: status[id] }));

  // Traffic: blue carries everything until the cutover completes; a rollback puts it back on blue.
  const onGreen = seen.switched && !rolledBack && !finished;
  const outcome: BlueGreenOutcome = finished
    ? "live"
    : rolledBack
      ? "rolled_back"
      : runEndedBadly
        ? "failed"
        : seen.started
          ? "in_progress"
          : "waiting";

  return {
    outcome,
    phases,
    traffic: onGreen ? { blue: 0, green: 100 } : { blue: 100, green: 0 },
    blueRuns: finished ? "new" : "previous",
  };
}
