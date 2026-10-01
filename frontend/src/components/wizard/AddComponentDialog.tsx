/**
 * frontend/src/components/wizard/AddComponentDialog.tsx
 *
 * AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.2 (Phase C) — the structured "add a specific component"
 * flow: pick a real AWS resource type from the full catalog, fill its minimal params, run a pre-flight
 * compatibility check (rule table or LLM fallback — never a full proposal regeneration), see the real
 * explanation/caveats, and only THEN confirm — which reuses the existing prompt-driven edit endpoint under
 * the hood (editInfraDraft) via the instruction string the compatibility check itself returned. Checking
 * is free to retry as many times as needed; nothing is generated or persisted until "Add this component."
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { AlertTriangle, CheckCircle2, Loader2, Plus, XCircle } from "lucide-react";
import {
  checkComponentCompatibility, editInfraDraft, getComponentCatalog,
  type ComponentCatalogEntry, type ComponentCompatibilityResult, type InfraDraft, type InfraEditMode,
} from "@/api/infraDrafts";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

export function AddComponentDialog({
  draftId, onApplied, offerReplaceMode = false,
}: {
  draftId: string;
  onApplied: (draft: InfraDraft) => void;
  /** Show the "add to current" vs "replace — start fresh" choice — only meaningful when a real proposal with additions already exists. */
  offerReplaceMode?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [resourceType, setResourceType] = useState<string>("");
  const [params, setParams] = useState<Record<string, unknown>>({});
  const [checkResult, setCheckResult] = useState<ComponentCompatibilityResult | null>(null);
  const [checking, setChecking] = useState(false);
  const [applying, setApplying] = useState(false);
  const [mode, setMode] = useState<InfraEditMode>("add");

  const { data } = useQuery({
    queryKey: ["component-catalog"],
    queryFn: getComponentCatalog,
    enabled: open,
    staleTime: Infinity,
  });

  const catalog = data?.catalog ?? [];
  const grouped = useMemo(() => {
    const map = new Map<string, ComponentCatalogEntry[]>();
    for (const entry of catalog) {
      const list = map.get(entry.category) ?? [];
      list.push(entry);
      map.set(entry.category, list);
    }
    return map;
  }, [catalog]);
  const selectedEntry = catalog.find((e) => e.resource_type === resourceType) ?? null;

  function reset() {
    setResourceType("");
    setParams({});
    setCheckResult(null);
    setMode("add");
  }

  function selectResourceType(value: string) {
    setResourceType(value);
    setParams({});
    setCheckResult(null);
  }

  async function handleCheck() {
    if (!resourceType) return;
    setChecking(true);
    setCheckResult(null);
    try {
      setCheckResult(await checkComponentCompatibility(draftId, resourceType, params));
    } catch (err) {
      toast.error("Could not check compatibility", { description: (err as Error).message });
    } finally {
      setChecking(false);
    }
  }

  async function handleConfirm() {
    if (!checkResult?.compatible) return;
    setApplying(true);
    try {
      const newDraft = await editInfraDraft(draftId, checkResult.instruction, { resourceType, mode });
      onApplied(newDraft);
      toast.success(
        mode === "replace"
          ? `Started fresh with ${selectedEntry?.display_name ?? "this component"} — review the new proposal below.`
          : `${selectedEntry?.display_name ?? "Component"} added — review the revised proposal below.`
      );
      setOpen(false);
      reset();
    } catch (err) {
      toast.error("Could not add this component", { description: (err as Error).message });
    } finally {
      setApplying(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { setOpen(next); if (!next) reset(); }}>
      <DialogTrigger asChild>
        <Button size="sm" variant="outline" className="gap-1.5">
          <Plus className="h-3.5 w-3.5" /> Add a component
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Add a component</DialogTitle>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="component-type">Resource type</Label>
            <Select value={resourceType} onValueChange={selectResourceType}>
              <SelectTrigger id="component-type"><SelectValue placeholder="Choose a resource type…" /></SelectTrigger>
              <SelectContent>
                {[...grouped.entries()].map(([category, entries]) => (
                  <div key={category}>
                    <div className="px-2 py-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{category}</div>
                    {entries.map((e) => (
                      <SelectItem key={e.resource_type} value={e.resource_type}>{e.display_name}</SelectItem>
                    ))}
                  </div>
                ))}
              </SelectContent>
            </Select>
            {selectedEntry && <p className="text-xs text-muted-foreground">{selectedEntry.description}</p>}
          </div>

          {selectedEntry && selectedEntry.params.length > 0 && (
            <div className="space-y-2 rounded-md border p-2">
              {selectedEntry.params.map((p) => (
                <div key={p.name} className="space-y-1">
                  <Label htmlFor={`param-${p.name}`} className="text-xs">
                    {p.name}{p.required && <span className="text-destructive"> *</span>}
                  </Label>
                  {p.type === "boolean" ? (
                    <div className="flex items-center gap-2">
                      <Switch
                        id={`param-${p.name}`}
                        checked={Boolean(params[p.name])}
                        onCheckedChange={(checked) => setParams((prev) => ({ ...prev, [p.name]: checked }))}
                      />
                      <span className="text-xs text-muted-foreground">{p.description}</span>
                    </div>
                  ) : (
                    <>
                      <Input
                        id={`param-${p.name}`}
                        type={p.type === "integer" ? "number" : "text"}
                        value={(params[p.name] as string | number | undefined) ?? ""}
                        onChange={(e) =>
                          setParams((prev) => ({
                            ...prev,
                            [p.name]: p.type === "integer" ? Number(e.target.value) : e.target.value,
                          }))
                        }
                      />
                      <p className="text-[11px] text-muted-foreground">{p.description}</p>
                    </>
                  )}
                </div>
              ))}
            </div>
          )}

          {offerReplaceMode && resourceType && (
            <div className="space-y-1.5 rounded-md border p-2">
              <Label className="text-xs">How should this be applied?</Label>
              <div className="grid gap-2 sm:grid-cols-2">
                {([
                  ["add", "Add to current infrastructure", "Keep everything already proposed and extend it with this component."],
                  ["replace", "Delete and start over", "Discard the current proposal entirely and generate a fresh one with only this component."],
                ] as const).map(([key, title, blurb]) => (
                  <button
                    type="button"
                    key={key}
                    onClick={() => setMode(key)}
                    className={`rounded-md border p-2 text-left text-xs transition-colors ${mode === key ? "border-primary bg-primary/5" : "hover:bg-accent"}`}
                  >
                    <div className="font-medium">{title}</div>
                    <div className="text-muted-foreground">{blurb}</div>
                  </button>
                ))}
              </div>
            </div>
          )}

          {resourceType && (
            <Button size="sm" variant="outline" onClick={handleCheck} disabled={checking} className="gap-1.5">
              {checking && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              Check compatibility
            </Button>
          )}

          {checkResult && (
            <div
              className={`space-y-1.5 rounded-md border p-3 text-sm ${
                checkResult.compatible ? "border-success/40 bg-success/5" : "border-destructive/40 bg-destructive/5"
              }`}
            >
              <div className="flex items-center gap-1.5 font-medium">
                {checkResult.compatible ? (
                  <CheckCircle2 className="h-4 w-4 text-success" />
                ) : (
                  <XCircle className="h-4 w-4 text-destructive" />
                )}
                {checkResult.compatible ? "Can be added" : "Cannot be added"}
              </div>
              <p className="text-muted-foreground">{checkResult.explanation}</p>
              {checkResult.caveats.length > 0 && (
                <ul className="space-y-1">
                  {checkResult.caveats.map((c, i) => (
                    <li key={i} className="flex items-start gap-1.5 text-xs text-warning">
                      <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" /> {c}
                    </li>
                  ))}
                </ul>
              )}
              {checkResult.source === "llm" && (
                <p className="text-[10px] uppercase tracking-wide text-muted-foreground">AI-checked (not in the standard rule table)</p>
              )}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
          <Button onClick={handleConfirm} disabled={!checkResult?.compatible || applying}>
            {applying ? "Adding…" : "Add this component"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
