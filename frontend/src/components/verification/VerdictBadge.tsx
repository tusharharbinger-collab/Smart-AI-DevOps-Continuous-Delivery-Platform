import { Badge } from "@/components/ui/badge";

const STATUS_VARIANT: Record<string, "success" | "warning" | "destructive" | "secondary"> = {
  HEALTHY: "success",
  DEGRADED: "warning",
  FAILED: "destructive",
  UNVERIFIABLE: "secondary",
};

export function VerdictBadge({
  status,
  confidence,
  small = false,
}: {
  status: string;
  confidence?: number;
  small?: boolean;
}) {
  return (
    <Badge variant={STATUS_VARIANT[status] ?? "secondary"} className={small ? "text-[10px]" : "px-3 py-1 text-sm"}>
      {status}
      {confidence !== undefined && <span className="ml-1 opacity-80">({(confidence * 100).toFixed(0)}%)</span>}
    </Badge>
  );
}
