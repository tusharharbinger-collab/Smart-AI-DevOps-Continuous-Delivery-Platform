/**
 * frontend/src/lib/pipelineStageSteps.ts
 *
 * Real-time, per-stage sub-step derivation for the Pipeline View's
 * StageTimeline — purely client-side pattern matching over the SAME real
 * log lines the "Live execution log" panel already streams (useLiveLogs),
 * no backend change. Generalizes the pattern PipelineDashboard.tsx already
 * used once for its `firstDeploymentPhase` banner: every sub-step shown
 * corresponds to a real `self._log(...)` line worker.py actually emitted —
 * never a fabricated step or a fake progress percentage. The exact strings
 * matched below were read directly out of services/pipeline-worker/src/worker.py,
 * not guessed.
 */

// Matches worker.py's `_log(run_id, f"--- Stage: {stage_name} ({stage_type}) ---")`
// marker convention.
export const STAGE_MARKER_RE = /^--- Stage: (.+?) \((.+?)\) ---$/;

/** Used by the "click a stage chip to filter the log panel" feature — unchanged
 * from its original home in PipelineDashboard.tsx, just moved here. */
export function filterLogsForStage(logLines: string[], selectedStage: string | null): string[] {
  if (!selectedStage) return logLines;
  const startIndex = logLines.findIndex((line) => STAGE_MARKER_RE.exec(line)?.[1] === selectedStage);
  if (startIndex === -1) return [];
  const endIndex = logLines.findIndex((line, i) => i > startIndex && STAGE_MARKER_RE.test(line));
  return logLines.slice(startIndex, endIndex === -1 ? undefined : endIndex);
}

export interface StageLogGroup {
  /** The real stage_type worker.py declared in the manifest (e.g. "build",
   * "deploy", "canary_loop") — read straight from the marker line itself,
   * never inferred from the stage name, so a differently-named stage of a
   * known type still gets the right sub-step catalog. */
  stageType: string;
  lines: string[];
}

/** Splits the live log stream into per-stage-name groups (marker lines
 * themselves excluded from `lines` — callers only need each stage's own
 * output). */
export function splitLogsByStage(logLines: string[]): Record<string, StageLogGroup> {
  const result: Record<string, StageLogGroup> = {};
  let current: string | null = null;
  for (const line of logLines) {
    const match = STAGE_MARKER_RE.exec(line);
    if (match) {
      current = match[1];
      result[current] = result[current] ?? { stageType: match[2], lines: [] };
      continue;
    }
    if (current) result[current].lines.push(line);
  }
  return result;
}

export type SubStepStatus = "done" | "active" | "pending" | "failed";
export type StageRowStatus = "pending" | "active" | "done" | "failed";

export interface SubStep {
  id: string;
  label: string;
  status: SubStepStatus;
}

interface CatalogEntry {
  match: RegExp;
  label: string;
}

// ─────────────────────────── fixed catalogs ───────────────────────────
// Each array is the REAL, deterministic sequence worker.py follows for that
// code path — confirmed by reading src/worker.py directly. Shown as a
// checklist: entries before the last matched one are "done", the matched-
// but-not-yet-superseded one reflects the stage's own live status, and
// entries after it are "pending" (a real, always-next step for this exact
// path — not a guess) until reached, or dropped once the stage fails.

const BUILD_STEPS: CatalogEntry[] = [
  { match: /synthesized one for/i, label: "Synthesizing a Dockerfile (none declared)" },
  { match: /^Building .+ -> tag/i, label: "Building the Docker image" },
  { match: /^Build finished:/i, label: "Build complete" },
];

const TEST_STEPS: CatalogEntry[] = [
  { match: /No test command configured — skipping|^Running: /i, label: "Running tests (or skipping if none configured)" },
  { match: /^Tests finished:|Test stage failed \(non-blocking/i, label: "Tests complete" },
];

const DEPLOY_STEPS: CatalogEntry[] = [
  { match: /^Deploying canary:/i, label: "Deploying the new version" },
  { match: /^Deploy finished:/i, label: "Deployment updated" },
];

const BLUE_GREEN_STEPS: CatalogEntry[] = [
  { match: /waiting for the new \(green\) ECS service to stabilize/i, label: "Waiting for the new version to stabilize" },
  { match: /Waiting for the green target group to report healthy/i, label: "Checking real ALB health" },
  { match: /Green is healthy — cutting over/i, label: "Cutting over 100% of traffic" },
  { match: /Live URL verified — graduating|Live URL verification failed after cutover/i, label: "Verifying the live URL through the real ALB" },
  { match: /Blue-green rollout complete/i, label: "Live and verified — promoting to baseline" },
];

const FIRST_DEPLOY_ECS_STEPS: CatalogEntry[] = [
  { match: /no baseline to compare against yet/i, label: "No prior version — shipping straight to 100%" },
  { match: /Waiting for the new ECS services to become stable/i, label: "Waiting for services to stabilize" },
  { match: /ECS services stable — cutting over traffic to 100%/i, label: "Cutting over to 100%" },
  { match: /Live URL verified — a real request|Live URL verification did not succeed/i, label: "Verifying the live URL through the real ALB" },
  { match: /Baseline aligned to the same image/i, label: "Baseline aligned — future deploys are verified canaries" },
];

const FIRST_DEPLOY_K8S_STEPS: CatalogEntry[] = [
  { match: /no baseline to compare against yet/i, label: "No prior version — shipping straight to 100%" },
  { match: /Waiting for the new deployment to become healthy/i, label: "Waiting for the deployment to become healthy" },
  { match: /Deployment is healthy — cutting over traffic to 100%/i, label: "Cutting over to 100%" },
  { match: /Baseline aligned to the same image/i, label: "Baseline aligned — future deploys are verified canaries" },
];

function deriveFixedCatalogSteps(catalog: CatalogEntry[], lines: string[], stageStatus: StageRowStatus): SubStep[] {
  // Per-entry, whether IT ACTUALLY matched a real line — never inferred
  // from catalog position alone. A conditional step (e.g. Dockerfile
  // synthesis, only logged when no real Dockerfile exists) must never read
  // as "done" just because a later, unconditional step already matched.
  const matched = catalog.map((entry) => lines.some((line) => entry.match.test(line)));
  const lastMatchedIndex = matched.lastIndexOf(true);

  return catalog
    .map((entry, i) => {
      const id = `${i}-${entry.label}`;
      if (matched[i]) {
        const status: SubStepStatus =
          i === lastMatchedIndex && stageStatus !== "done"
            ? stageStatus === "failed"
              ? "failed"
              : "active"
            : "done";
        return { id, label: entry.label, status };
      }
      // Unmatched: a later entry already firing means the real run's
      // actual path skipped this one (e.g. no synthesis needed) — drop it
      // rather than ever showing it as done or stuck pending. Otherwise
      // it's a real, deterministic step still genuinely ahead — shown as
      // pending while the stage is still running, dropped once the run has
      // ended (done/failed) without ever reaching it.
      const laterEntryMatched = matched.slice(i + 1).some(Boolean);
      if (laterEntryMatched || stageStatus === "done" || stageStatus === "failed") return null;
      return { id, label: entry.label, status: "pending" as const };
    })
    .filter((s): s is SubStep => s !== null);
}

/** One entry per real "Traffic step: X% canary — running verification" /
 * "Verdict: ..." pair that has actually appeared — grown live, never a
 * fixed/guessed total, since a project can configure any number of steps. */
function deriveCanaryRampSteps(lines: string[], stageStatus: StageRowStatus): SubStep[] {
  const steps: SubStep[] = [];
  let pendingStepLabel: string | null = null;
  let stepIndex = 0;

  for (const line of lines) {
    const stepMatch = /^Traffic step: (\d+)% canary — running verification$/.exec(line);
    if (stepMatch) {
      stepIndex += 1;
      pendingStepLabel = `Step ${stepIndex}: ${stepMatch[1]}% canary — verifying`;
      steps.push({ id: `step-${stepIndex}`, label: pendingStepLabel, status: "active" });
      continue;
    }
    const verdictMatch = /^Verdict: (\w+) \(confidence=([^,]+), score=([^)]+)\)$/.exec(line);
    if (verdictMatch && steps.length > 0) {
      const last = steps[steps.length - 1];
      last.label = `Step ${stepIndex}: verdict ${verdictMatch[1]} (confidence ${verdictMatch[2]})`;
      last.status = "done";
    }
  }

  if (steps.length > 0) {
    const last = steps[steps.length - 1];
    if (last.status === "active") {
      last.status = stageStatus === "failed" ? "failed" : stageStatus === "active" ? "active" : "done";
    }
  }
  return steps;
}

/**
 * Entry point — detects which real code path canary_verify is actually
 * running (from the log content itself, never guessed) and returns the
 * matching sub-step sequence. `stageStatus` is the SAME pending/active/
 * done/failed state StageTimeline already computes for the stage-level row
 * (mirrors PipelineDAG.tsx's isCurrent/isDone/isFailed logic) — it decides
 * whether the last real sub-step reads as still-spinning, complete, or the
 * point of failure, never a separate guess.
 */
export function deriveSubSteps(stageType: string, stageLogLines: string[], stageStatus: StageRowStatus): SubStep[] {
  if (stageStatus === "pending") return [];

  if (stageType === "build") return deriveFixedCatalogSteps(BUILD_STEPS, stageLogLines, stageStatus);
  if (stageType === "test") return deriveFixedCatalogSteps(TEST_STEPS, stageLogLines, stageStatus);
  if (stageType === "deploy") return deriveFixedCatalogSteps(DEPLOY_STEPS, stageLogLines, stageStatus);

  if (stageType === "canary_loop") {
    if (stageLogLines.some((l) => /Blue-green rollout —/.test(l))) {
      return deriveFixedCatalogSteps(BLUE_GREEN_STEPS, stageLogLines, stageStatus);
    }
    if (stageLogLines.some((l) => /no baseline to compare against yet/i.test(l))) {
      const isEcs = stageLogLines.some((l) => /ECS services/i.test(l));
      return deriveFixedCatalogSteps(isEcs ? FIRST_DEPLOY_ECS_STEPS : FIRST_DEPLOY_K8S_STEPS, stageLogLines, stageStatus);
    }
    if (stageLogLines.some((l) => /^Traffic step:/.test(l))) {
      return deriveCanaryRampSteps(stageLogLines, stageStatus);
    }
    // Stage just started — no distinguishing line has arrived yet. A single
    // real, honest placeholder (not a fabricated step) so the timeline
    // never looks empty/stuck in this brief window.
    return [{ id: "starting", label: "Starting verification…", status: stageStatus === "failed" ? "failed" : "active" }];
  }

  return [];
}
