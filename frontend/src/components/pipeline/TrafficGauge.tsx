import { Badge } from "@/components/ui/badge";

const STATUS_VARIANT: Record<string, "default" | "success" | "destructive" | "secondary" | "warning"> = {
  RUNNING: "default",
  COMPLETED: "success",
  FAILED: "destructive",
  PAUSED: "warning",
  ROLLED_BACK: "destructive",
  AWAITING_APPROVAL: "warning",
};

export function TrafficGauge({ weight, status }: { weight: number; status: string }) {
  return (
    <div className="mt-4">
      <div className="mb-1 flex items-center justify-between text-xs">
        <span className="text-muted-foreground">Canary traffic weight</span>
        <span className="flex items-center gap-2 font-medium">
          {weight}%
          <Badge variant={STATUS_VARIANT[status] ?? "secondary"}>{status}</Badge>
        </span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
        <div
          className="h-2 rounded-full bg-primary transition-all duration-500"
          style={{ width: `${Math.min(100, Math.max(0, weight))}%` }}
        />
      </div>
    </div>
  );
}
