import React, { useState } from "react";
import { 
  Sparkles, 
  FileCode, 
  GitCommit, 
  Terminal, 
  Copy, 
  Check, 
  Lightbulb, 
  Bug,
  ChevronDown,
  ChevronUp
} from "lucide-react";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";

export interface StructuredRca {
  executive_summary: string;
  root_cause_file?: string | null;
  line_number?: number | null;
  suspect_commit?: string | null;
  error_log_snippet?: string | null;
  suggested_patch?: string | null;
  suggested_remediation?: string;
  triggering_metrics?: Array<{ metric_name: string; citation: string }>;
  policy_clauses_evaluated?: string[];
  // AI Degradation Diagnosis extensions
  degradation_reason?: string | null;
  ways_to_improve?: string[];
  required_changes?: string | null;
}

interface RcaDiffViewerProps {
  rca: StructuredRca;
}

export const RcaDiffViewer: React.FC<RcaDiffViewerProps> = ({ rca }) => {
  const [copied, setCopied] = useState(false);
  const [showLogs, setShowLogs] = useState(true);

  const isDegradation = Boolean(rca.degradation_reason || (rca.ways_to_improve && rca.ways_to_improve.length > 0));

  const handleCopyPatch = async () => {
    if (!rca.suggested_patch) return;
    try {
      await navigator.clipboard.writeText(rca.suggested_patch);
      setCopied(true);
      toast.success("Code patch copied to clipboard");
      setTimeout(() => setCopied(false), 2500);
    } catch {
      toast.error("Failed to copy patch to clipboard");
    }
  };

  // Render unified diff lines with syntax coloring
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

  return (
    <Card className="border border-primary/20 bg-card/60 backdrop-blur-sm shadow-md overflow-hidden">
      <CardHeader className="border-b border-border/40 bg-muted/20 pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-base font-semibold">
            <Sparkles className="h-4 w-4 text-primary animate-pulse" />
            {isDegradation ? "AI Degradation Diagnosis & Remediation" : "AI Root Cause Analysis & Proposed Fix"}
          </CardTitle>

          <div className="flex flex-wrap items-center gap-1.5">
            {rca.degradation_reason && (
              <span className="inline-flex items-center gap-1 rounded-md bg-amber-500/10 px-2 py-0.5 text-xs font-medium text-amber-600 dark:text-amber-400 border border-amber-500/20">
                <Lightbulb className="h-3 w-3" />
                <span>{rca.degradation_reason}</span>
              </span>
            )}

            {rca.root_cause_file && (
              <span className="inline-flex items-center gap-1 rounded-md bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive border border-destructive/20">
                <Bug className="h-3 w-3" />
                <span className="font-mono">{rca.root_cause_file}</span>
                {rca.line_number && (
                  <span className="font-mono text-[10px] opacity-80">:{rca.line_number}</span>
                )}
              </span>
            )}

            {rca.suspect_commit && (
              <span className="inline-flex items-center gap-1 rounded-md bg-muted px-2 py-0.5 text-xs font-mono text-muted-foreground border border-border/40">
                <GitCommit className="h-3 w-3" />
                {rca.suspect_commit.slice(0, 7)}
              </span>
            )}
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-4 pt-4">
        {/* Executive Summary */}
        <p className="text-sm leading-relaxed text-foreground/90">
          {rca.executive_summary}
        </p>

        {/* Ways to Improve (Degradation specific) */}
        {rca.ways_to_improve && rca.ways_to_improve.length > 0 && (
          <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-3.5 space-y-2">
            <div className="flex items-center gap-2 text-xs font-semibold text-amber-600 dark:text-amber-400">
              <Lightbulb className="h-4 w-4" />
              <span>Recommended Ways to Stabilize & Pass Verification:</span>
            </div>
            <ul className="space-y-1.5 pl-6 list-disc text-xs text-foreground/90">
              {rca.ways_to_improve.map((way, idx) => (
                <li key={idx} className="leading-relaxed">{way}</li>
              ))}
            </ul>
          </div>
        )}

        {/* Required Changes / Config modifications */}
        {rca.required_changes && (
          <div className="rounded-lg border border-border/50 bg-muted/30 p-3 text-xs space-y-1">
            <span className="font-semibold text-foreground/90">Required Target State / Changes:</span>
            <p className="text-muted-foreground leading-relaxed">{rca.required_changes}</p>
          </div>
        )}

        {/* Runtime Error Log Snippet (if available) */}
        {rca.error_log_snippet && (
          <div className="rounded-lg border border-border/60 bg-slate-950 overflow-hidden text-xs">
            <div 
              className="flex items-center justify-between border-b border-slate-800/80 bg-slate-900/80 px-3 py-1.5 cursor-pointer select-none"
              onClick={() => setShowLogs(!showLogs)}
            >
              <div className="flex items-center gap-2 text-slate-400 font-mono text-[11px]">
                <Terminal className="h-3.5 w-3.5 text-amber-400" />
                <span>Runtime Container Error Logs</span>
              </div>
              <Button variant="ghost" size="sm" className="h-5 px-1 text-slate-400 hover:text-slate-200">
                {showLogs ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
              </Button>
            </div>
            {showLogs && (
              <div className="p-3 font-mono text-rose-300 text-xs leading-relaxed overflow-x-auto max-h-36">
                <pre className="whitespace-pre-wrap">{rca.error_log_snippet}</pre>
              </div>
            )}
          </div>
        )}

        {/* Suggested Code Patch */}
        {rca.suggested_patch && (
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                <FileCode className="h-3.5 w-3.5 text-primary" />
                <span>Suggested Code Patch (git diff)</span>
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
                    <span className="text-success font-medium">Copied</span>
                  </>
                ) : (
                  <>
                    <Copy className="h-3.5 w-3.5" />
                    <span>Copy Patch</span>
                  </>
                )}
              </Button>
            </div>

            <div className="rounded-lg border border-slate-800 bg-slate-950 overflow-hidden shadow-inner max-h-96 overflow-y-auto">
              <div className="border-b border-slate-800 bg-slate-900/60 px-3 py-1 flex items-center justify-between text-[11px] text-slate-400 font-mono">
                <span>{rca.root_cause_file || "unified.diff"}</span>
                <span className="text-slate-500">unified diff</span>
              </div>
              <div className="py-2">
                {renderDiffLines(rca.suggested_patch)}
              </div>
            </div>
          </div>
        )}

        {/* Actionable Remediation Guidance */}
        {rca.suggested_remediation && (
          <div className="flex items-start gap-2.5 rounded-lg border border-amber-500/20 bg-amber-500/5 p-3 text-xs text-foreground/90">
            <Lightbulb className="h-4 w-4 shrink-0 text-amber-500 mt-0.5" />
            <div className="space-y-1">
              <span className="font-semibold text-amber-600 dark:text-amber-400">Actionable Remediation:</span>
              <p className="leading-relaxed">{rca.suggested_remediation}</p>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
};
