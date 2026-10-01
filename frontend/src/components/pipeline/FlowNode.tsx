/**
 * frontend/src/components/pipeline/FlowNode.tsx
 *
 * Visual-only redesign of the infra/pipeline React Flow graphs (InfraTopologyGraph.tsx,
 * ProjectDeliveryGraph.tsx) — the flat "border + plain background" boxes they used to render read like a
 * database ER diagram, not a delivery pipeline. This gives every node an n8n/Make.com-style card: a
 * colored icon badge keyed to what kind of real resource it is, a soft tinted glow, and a pulsing ring for
 * whichever one is actively running right now. Purely presentational — no behavior, data shape, layout
 * algorithm, or interactivity change; still read-only (no onNodesChange/onEdgesChange), still positioned by
 * the exact same dagre output each caller already computes.
 */
import { Boxes, Database, Globe, HardDrive, Layers, Server, Zap, type LucideIcon } from "lucide-react";
import { Handle, Position, type NodeProps } from "@xyflow/react";

/** Category accents — deliberately distinct from the app's semantic tokens (primary/success/destructive
 * are reserved for real pass/fail/active state); these are purely "what kind of resource is this". */
export type NodeTint = "sky" | "violet" | "emerald" | "amber" | "pink" | "slate" | "primary" | "success" | "destructive" | "muted";

const TINT_CLASSES: Record<NodeTint, { badge: string; ring: string; glow: string; border: string }> = {
  sky: { badge: "bg-sky-500/15 text-sky-400", ring: "ring-sky-400/40", glow: "shadow-sky-500/20", border: "border-sky-500/30" },
  violet: { badge: "bg-violet-500/15 text-violet-400", ring: "ring-violet-400/40", glow: "shadow-violet-500/20", border: "border-violet-500/30" },
  emerald: { badge: "bg-emerald-500/15 text-emerald-400", ring: "ring-emerald-400/40", glow: "shadow-emerald-500/20", border: "border-emerald-500/30" },
  amber: { badge: "bg-amber-500/15 text-amber-400", ring: "ring-amber-400/40", glow: "shadow-amber-500/20", border: "border-amber-500/30" },
  pink: { badge: "bg-pink-500/15 text-pink-400", ring: "ring-pink-400/40", glow: "shadow-pink-500/20", border: "border-pink-500/30" },
  slate: { badge: "bg-slate-500/15 text-slate-400", ring: "ring-slate-400/40", glow: "shadow-slate-500/10", border: "border-slate-500/30" },
  primary: { badge: "bg-primary/15 text-primary", ring: "ring-primary/40", glow: "shadow-primary/25", border: "border-primary/40" },
  success: { badge: "bg-success/15 text-success", ring: "ring-success/40", glow: "shadow-success/25", border: "border-success/40" },
  destructive: { badge: "bg-destructive/15 text-destructive", ring: "ring-destructive/40", glow: "shadow-destructive/25", border: "border-destructive/40" },
  muted: { badge: "bg-muted text-muted-foreground", ring: "ring-border", glow: "shadow-none", border: "border-border" },
};

/** Hex equivalents for the same tints, for edge strokes/markers (SVG can't consume Tailwind classes). */
export const TINT_HEX: Record<NodeTint, string> = {
  sky: "#38bdf8", violet: "#a78bfa", emerald: "#34d399", amber: "#fbbf24", pink: "#f472b6",
  slate: "#94a3b8", primary: "#6366f1", success: "#22c55e", destructive: "#ef4444", muted: "#94a3b8",
};

export interface FlowCardData {
  icon: LucideIcon;
  title: string;
  subtitle: string;
  tint: NodeTint;
  /** Currently executing — gets a pulsing ring, same visual language as StageTimeline's active row. */
  pulse?: boolean;
  [key: string]: unknown;
}

/**
 * Custom React Flow node ("card" in nodeTypes) — replaces the old inline `data.label` JSX + flat
 * `style={{border, background}}` every caller used to build by hand. Handles are invisible (kept for React
 * Flow's edge anchoring math) since these graphs are read-only — nothing to click-and-drag a connection from.
 */
export function FlowCardNode({ data }: NodeProps) {
  const { icon: Icon, title, subtitle, tint, pulse } = data as unknown as FlowCardData;
  const t = TINT_CLASSES[tint];

  return (
    <div
      className={`group flex items-center gap-2.5 rounded-xl border ${t.border} bg-card/90 px-3 py-2.5 shadow-lg ${t.glow} backdrop-blur-sm transition-transform hover:scale-[1.02]`}
    >
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <div className={`relative flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${t.badge}`}>
        {pulse && <span className={`absolute inset-0 rounded-lg ring-2 ${t.ring} animate-ping`} />}
        <Icon className="h-4 w-4" />
      </div>
      <div className="min-w-0">
        <div className="truncate text-xs font-semibold text-foreground">{title}</div>
        <div className="truncate text-[10px] uppercase tracking-wide text-muted-foreground">{subtitle}</div>
      </div>
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  );
}

export const flowNodeTypes = { card: FlowCardNode };

/**
 * Maps a real infra resource type (the backend's naming varies — `alb`, `httproute`, `ecs_service`,
 * `deployment`, `rds`, `aws_rds_instance`, `elasticache`, `aws_elasticache_cluster`, `s3`,
 * `aws_s3_bucket`, `app`, …) to an icon + category tint, by substring so every real variant this platform's
 * generators actually emit resolves to the right category without needing an exhaustive exact-match table.
 */
export function infraAccentFor(type: string): { icon: LucideIcon; tint: NodeTint } {
  const t = type.toLowerCase();
  if (t.includes("alb") || t.includes("route") || t.includes("gateway") || t.includes("load")) return { icon: Globe, tint: "sky" };
  if (t.includes("s3") || t.includes("bucket") || t.includes("storage")) return { icon: HardDrive, tint: "pink" };
  if (t.includes("rds") || t.includes("database") || t === "db") return { icon: Database, tint: "emerald" };
  if (t.includes("cache") || t.includes("redis")) return { icon: Zap, tint: "amber" };
  if (t.includes("ecs") || t.includes("deployment") || t.includes("service") || t.includes("fargate")) return { icon: Server, tint: "violet" };
  if (t === "app") return { icon: Boxes, tint: "primary" };
  return { icon: Layers, tint: "slate" };
}
