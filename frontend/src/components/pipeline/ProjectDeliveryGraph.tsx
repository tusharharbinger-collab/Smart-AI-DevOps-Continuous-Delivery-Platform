/**
 * frontend/src/components/pipeline/ProjectDeliveryGraph.tsx
 *
 * n8n-style visualization for the ACTUAL persistent project view (not the
 * onboarding wizard) — the gap flagged live: InfraTopologyGraph.tsx only
 * ever rendered inside RequirementsForm during onboarding, and was thrown
 * away the moment the wizard advanced. This component renders ONE connected
 * graph combining:
 *   1. The real infra topology this project was provisioned from (fetched
 *      via getProjectInfraDraft() — null for a project that never went
 *      through the Requirements Form / infra-draft flow), and
 *   2. The real, LIVE pipeline stage chain for the currently viewed run
 *      (the same `stages`/`currentStage`/`status` StageTimeline.tsx already
 *      renders as a vertical list) as a connected chain of nodes flowing out
 *      of the infra graph's compute node.
 *
 * Reuses the dagre auto-layout pattern from InfraTopologyGraph.tsx
 * (dagre gives node CENTER, React Flow positions from the TOP-LEFT corner —
 * every node position is converted before being handed to React Flow).
 * Read-only, same as InfraTopologyGraph: no onNodesChange/onEdgesChange.
 */
import { useMemo } from "react";
import dagre from "dagre";
import {
  ReactFlow, Background, BackgroundVariant, Controls, MarkerType, type Edge, type Node, Position,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { CheckCircle2, CircleDot, GitBranch, XCircle } from "lucide-react";
import { flowNodeTypes, infraAccentFor, TINT_HEX, type FlowCardData, type NodeTint } from "@/components/pipeline/FlowNode";
import type { InfraTopology } from "@/api/infraDrafts";
import type { StageRowStatus } from "@/lib/pipelineStageSteps";

const NODE_WIDTH = 190;
const NODE_HEIGHT = 60;

const STAGE_ROW_ICON: Record<StageRowStatus, typeof CheckCircle2> = {
  pending: GitBranch,
  active: CircleDot,
  done: CheckCircle2,
  failed: XCircle,
};

/** Stage status maps onto the same tint vocabulary the infra nodes use, so "active"/"done"/"failed" read as
 * consistently colored states across the whole connected graph, not a second unrelated color language. */
const STAGE_ROW_TINT: Record<StageRowStatus, NodeTint> = {
  pending: "muted",
  active: "primary",
  done: "success",
  failed: "destructive",
};

export interface ProjectDeliveryGraphProps {
  /** Null for a project created without ever going through the infra-draft flow. */
  infraTopology: InfraTopology | null;
  stages?: string[];
  currentStage?: string;
  status?: string;
}

/**
 * Real bug class to avoid: stages/currentStage/status use the exact same
 * pending/active/done/failed derivation as StageTimeline.tsx so the two
 * views of the same run never disagree about where it currently is.
 */
function stageRowStatus(stageName: string, stages: string[], currentStage?: string, status?: string): StageRowStatus {
  const currentIndex = stages.indexOf(currentStage ?? "");
  const i = stages.indexOf(stageName);
  const isCurrent = stageName === currentStage;
  const isDone = currentIndex >= 0 && i < currentIndex;
  if (isCurrent && status === "FAILED") return "failed";
  if (isCurrent) return "active";
  if (isDone) return "done";
  return "pending";
}

function buildGraph(
  infraTopology: InfraTopology | null,
  stages: string[],
  currentStage?: string,
  status?: string,
): { nodes: Node[]; edges: Edge[] } {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "TB", nodesep: 40, ranksep: 60 });

  const infraNodes = infraTopology?.nodes ?? [];
  const infraEdges = infraTopology?.edges ?? [];

  for (const n of infraNodes) {
    g.setNode(`infra:${n.id}`, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const e of infraEdges) {
    g.setEdge(`infra:${e.source}`, `infra:${e.target}`);
  }

  for (const stageName of stages) {
    g.setNode(`stage:${stageName}`, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (let i = 0; i < stages.length - 1; i++) {
    g.setEdge(`stage:${stages[i]}`, `stage:${stages[i + 1]}`);
  }

  // Connect the infra graph's compute node — prefer the baseline service/
  // deployment (what a rollout actually patches on graduation), falling
  // back to any ecs_service/deployment-typed node, then the last infra
  // node — into the pipeline's first stage. This is the one edge that
  // actually makes it ONE connected diagram rather than two unrelated
  // graphs stacked in the same canvas.
  const findComputeNode = () =>
    infraNodes.find((n) => n.id.includes("baseline")) ??
    infraNodes.find((n) => ["ecs_service", "deployment"].includes(n.type.toLowerCase())) ??
    infraNodes[infraNodes.length - 1];
  if (infraNodes.length > 0 && stages.length > 0) {
    g.setEdge(`infra:${findComputeNode().id}`, `stage:${stages[0]}`);
  }

  dagre.layout(g);

  const nodes: Node[] = [
    ...infraNodes.map((n) => {
      const pos = g.node(`infra:${n.id}`);
      const { icon, tint } = infraAccentFor(n.type);
      return {
        id: `infra:${n.id}`,
        type: "card",
        position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 },
        data: { icon, title: n.label, subtitle: n.type, tint } satisfies FlowCardData,
        sourcePosition: Position.Bottom,
        targetPosition: Position.Top,
        style: { width: NODE_WIDTH },
      };
    }),
    ...stages.map((stageName) => {
      const pos = g.node(`stage:${stageName}`);
      const rowStatus = stageRowStatus(stageName, stages, currentStage, status);
      return {
        id: `stage:${stageName}`,
        type: "card",
        position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 },
        data: {
          icon: STAGE_ROW_ICON[rowStatus], title: stageName, subtitle: rowStatus,
          tint: STAGE_ROW_TINT[rowStatus], pulse: rowStatus === "active",
        } satisfies FlowCardData,
        sourcePosition: Position.Bottom,
        targetPosition: Position.Top,
        style: { width: NODE_WIDTH },
      };
    }),
  ];

  const edges: Edge[] = [
    ...infraEdges.map((e) => {
      const color = TINT_HEX[infraAccentFor(infraNodes.find((n) => n.id === e.source)?.type ?? "").tint];
      return {
        id: `infra:${e.source}-infra:${e.target}`,
        source: `infra:${e.source}`,
        target: `infra:${e.target}`,
        animated: true,
        style: { stroke: color },
        markerEnd: { type: MarkerType.ArrowClosed, color },
      };
    }),
    ...stages.slice(0, -1).map((stageName, i) => {
      const nextStatus = stageRowStatus(stages[i + 1], stages, currentStage, status);
      const color = TINT_HEX[STAGE_ROW_TINT[nextStatus]];
      return {
        id: `stage:${stageName}-stage:${stages[i + 1]}`,
        source: `stage:${stageName}`,
        target: `stage:${stages[i + 1]}`,
        animated: nextStatus === "active",
        style: { stroke: color },
        markerEnd: { type: MarkerType.ArrowClosed, color },
      };
    }),
  ];
  if (infraNodes.length > 0 && stages.length > 0) {
    const computeNode = findComputeNode();
    const color = TINT_HEX[infraAccentFor(computeNode.type).tint];
    edges.push({
      id: `infra:${computeNode.id}-stage:${stages[0]}`,
      source: `infra:${computeNode.id}`,
      target: `stage:${stages[0]}`,
      animated: true,
      style: { stroke: color },
      markerEnd: { type: MarkerType.ArrowClosed, color },
    });
  }

  return { nodes, edges };
}

export function ProjectDeliveryGraph({ infraTopology, stages, currentStage, status }: ProjectDeliveryGraphProps) {
  const stageList = stages ?? [];
  const { nodes, edges } = useMemo(
    () => buildGraph(infraTopology, stageList, currentStage, status),
    // stageList is a fresh array each render, so key on its content, not identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [infraTopology, stageList.join(","), currentStage, status],
  );

  if (nodes.length === 0) {
    return <p className="text-sm text-muted-foreground">Waiting for pipeline DAG…</p>;
  }

  return (
    <div style={{ height: Math.max(260, nodes.length * 90) }} className="flow-canvas overflow-hidden rounded-xl border">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={flowNodeTypes}
        fitView
        nodesDraggable={false}
        nodesConnectable={false}
        edgesFocusable={false}
        elementsSelectable={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={18} size={1.2} className="opacity-40" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
