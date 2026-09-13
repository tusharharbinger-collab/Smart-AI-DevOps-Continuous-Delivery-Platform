/**
 * frontend/src/pages/AuditLedger.tsx — Screen 4 + Report 4 (SOC 2 export).
 */
import { Fragment, useMemo, useState } from "react";
import { toast } from "sonner";
import { ArrowUpDown, ChevronDown, ChevronRight, Copy, Download, Lock, History, ShieldCheck } from "lucide-react";
import { useAuditLog } from "@/hooks/useAuditLog";
import { useAppContext } from "@/hooks/useAppContext";
import { exportSOC2, type AuditEntry } from "@/api/audit";
import { VerdictBadge } from "@/components/verification/VerdictBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableHeader, TableBody, TableHead, TableRow, TableCell } from "@/components/ui/table";

type SortKey = keyof Pick<AuditEntry, "timestamp" | "action" | "confidence">;

function copyToClipboard(text: string, label: string) {
  navigator.clipboard.writeText(text);
  toast.success(`Copied ${label}`);
}

export function AuditLedger() {
  const { tenantId, projectId } = useAppContext();
  const { entries, isLoading } = useAuditLog(tenantId, projectId);
  const [sortKey, setSortKey] = useState<SortKey>("timestamp");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [exporting, setExporting] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const sorted = useMemo(() => {
    const copy = [...entries];
    copy.sort((a, b) => {
      const av = a[sortKey] ?? "";
      const bv = b[sortKey] ?? "";
      const cmp = av > bv ? 1 : av < bv ? -1 : 0;
      return sortDir === "asc" ? cmp : -cmp;
    });
    return copy;
  }, [entries, sortKey, sortDir]);

  const stats = useMemo(() => {
    // Matches the exact action strings actuation_executor.py's
    // record_actuation() writes ("WEIGHT_UPDATE" / "ROLLBACK") — a prior
    // version of this matched on `.includes("promot")`, which never
    // matched "WEIGHT_UPDATE" and silently reported 0 promotions against
    // a real, non-empty ledger. Found live against the running backend.
    const rollbacks = entries.filter((e) => e.action.toUpperCase() === "ROLLBACK").length;
    const promotions = entries.filter((e) => e.action.toUpperCase() === "WEIGHT_UPDATE").length;
    const latest = [...entries].sort((a, b) => b.timestamp.localeCompare(a.timestamp))[0];
    return { total: entries.length, rollbacks, promotions, latest };
  }, [entries]);

  function toggleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  function toggleRow(id: string) {
    setExpanded((prev) => ({ ...prev, [id]: !prev[id] }));
  }

  async function handleExport() {
    setExporting(true);
    try {
      await exportSOC2(tenantId);
      toast.success("SOC 2 export downloaded");
    } catch (err) {
      toast.error("Export failed", { description: (err as Error).message });
    } finally {
      setExporting(false);
    }
  }

  if (isLoading) return <Skeleton className="h-96" />;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Card>
          <CardContent className="flex items-center justify-between p-4">
            <div>
              <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Total actions</span>
              <div className="text-stat text-xl">{stats.total}</div>
              <span className="text-code text-[11px] text-primary">
                {stats.promotions} promotions · {stats.rollbacks} rollbacks
              </span>
            </div>
            <History className="h-6 w-6 text-muted-foreground/50" />
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center justify-between p-4">
            <div>
              <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Signature scheme</span>
              <div className="text-stat text-xl">HMAC-SHA256</div>
              <span className="text-code text-[11px] text-success">Verified before actuation</span>
            </div>
            <Lock className="h-6 w-6 text-muted-foreground/50" />
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center justify-between p-4">
            <div>
              <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Most recent entry</span>
              <div className="text-stat text-xl">
                {stats.latest ? new Date(stats.latest.timestamp).toLocaleTimeString() : "—"}
              </div>
              <span className="text-code text-[11px] text-muted-foreground">
                {stats.latest ? stats.latest.action : "no entries yet"}
              </span>
            </div>
            <ShieldCheck className="h-6 w-6 text-muted-foreground/50" />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardContent className="p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold">Audit Ledger</h2>
            {/* The export endpoint is tenant-wide. Inside a project workspace
                the table above is project-scoped, so the label says plainly
                that the download is broader than what's on screen rather
                than quietly exporting other projects' rows under a label
                that implies otherwise. */}
            <Button size="sm" variant="outline" onClick={handleExport} disabled={exporting}>
              <Download className="h-3.5 w-3.5" />
              {exporting ? "Exporting…" : projectId ? "Export SOC 2 CSV (all projects)" : "Export SOC 2 CSV"}
            </Button>
          </div>

          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8" />
                <TableHead onClick={() => toggleSort("timestamp")}>
                  <span className="inline-flex items-center gap-1">Time <ArrowUpDown className="h-3 w-3" /></span>
                </TableHead>
                <TableHead onClick={() => toggleSort("action")}>
                  <span className="inline-flex items-center gap-1">Action <ArrowUpDown className="h-3 w-3" /></span>
                </TableHead>
                <TableHead>Run</TableHead>
                <TableHead>Verdict</TableHead>
                <TableHead onClick={() => toggleSort("confidence")}>
                  <span className="inline-flex items-center gap-1">Confidence <ArrowUpDown className="h-3 w-3" /></span>
                </TableHead>
                <TableHead>Authorized By</TableHead>
                <TableHead>HMAC</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sorted.map((e) => {
                const isOpen = expanded[e.actuation_id];
                return (
                  <Fragment key={e.actuation_id}>
                    <TableRow className="cursor-pointer" onClick={() => toggleRow(e.actuation_id)}>
                      <TableCell className="text-muted-foreground">
                        {isOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                      </TableCell>
                      <TableCell className="text-xs">{new Date(e.timestamp).toLocaleString()}</TableCell>
                      <TableCell className="text-xs">{e.action}</TableCell>
                      <TableCell className="text-code text-xs">{e.pipeline_run_id.slice(0, 8)}…</TableCell>
                      <TableCell>{e.verdict ? <VerdictBadge status={e.verdict} small /> : "—"}</TableCell>
                      <TableCell className="text-code text-xs">{e.confidence !== null ? e.confidence.toFixed(2) : "—"}</TableCell>
                      <TableCell className="text-xs">{e.authorized_by}</TableCell>
                      <TableCell className="text-code text-xs">{e.hmac_signature.slice(0, 12)}…</TableCell>
                    </TableRow>
                    {isOpen && (
                      <TableRow className="bg-muted/20 hover:bg-muted/20">
                        <TableCell colSpan={8} className="p-0">
                          <div className="space-y-2 p-3">
                            <div className="flex items-center justify-between text-code text-[11px] text-muted-foreground">
                              <span>Actuation ID: <span className="text-foreground">{e.actuation_id}</span></span>
                              <button
                                onClick={(ev) => { ev.stopPropagation(); copyToClipboard(e.hmac_signature, "HMAC signature"); }}
                                className="inline-flex items-center gap-1 text-primary hover:underline"
                              >
                                <Copy className="h-3 w-3" /> Copy full HMAC
                              </button>
                            </div>
                            <pre className="overflow-x-auto rounded-md border bg-background p-2 text-code text-[11px] leading-relaxed">
                              {JSON.stringify(e, null, 2)}
                            </pre>
                          </div>
                        </TableCell>
                      </TableRow>
                    )}
                  </Fragment>
                );
              })}
            </TableBody>
          </Table>
          {entries.length === 0 && (
            <p className="mt-4 text-center text-sm text-muted-foreground">No audit entries recorded yet.</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
