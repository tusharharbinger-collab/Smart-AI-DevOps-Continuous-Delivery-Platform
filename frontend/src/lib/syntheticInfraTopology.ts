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

/** Appends the real live version tag to a node label, e.g. "myapp-baseline (v1.0.0)" — omitted entirely
 * when no version is known yet (onboarding preview, before any real rollout has run). */
function withVersion(label: string, version?: string | null): string {
  return version ? `${label} (${version})` : label;
}

export function synthesizeRealInfraTopology(
  projectName: string,
  deployTarget: string | undefined,
  baselineVersion?: string | null,
  canaryVersion?: string | null,
): InfraTopology {
  const service = slugify(projectName);

  if (deployTarget === "aws_ecs") {
    return {
      nodes: [
        { id: "alb", type: "alb", label: "Shared ALB (smartcd-platform-alb)" },
        { id: "ecs_baseline", type: "ecs_service", label: withVersion(`${service}-baseline`, baselineVersion) },
        { id: "ecs_canary", type: "ecs_service", label: withVersion(`${service}-canary`, canaryVersion) },
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
      { id: "deploy_baseline", type: "deployment", label: withVersion(`${service}-baseline`, baselineVersion) },
      { id: "deploy_canary", type: "deployment", label: withVersion(`${service}-canary`, canaryVersion) },
    ],
    edges: [
      { source: "httproute", target: "deploy_baseline" },
      { source: "httproute", target: "deploy_canary" },
    ],
  };
}

/**
 * Real gap found live: an AI-generated infra draft's topology only ever contains the EXTRAS the app needs
 * beyond the platform's standard setup (a single collapsed "app" node standing in for everything the
 * platform already provides, plus e.g. an S3 bucket) — see infra_generator.py's "EXTRAS-ONLY MODE"
 * instruction: the model is explicitly forbidden from drawing the ALB/ECS services itself, since the
 * platform builds those, not the AI. `PipelineDashboard.tsx` used to treat the AI topology as the WHOLE
 * picture and show it alone, so a project with a linked draft displayed only "Your app" + "S3 Bucket" —
 * the real ALB/baseline/canary services it's actually running on had simply vanished from the graph.
 * This grafts the AI draft's real extras onto the ALWAYS-real synthesized platform topology instead of
 * replacing it, so the graph shows the complete real picture either way: platform infra + AI-added extras.
 */
export function mergeInfraTopologies(platform: InfraTopology, aiTopology: InfraTopology | null): InfraTopology {
  if (!aiTopology) return platform;

  const appPlaceholder = aiTopology.nodes.find((n) => n.type === "app");
  const extraNodes = aiTopology.nodes.filter((n) => n.type !== "app");
  if (extraNodes.length === 0) return platform;

  // The AI's own compute-standin edges point FROM its "app" placeholder — rewire those onto the real
  // platform's baseline node (what a rollout actually patches on graduation) so the extras hang off the
  // real infra instead of a node that no longer exists in the merged graph.
  const computeNodeId = platform.nodes.find((n) => n.id.includes("baseline"))?.id ?? platform.nodes[0]?.id;
  const extraEdges = aiTopology.edges.map((e) => ({
    source: e.source === appPlaceholder?.id ? computeNodeId : e.source,
    target: e.target === appPlaceholder?.id ? computeNodeId : e.target,
  }));

  return {
    nodes: [...platform.nodes, ...extraNodes],
    edges: [...platform.edges, ...extraEdges],
  };
}
