/**
 * Backlog #4 - explains WHY an infrastructure change failed, from the real CloudFormation events. Advice only:
 * a template fix is handed to the "Edit with AI" box for the human to review; nothing is applied automatically.
 */
import { useEffect, useState } from "react";
import { Loader2, Wand2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { analyzeInfraFailure, type InfraFailureAnalysis } from "@/api/infraDrafts";

interface Props {
  draftId: string;
  onUseSuggestedEdit: (instruction: string) => void;
}

export function InfraFailureAnalysisPanel({ draftId, onUseSuggestedEdit }: Props) {
  const [result, setResult] = useState<InfraFailureAnalysis | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setResult(null);
    setError(null);
    analyzeInfraFailure(draftId)
      .then((r) => !cancelled && setResult(r))
      .catch((e) => !cancelled && setError((e as Error).message));
    return () => {
      cancelled = true;
    };
  }, [draftId]);

  if (error) return <p className="text-xs text-muted-foreground">Could not analyze this failure: {error}</p>;
  if (!result) {
    return (
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground" data-testid="failure-analysis-loading">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Analyzing the failure…
      </p>
    );
  }
  return (
    <div className="space-y-2 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-xs" data-testid="failure-analysis">
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold">Why this failed</span>
        <Badge variant="outline" className="text-[10px]">
          {result.source === "model" ? "AI explanation · evidence verified" : "Rule-based explanation"}
        </Badge>
      </div>
      <p>{result.likely_cause}</p>
      <ul className="space-y-1">
        {result.evidence.map((line, i) => (
          <li key={i} className="rounded bg-muted p-1.5 font-mono text-[10px] break-words">{line}</li>
        ))}
      </ul>
      <p><span className="font-semibold">Suggested fix:</span> {result.suggested_fix}</p>
      {result.suggested_edit_instruction && (
        <Button size="sm" variant="outline" onClick={() => onUseSuggestedEdit(result.suggested_edit_instruction!)}>
          <Wand2 className="h-3.5 w-3.5" /> Use as an edit instruction
        </Button>
      )}
    </div>
  );
}
