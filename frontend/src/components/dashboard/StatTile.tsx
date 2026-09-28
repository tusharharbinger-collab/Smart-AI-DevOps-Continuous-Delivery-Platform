/**
 * frontend/src/components/dashboard/StatTile.tsx
 *
 * Extracted from the Reports tab redesign (2026-09-28) so Cost tab gets the same treatment (color-graded by
 * real thresholds, an optional real-data sparkline) instead of duplicating it — one stat-tile look across
 * the delivery-ops console, matching current dashboard practice: sparklines show trend without a full chart,
 * and color communicates status rather than decorating it.
 */
import { AreaChart, Area, ResponsiveContainer, YAxis } from "recharts";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";

export type Health = "good" | "warn" | "bad" | "neutral";

const HEALTH_STYLE: Record<Health, { text: string; ring: string; icon: string; spark: string }> = {
  good: { text: "text-success", ring: "ring-success/15", icon: "bg-success/10 text-success", spark: "hsl(var(--success))" },
  warn: { text: "text-warning", ring: "ring-warning/15", icon: "bg-warning/10 text-warning", spark: "hsl(var(--warning))" },
  bad: { text: "text-destructive", ring: "ring-destructive/15", icon: "bg-destructive/10 text-destructive", spark: "hsl(var(--destructive))" },
  neutral: { text: "text-foreground", ring: "ring-border", icon: "bg-muted text-muted-foreground", spark: "hsl(var(--muted-foreground))" },
};

/** A tiny trend line with no axes/legend — shows shape, not precision. Real data only, oldest to newest. */
export function Sparkline({ points, color }: { points: number[]; color: string }) {
  if (points.length < 2) return null;
  const data = points.map((v, i) => ({ i, v }));
  return (
    <ResponsiveContainer width="100%" height={28}>
      <AreaChart data={data} margin={{ top: 2, right: 0, left: 0, bottom: 0 }}>
        <YAxis hide domain={["dataMin", "dataMax"]} />
        <defs>
          <linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity={0.35} />
            <stop offset="100%" stopColor={color} stopOpacity={0} />
          </linearGradient>
        </defs>
        <Area type="monotone" dataKey="v" stroke={color} strokeWidth={1.5} fill="url(#sparkFill)" dot={false} isAnimationActive={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export function StatTile({
  label, value, sub, icon: Icon, health = "neutral", sparkline,
}: {
  label: string;
  value: string;
  sub?: string;
  icon: React.ComponentType<{ className?: string }>;
  health?: Health;
  sparkline?: number[];
}) {
  const style = HEALTH_STYLE[health];
  return (
    <Card className={cn("ring-1", style.ring)}>
      <CardContent className="p-4">
        <div className="flex items-start justify-between">
          <div>
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</span>
            <div className={cn("text-stat text-2xl font-semibold", style.text)}>{value}</div>
            {sub && <span className="text-code text-[11px] text-muted-foreground">{sub}</span>}
          </div>
          <div className={cn("rounded-md p-1.5", style.icon)}>
            <Icon className="h-4 w-4" />
          </div>
        </div>
        {sparkline && sparkline.length >= 2 && (
          <div className="mt-2 -mb-1">
            <Sparkline points={sparkline} color={style.spark} />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
