/**
 * frontend/src/pages/PolicyManager.tsx — Screen 3.
 */
import { useEffect, useState } from "react";
import CodeMirror from "@uiw/react-codemirror";
import { yaml as yamlLang } from "@codemirror/lang-yaml";
import { toast } from "sonner";
import { CheckCircle2, XCircle, FileCode2, ShieldAlert, SlidersHorizontal } from "lucide-react";
import { validatePolicy, savePolicy, getPolicy, type ValidationResult } from "@/api/policy";
import { useAppContext } from "@/hooks/useAppContext";
import { useThemeStore } from "@/lib/theme";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

export function PolicyManager() {
  const { pipelineId } = useAppContext();
  const theme = useThemeStore((s) => s.theme);
  const [yamlText, setYamlText] = useState("");
  const [validationResult, setValidationResult] = useState<ValidationResult | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!pipelineId) return;
    getPolicy(pipelineId)
      .then((res) => setYamlText(res.policy_yaml))
      .catch(() => setYamlText(""));
  }, [pipelineId]);

  useEffect(() => {
    if (!yamlText.trim()) {
      setValidationResult(null);
      return;
    }
    const handle = setTimeout(async () => {
      try {
        setValidationResult(await validatePolicy(yamlText));
      } catch (e) {
        setValidationResult({ valid: false, errors: [(e as Error).message] });
      }
    }, 500);
    return () => clearTimeout(handle);
  }, [yamlText]);

  async function handleSave() {
    if (!pipelineId) return;
    setSaving(true);
    try {
      await savePolicy(pipelineId, yamlText);
      toast.success("Policy saved");
    } catch (e) {
      toast.error("Save failed", { description: (e as Error).message });
    } finally {
      setSaving(false);
    }
  }

  if (!pipelineId) {
    return <p className="text-sm text-muted-foreground">Select a pipeline to edit its policy.</p>;
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Card className="overflow-hidden">
        <CardHeader className="flex-row items-center justify-between space-y-0 border-b bg-muted/30">
          <CardTitle className="flex items-center gap-1.5">
            <FileCode2 className="h-3.5 w-3.5 text-muted-foreground" />
            Pipeline & Gates YAML
          </CardTitle>
          {validationResult && (
            <Badge variant={validationResult.valid ? "success" : "destructive"}>
              {validationResult.valid ? <CheckCircle2 className="mr-1 h-3 w-3" /> : <XCircle className="mr-1 h-3 w-3" />}
              {validationResult.valid ? "valid" : "invalid"}
            </Badge>
          )}
        </CardHeader>
        <CardContent className="pt-4">
          <div className="overflow-hidden rounded-md border">
            <CodeMirror
              value={yamlText}
              height="500px"
              extensions={[yamlLang()]}
              theme={theme}
              onChange={setYamlText}
              placeholder="Paste the pipeline's gates/guardrails YAML…"
            />
          </div>
          <Button className="mt-3" onClick={handleSave} disabled={!validationResult?.valid || saving}>
            {saving ? "Saving…" : "Save Policy"}
          </Button>
          {validationResult?.errors?.map((err) => (
            <p key={err} className="mt-1 text-xs text-destructive">{err}</p>
          ))}
        </CardContent>
      </Card>

      <div className="space-y-4">
        <Card>
          <CardHeader className="border-b bg-muted/30">
            <CardTitle className="flex items-center gap-1.5">
              <ShieldAlert className="h-3.5 w-3.5 text-muted-foreground" />
              Blocked Deploy Windows
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-4">
            {(validationResult?.parsedGates?.blockedDeployWindows ?? []).length === 0 ? (
              <p className="text-xs text-muted-foreground">None configured.</p>
            ) : (
              <ul className="space-y-1 text-xs">
                {validationResult!.parsedGates!.blockedDeployWindows!.map((w, i) => (
                  <li key={i} className="rounded bg-muted px-2 py-1 text-code">
                    {w.days.join(", ")} · {w.startTime}–{w.endTime}
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="border-b bg-muted/30">
            <CardTitle className="flex items-center gap-1.5">
              <SlidersHorizontal className="h-3.5 w-3.5 text-muted-foreground" />
              Guardrails
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-1.5 pt-4 text-sm">
            <div className="flex justify-between border-b border-dashed pb-1.5">
              <span className="text-muted-foreground">Confidence floor</span>
              <span className="text-stat text-sm">{validationResult?.parsedGuardrails?.requireMinimumConfidence ?? "—"}</span>
            </div>
            <div className="flex justify-between border-b border-dashed pb-1.5">
              <span className="text-muted-foreground">Min sample size</span>
              <span className="text-stat text-sm">{validationResult?.parsedGuardrails?.minSampleSize ?? "—"}</span>
            </div>
            <div className="flex justify-between border-b border-dashed pb-1.5">
              <span className="text-muted-foreground">Max cost delta</span>
              <span className="text-stat text-sm">{validationResult?.parsedGuardrails?.maxPermittedCostDeltaPercent ?? "—"}%</span>
            </div>
            <div className="flex justify-between pb-1">
              <span className="text-muted-foreground">Dry-run allows promotion</span>
              <span className="text-stat text-sm">{validationResult?.dryRunAllowAction === undefined ? "—" : String(validationResult.dryRunAllowAction)}</span>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
