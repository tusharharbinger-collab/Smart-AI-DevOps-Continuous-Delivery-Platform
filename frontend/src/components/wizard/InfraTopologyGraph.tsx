/**
 * frontend/src/components/wizard/InfraTopologyGraph.tsx
 *
 * Phase 5 (AI_AGENTIC_ORCHESTRATION_PLAN.md §5/§R.7) — renders the real
 * AI-generated infra topology (services/explainability-service/src/
 * infra_generator.py's output, via POST /infra-drafts) as an actual
 * node/edge graph, not a linear pill row. React Flow only renders at given
 * coordinates; it has no layout engine of its own, so `dagre` computes an
 * automatic top-down layout — including the documented gotcha this
 * research flagged: dagre returns each node's CENTER point, while React
 * Flow positions from the node's TOP-LEFT corner, so every node's position
 * is converted before being handed to React Flow.
 *
 * Read-only by design (§5A: the sprint scope is a visualization, not a
 * drag-and-drop editor) — no onNodesChange/onEdgesChange wiring.
 */
import { useMemo } from "react";
import dagre from "dagre";
import {
  ReactFlow, Background, BackgroundVariant, Controls, MarkerType, type Edge, type Node, Position,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { flowNodeTypes, infraAccentFor, TINT_HEX, type FlowCardData } from "@/components/pipeline/FlowNode";
import type { InfraTopology } from "@/api/infraDrafts";

const NODE_WIDTH = 190;
const NODE_HEIGHT = 60;

/**
 * dagre computes layout in its own graph object, then we read positions
 * back off it — this is the standard React Flow + dagre integration
 * pattern (no maintained alternative avoids this two-step shape).
 */
function layoutWithDagre(topology: InfraTopology): { nodes: Node[]; edges: Edge[] } {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "TB", nodesep: 40, ranksep: 60 });

  for (const n of topology.nodes) {
    g.setNode(n.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const e of topology.edges) {
    g.setEdge(e.source, e.target);
  }
  dagre.layout(g);

  const nodes: Node[] = topology.nodes.map((n) => {
    const pos = g.node(n.id);
    const { icon, tint } = infraAccentFor(n.type);
    return {
      id: n.id,
      type: "card",
      // dagre gives the CENTER of the node; React Flow positions from the
      // top-left corner — convert or every node renders offset from where
      // dagre actually intended it.
      position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 },
      data: { icon, title: n.label, subtitle: n.type, tint } satisfies FlowCardData,
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
      style: { width: NODE_WIDTH },
    };
  });

  const edges: Edge[] = topology.edges.map((e) => {
    const color = TINT_HEX[infraAccentFor(topology.nodes.find((n) => n.id === e.source)?.type ?? "").tint];
    return {
      id: `${e.source}-${e.target}`,
      source: e.source,
      target: e.target,
      animated: true,
      style: { stroke: color },
      markerEnd: { type: MarkerType.ArrowClosed, color },
    };
  });

  return { nodes, edges };
}

export interface InfraTopologyGraphProps {
  topology: InfraTopology;
}

export function InfraTopologyGraph({ topology }: InfraTopologyGraphProps) {
  const { nodes, edges } = useMemo(() => layoutWithDagre(topology), [topology]);

  if (topology.nodes.length === 0) {
    return <p className="text-sm text-muted-foreground">No infra nodes in this proposal.</p>;
  }

  return (
    <div style={{ height: Math.max(220, topology.nodes.length * 90) }} className="flow-canvas overflow-hidden rounded-xl border">
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
