/**
 * frontend/src/lib/syntheticInfraTopology.ts
 *
 * Real gap found live: ProjectDeliveryGraph.tsx only ever rendered infra
 * boxes when an AI-generated infra_build_state draft was linked to the
 * project (via getProjectInfraDraft()) — a project onboarded before that
 * linking existed, or onboarded without ever clicking "Preview
 * Infrastructure (AI)" in the Requirements Form, showed NO infrastructure
 * at all, just the pipeline stage chain. But every onboarded project has
 * REAL infrastructure behind it regardless of whether it went through the
 * AI flow — a shared ALB + baseline/canary ECS services (deploy_target ==
 * "aws_ecs"), or an HTTPRoute + baseline/canary Deployments (deploy_target
 * == "kubernetes") — see shared/aws_ecs_actuation.py / actuation_executor.py.
 *
 * This synthesizes that always-true topology from the project's own
 * onboarding fields (deploy_target, name) as the fallback shown whenever no
 * AI draft is linked — real resource shapes and naming convention (matching
 * projects_router.py's `_k8s_name` slug + `{service}-baseline`/
 * `{service}-canary` target-group naming), not a placeholder.
 */
import type { InfraTopology } from "@/api/infraDrafts";

/** Mirrors projects_router.py's `_k8s_name()` — lowercase-slugify for a real resource name. */
function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/^-+|-+$/g, "") || "service";
}

export function synthesizeRealInfraTopology(
  projectName: string,
  deployTarget: string | undefined,
): InfraTopology {
  const service = slugify(projectName);

  if (deployTarget === "aws_ecs") {
    return {
      nodes: [
        { id: "alb", type: "alb", label: "Shared ALB (smartcd-platform-alb)" },
        { id: "ecs_baseline", type: "ecs_service", label: `${service}-baseline` },
        { id: "ecs_canary", type: "ecs_service", label: `${service}-canary` },
      ],
      edges: [
        { source: "alb", target: "ecs_baseline" },
        { source: "alb", target: "ecs_canary" },
      ],
    };
  }

  // Default / "kubernetes": Envoy Gateway HTTPRoute splitting weighted
  // traffic across baseline/canary Deployments (invariant 5 — traffic
  // shifting via the HTTPRoute, never replica-count games).
  return {
    nodes: [
      { id: "httproute", type: "httproute", label: `${service}-rollout HTTPRoute` },
      { id: "deploy_baseline", type: "deployment", label: `${service}-baseline` },
      { id: "deploy_canary", type: "deployment", label: `${service}-canary` },
    ],
    edges: [
      { source: "httproute", target: "deploy_baseline" },
      { source: "httproute", target: "deploy_canary" },
    ],
  };
}
