import React, { useState } from "react";
import { 
  Sparkles, 
  Trash2, 
  DollarSign, 
  ShieldAlert, 
  FileCode, 
  Check, 
  Copy, 
  RefreshCw,
  AlertTriangle,
  Flame
} from "lucide-react";
import { Card, CardHeader, CardTitle, CardContent, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { toast } from "sonner";

export interface LogHygieneIssue {
  file: string;
  line_number: number;
  statement: string;
  issue_type: "NOISY_DEBUG" | "SENSITIVE_LEAK_RISK" | "HIGH_FREQUENCY_SPAM";
  reason: string;
  estimated_monthly_cost_usd: number;
}

export interface LogHygieneReport {
  detected_issues: LogHygieneIssue[];
  total_log_statements_found: number;
  estimated_monthly_savings_usd: number;
  annual_projected_savings_usd: number;
  recommended_best_practices: string[];
  suggested_patch?: string | null;
}

interface LogHygieneCardProps {
  projectId: string;
  initialReport?: LogHygieneReport | null;
}

export const LogHygieneCard: React.FC<LogHygieneCardProps> = ({ projectId, initialReport }) => {
  const [report, setReport] = useState<LogHygieneReport | null>(initialReport || null);
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);

  const fetchHygieneAnalysis = async () => {
    setLoading(true);
    try {
      const resp = await fetch(`/api/v1/projects/${projectId}/log-hygiene`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code_files: {}, cloudwatch_logs: [] }),
      });
      if (!resp.ok) {
        throw new Error(`Scan failed: ${resp.statusText}`);
      }
      const data = await resp.json();
      setReport(data);
      toast.success("AI Log Hygiene scan complete");
    } catch (err: any) {
      toast.error(err.message || "Failed to run log hygiene analysis");
    } finally {
      setLoading(false);
    }
  };

  const handleCopyPatch = async () => {
    if (!report?.suggested_patch) return;
    try {
      await navigator.clipboard.writeText(report.suggested_patch);
      setCopied(true);
      toast.success("Clean-up patch copied to clipboard");
      setTimeout(() => setCopied(false), 2500);
    } catch {
      toast.error("Failed to copy patch to clipboard");
    }
  };

  const renderDiffLines = (diff: string) => {
    return diff.split("\n").map((line, idx) => {
      let lineStyle = "text-slate-300";
      let bgStyle = "hover:bg-slate-800/40";
      let prefixIcon = " ";

      if (line.startsWith("@@")) {
        lineStyle = "text-sky-400 font-semibold bg-sky-950/30";
        prefixIcon = "";
      } else if (line.startsWith("+") && !line.startsWith("+++")) {
        lineStyle = "text-emerald-400 bg-emerald-950/25";
        bgStyle = "hover:bg-emerald-900/30";
        prefixIcon = "+";
      } else if (line.startsWith("-") && !line.startsWith("---")) {
        lineStyle = "text-rose-400 bg-rose-950/25";
        bgStyle = "hover:bg-rose-900/30";
        prefixIcon = "-";
      } else if (line.startsWith("---") || line.startsWith("+++")) {
        lineStyle = "text-slate-400 font-semibold";
      }

      return (
        <div 
          key={idx} 
          className={`flex items-start px-3 py-0.5 font-mono text-xs leading-5 select-text ${lineStyle} ${bgStyle}`}
        >
          <span className="w-8 shrink-0 select-none text-right pr-3 text-slate-600 text-[10px]">
            {idx + 1}
          </span>
          <span className="w-4 shrink-0 select-none text-center font-bold">
            {prefixIcon}
          </span>
          <span className="break-all whitespace-pre-wrap flex-1">
            {line.startsWith("+") || line.startsWith("-") ? line.slice(1) : line}
          </span>
        </div>
      );
    });
  };

  const sensitiveLeaks = report?.detected_issues.filter(i => i.issue_type === "SENSITIVE_LEAK_RISK") || [];

  return (
    <Card className="border border-border/60 bg-card/60 backdrop-blur-sm shadow-md overflow-hidden">
      <CardHeader className="border-b border-border/40 bg-muted/20 pb-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-base font-semibold">
              <Sparkles className="h-4 w-4 text-primary" />
              AI Code & CloudWatch Log Hygiene Analyzer
            </CardTitle>
            <CardDescription className="text-xs text-muted-foreground mt-1">
              Scans container logs and repository source files for noisy <code className="text-primary font-mono">console.log</code> / <code className="text-primary font-mono">print()</code> calls, identifies security leaks, and calculates AWS ingestion waste.
            </CardDescription>
          </div>

          <Button
            variant="outline"
            size="sm"
            onClick={fetchHygieneAnalysis}
            disabled={loading}
            className="h-8 gap-2 text-xs border-primary/30 hover:bg-primary/10"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
            {loading ? "Scanning Code & Logs..." : report ? "Re-scan Hygiene" : "Run AI Hygiene Scan"}
          </Button>
        </div>
      </CardHeader>

      <CardContent className="space-y-5 pt-4">
        {!report && !loading && (
          <div className="flex flex-col items-center justify-center p-8 text-center border border-dashed border-border/60 rounded-xl bg-muted/10">
            <Trash2 className="h-10 w-10 text-muted-foreground/40 mb-3" />
            <p className="text-sm font-medium text-foreground">No Hygiene Scan Run Yet</p>
            <p className="text-xs text-muted-foreground max-w-md mt-1 mb-4">
              Click &quot;Run AI Hygiene Scan&quot; to inspect your repository code and CloudWatch logs for redundant debug logging and potential secret leakage.
            </p>
            <Button size="sm" onClick={fetchHygieneAnalysis} className="gap-2">
              <Sparkles className="h-3.5 w-3.5" />
              Run Hygiene Scan Now
            </Button>
          </div>
        )}

        {report && (
          <>
            {/* KPI Cards */}
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div className="rounded-lg border border-border/50 bg-background/50 p-3 flex items-center gap-3">
                <div className="p-2 rounded-md bg-amber-500/10 text-amber-500">
                  <Flame className="h-5 w-5" />
                </div>
                <div>
                  <div className="text-xs text-muted-foreground">Log Statements</div>
                  <div className="text-lg font-bold text-foreground">
                    {report.total_log_statements_found}
                  </div>
                </div>
              </div>

              <div className="rounded-lg border border-border/50 bg-background/50 p-3 flex items-center gap-3">
                <div className="p-2 rounded-md bg-emerald-500/10 text-emerald-500">
                  <DollarSign className="h-5 w-5" />
                </div>
                <div>
                  <div className="text-xs text-muted-foreground">Monthly AWS Waste</div>
                  <div className="text-lg font-bold text-emerald-500">
                    ${report.estimated_monthly_savings_usd.toFixed(2)}/mo
                  </div>
                </div>
              </div>

              <div className="rounded-lg border border-border/50 bg-background/50 p-3 flex items-center gap-3">
                <div className={`p-2 rounded-md ${sensitiveLeaks.length > 0 ? "bg-rose-500/10 text-rose-500" : "bg-primary/10 text-primary"}`}>
                  <ShieldAlert className="h-5 w-5" />
                </div>
                <div>
                  <div className="text-xs text-muted-foreground">Security Leaks</div>
                  <div className={`text-lg font-bold ${sensitiveLeaks.length > 0 ? "text-rose-500" : "text-foreground"}`}>
                    {sensitiveLeaks.length}
                  </div>
                </div>
              </div>
            </div>

            {/* Sensitive Leaks Alert Banner */}
            {sensitiveLeaks.length > 0 && (
              <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 p-3.5 flex items-start gap-3">
                <AlertTriangle className="h-5 w-5 text-rose-500 shrink-0 mt-0.5" />
                <div className="space-y-1">
                  <span className="text-xs font-semibold text-rose-400">Security Warning: Sensitive Data Logged to CloudWatch</span>
                  <p className="text-xs text-rose-300/90 leading-relaxed">
                    Detected logging statements printing credentials, authorization headers, or tokens. These lines expose sensitive keys in plain text inside container logs.
                  </p>
                </div>
              </div>
            )}

            {/* Issues List */}
            {report.detected_issues.length > 0 ? (
              <div className="space-y-2">
                <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                  Detected Statements ({report.detected_issues.length})
                </span>
                <div className="rounded-lg border border-border/50 divide-y divide-border/40 overflow-hidden bg-background/30 text-xs">
                  {report.detected_issues.map((issue, idx) => (
                    <div key={idx} className="p-3 flex flex-col sm:flex-row sm:items-center justify-between gap-2 hover:bg-muted/10 transition-colors">
                      <div className="space-y-1">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-foreground font-medium">{issue.file}:{issue.line_number}</span>
                          <Badge 
                            variant="outline" 
                            className={`text-[10px] px-1.5 py-0 ${
                              issue.issue_type === "SENSITIVE_LEAK_RISK" 
                                ? "border-rose-500/30 text-rose-400 bg-rose-500/10" 
                                : issue.issue_type === "HIGH_FREQUENCY_SPAM" 
                                  ? "border-amber-500/30 text-amber-400 bg-amber-500/10"
                                  : "border-slate-500/30 text-slate-400"
                            }`}
                          >
                            {issue.issue_type}
                          </Badge>
                        </div>
                        <p className="text-slate-400 font-mono text-[11px] truncate max-w-xl">
                          {issue.statement}
                        </p>
                        <p className="text-muted-foreground text-[11px]">{issue.reason}</p>
                      </div>

                      <div className="shrink-0 text-right">
                        <span className="text-emerald-500 font-mono text-xs font-medium">
                          +${issue.estimated_monthly_cost_usd.toFixed(2)}/mo
                        </span>
                        <div className="text-[10px] text-muted-foreground">ingestion cost</div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-4 text-center">
                <Check className="h-6 w-6 text-emerald-500 mx-auto mb-1" />
                <p className="text-xs font-semibold text-emerald-400">Clean Log Hygiene</p>
                <p className="text-xs text-muted-foreground mt-0.5">
                  No noisy debug statements or security leaks detected in current codebase or logs.
                </p>
              </div>
            )}

            {/* Suggested Clean-Up Patch */}
            {report.suggested_patch && (
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-1.5 text-xs font-medium text-foreground">
                    <FileCode className="h-3.5 w-3.5 text-primary" />
                    <span>AI Clean-Up Unified Patch</span>
                  </div>

                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 gap-1.5 text-xs border-primary/30 hover:bg-primary/10"
                    onClick={handleCopyPatch}
                  >
                    {copied ? (
                      <>
                        <Check className="h-3.5 w-3.5 text-success" />
                        <span className="text-success font-medium">Copied Patch</span>
                      </>
                    ) : (
                      <>
                        <Copy className="h-3.5 w-3.5" />
                        <span>Copy Clean-Up Patch</span>
                      </>
                    )}
                  </Button>
                </div>

                <div className="rounded-lg border border-slate-800 bg-slate-950 overflow-hidden shadow-inner max-h-72 overflow-y-auto">
                  <div className="border-b border-slate-800 bg-slate-900/60 px-3 py-1 flex items-center justify-between text-[11px] text-slate-400 font-mono">
                    <span>clean-up.patch</span>
                    <span className="text-slate-500">unified diff</span>
                  </div>
                  <div className="py-2">
                    {renderDiffLines(report.suggested_patch)}
                  </div>
                </div>
              </div>
            )}

            {/* Best Practice Tips */}
            {report.recommended_best_practices.length > 0 && (
              <div className="rounded-lg border border-border/50 bg-muted/20 p-3 space-y-1.5 text-xs">
                <span className="font-semibold text-foreground">Production Logging Best Practices:</span>
                <ul className="list-disc pl-5 space-y-1 text-muted-foreground">
                  {report.recommended_best_practices.map((rec, i) => (
                    <li key={i}>{rec}</li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
};
