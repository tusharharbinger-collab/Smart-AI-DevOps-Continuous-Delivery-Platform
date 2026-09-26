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
  ReactFlow, Background, Controls, type Edge, type Node, Position,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { CheckCircle2, CircleDot, Database, GitBranch, Globe, HardDrive, Layers, Server, XCircle, Zap } from "lucide-react";
import type { InfraTopology } from "@/api/infraDrafts";
import type { StageRowStatus } from "@/lib/pipelineStageSteps";

const NODE_WIDTH = 190;
const NODE_HEIGHT = 56;

const INFRA_TYPE_ICON: Record<string, typeof Server> = {
  alb: Globe,
  httproute: Globe,
  ecs_service: Server,
  deployment: Server,
  rds: Database,
  elasticache: Zap,
  s3: HardDrive,
};

function infraIconFor(type: string) {
  const Icon = INFRA_TYPE_ICON[type.toLowerCase()] ?? Layers;
  return <Icon className="h-3.5 w-3.5" />;
}

const STAGE_ROW_ICON: Record<StageRowStatus, typeof CheckCircle2> = {
  pending: GitBranch,
  active: CircleDot,
  done: CheckCircle2,
  failed: XCircle,
};

const STAGE_ROW_COLOR: Record<StageRowStatus, string> = {
  pending: "var(--muted-foreground, #888)",
  active: "var(--primary, #6366f1)",
  done: "var(--success, #22c55e)",
  failed: "var(--destructive, #ef4444)",
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
      return {
        id: `infra:${n.id}`,
        position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 },
        data: {
          label: (
            <div className="flex items-center gap-2 text-xs">
              {infraIconFor(n.type)}
              <div>
                <div className="font-medium">{n.label}</div>
                <div className="text-[10px] uppercase text-muted-foreground">{n.type}</div>
              </div>
            </div>
          ),
        },
        sourcePosition: Position.Bottom,
        targetPosition: Position.Top,
        style: {
          width: NODE_WIDTH,
          border: "1px solid var(--border, #333)",
          borderRadius: 8,
          padding: 8,
          background: "var(--card, #1a1a1a)",
        },
      };
    }),
    ...stages.map((stageName) => {
      const pos = g.node(`stage:${stageName}`);
      const rowStatus = stageRowStatus(stageName, stages, currentStage, status);
      const Icon = STAGE_ROW_ICON[rowStatus];
      const color = STAGE_ROW_COLOR[rowStatus];
      return {
        id: `stage:${stageName}`,
        position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 },
        data: {
          label: (
            <div className="flex items-center gap-2 text-xs">
              <Icon className={rowStatus === "active" ? "h-3.5 w-3.5 animate-pulse" : "h-3.5 w-3.5"} style={{ color }} />
              <div>
                <div className="font-medium" style={{ color: rowStatus === "pending" ? undefined : color }}>
                  {stageName}
                </div>
                <div className="text-[10px] uppercase text-muted-foreground">{rowStatus}</div>
              </div>
            </div>
          ),
        },
        sourcePosition: Position.Bottom,
        targetPosition: Position.Top,
        style: {
          width: NODE_WIDTH,
          border: `1px solid ${rowStatus === "pending" ? "var(--border, #333)" : color}`,
          borderRadius: 8,
          padding: 8,
          background: "var(--card, #1a1a1a)",
        },
      };
    }),
  ];

  const edges: Edge[] = [
    ...infraEdges.map((e) => ({
      id: `infra:${e.source}-infra:${e.target}`,
      source: `infra:${e.source}`,
      target: `infra:${e.target}`,
      animated: false,
    })),
    ...stages.slice(0, -1).map((stageName, i) => ({
      id: `stage:${stageName}-stage:${stages[i + 1]}`,
      source: `stage:${stageName}`,
      target: `stage:${stages[i + 1]}`,
      animated: stageRowStatus(stages[i + 1], stages, currentStage, status) === "active",
    })),
  ];
  if (infraNodes.length > 0 && stages.length > 0) {
    const computeNode = findComputeNode();
    edges.push({
      id: `infra:${computeNode.id}-stage:${stages[0]}`,
      source: `infra:${computeNode.id}`,
      target: `stage:${stages[0]}`,
      animated: false,
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
    <div style={{ height: Math.max(260, nodes.length * 90) }} className="rounded-md border">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        nodesDraggable={false}
        nodesConnectable={false}
        edgesFocusable={false}
        elementsSelectable={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={16} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
