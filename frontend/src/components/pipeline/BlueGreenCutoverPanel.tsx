/**
 * frontend/src/components/pipeline/BlueGreenCutoverPanel.tsx
 *
 * Backlog #5 - the blue-green cutover view for the Pipeline View tab: the five real phases of the cutover
 * (green starting -> health check -> traffic switch -> live-URL check -> graduation) and where traffic
 * actually is, both derived from the real log lines (lib/blueGreenCutover.ts) - never a fabricated number.
 * Only rendered for a project whose deploy mode is blue_green.
 */
import { CheckCircle2, Circle, Loader2, MinusCircle, XCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import {
  deriveBlueGreenCutover, type BlueGreenOutcome, type BlueGreenPhaseStatus,
} from "@/lib/blueGreenCutover";

const PHASE_ICON: Record<BlueGreenPhaseStatus, React.ReactNode> = {
  pending: <Circle className="h-4 w-4 text-muted-foreground/50" />,
  active: <Loader2 className="h-4 w-4 animate-spin text-primary" />,
  done: <CheckCircle2 className="h-4 w-4 text-success" />,
  failed: <XCircle className="h-4 w-4 text-destructive" />,
  skipped: <MinusCircle className="h-4 w-4 text-muted-foreground/50" />,
};

const OUTCOME: Record<BlueGreenOutcome, { label: string; variant: "default" | "success" | "destructive" | "secondary" }> = {
  waiting: { label: "Waiting for the cutover to start", variant: "secondary" },
  in_progress: { label: "Cutover in progress", variant: "default" },
  live: { label: "Live and verified", variant: "success" },
  rolled_back: { label: "Rolled back to the previous version", variant: "destructive" },
  failed: { label: "Failed", variant: "destructive" },
};

function Lane({ name, sub, percent, tone }: { name: string; sub: string; percent: number; tone: "blue" | "green" }) {
  return (
    <div className="flex-1 rounded-md border p-3" data-testid={`bg-lane-${tone}`}>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-sm font-medium">{name}</span>
        <span className="text-code text-xs text-muted-foreground" data-testid={`bg-traffic-${tone}`}>{percent}% of traffic</span>
      </div>
      <p className="mt-0.5 text-[11px] text-muted-foreground">{sub}</p>
      <div className="mt-2 h-1.5 overflow-hidden rounded bg-muted">
        <div
          className={cn("h-full rounded transition-all duration-500", tone === "blue" ? "bg-sky-500" : "bg-emerald-500")}
          style={{ width: `${percent}%` }}
        />
      </div>
    </div>
  );
}

export function BlueGreenCutoverPanel({ logLines, status }: { logLines: string[]; status?: string }) {
  const state = deriveBlueGreenCutover(logLines, status);
  const outcome = OUTCOME[state.outcome];

  return (
    <Card data-testid="blue-green-panel">
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle>Blue-green cutover</CardTitle>
        <Badge variant={outcome.variant}>{outcome.label}</Badge>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-col gap-3 sm:flex-row">
          <Lane
            name="Blue — baseline"
            sub={state.blueRuns === "new" ? "Now runs the new version" : "The previous, currently-live version"}
            percent={state.traffic.blue}
            tone="blue"
          />
          <Lane
            name="Green — new version"
            sub={state.traffic.green > 0 ? "Serving all traffic" : "Started, stabilizing and health-checked before any traffic"}
            percent={state.traffic.green}
            tone="green"
          />
        </div>

        <ol className="grid gap-2 sm:grid-cols-5">
          {state.phases.map((p, i) => (
            <li
              key={p.id}
              data-testid={`bg-phase-${p.id}`}
              data-status={p.status}
              className={cn(
                "rounded-md border p-2 text-xs",
                p.status === "active" && "border-primary/50 bg-primary/5",
                p.status === "failed" && "border-destructive/50 bg-destructive/5"
              )}
            >
              <div className="flex items-center gap-1.5">
                {PHASE_ICON[p.status]}
                <span className={cn("font-medium", p.status === "pending" || p.status === "skipped" ? "text-muted-foreground" : "")}>
                  {i + 1}. {p.label}
                </span>
              </div>
              <p className="mt-1 text-[11px] text-muted-foreground">
                {p.status === "skipped" ? "Skipped — rolled back instead" : p.detail}
              </p>
            </li>
          ))}
        </ol>
      </CardContent>
    </Card>
  );
}
