/**
 * frontend/src/pages/SettingsPage.tsx
 *
 * Platform LLM-provider settings (AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.5) - the Gemini -> Groq
 * -> OpenRouter -> Mistral failover chain every AI-backed feature in this platform routes through. Viewing
 * status (priority, active model, masked credential, endpoint, last-measured latency) is open to any
 * authenticated user; editing a credential is role-gated server-side (lead-sre+) - a developer sees the
 * "Edit Credentials" button but the request 403s if attempted, same as every other role-gated action in
 * this app (e.g. infra draft approval).
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Zap, Loader2, KeyRound } from "lucide-react";
import { listLlmProviders, setLlmProviderCredentials, testLlmProvider, type LlmProvider } from "@/api/settings";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const ORDINAL: Record<number, string> = { 1: "Primary", 2: "Secondary", 3: "Tertiary", 4: "Quaternary" };
const ACCENT: Record<number, string> = {
  1: "border-l-primary",
  2: "border-l-warning",
  3: "border-l-violet-500",
  4: "border-l-orange-500",
};

function ProviderCard({ provider }: { provider: LlmProvider }) {
  const queryClient = useQueryClient();
  const [editOpen, setEditOpen] = useState(false);
  const [apiKey, setApiKey] = useState("");

  const testMutation = useMutation({
    mutationFn: () => testLlmProvider(provider.name),
    onSuccess: (result) => {
      if (result.ok) {
        toast.success(`${provider.display_name} connection OK`, { description: `${result.latency_ms}ms` });
      } else {
        toast.error(`${provider.display_name} connection failed`, { description: result.error ?? undefined });
      }
    },
    onError: (err: Error) => toast.error("Test connection failed", { description: err.message }),
  });

  const credentialMutation = useMutation({
    mutationFn: () => setLlmProviderCredentials(provider.name, apiKey),
    onSuccess: () => {
      toast.success(`${provider.display_name} credentials updated`);
      setEditOpen(false);
      setApiKey("");
      queryClient.invalidateQueries({ queryKey: ["llm-providers"] });
    },
    onError: (err: Error) => toast.error("Failed to update credentials", { description: err.message }),
  });

  return (
    <Card className={`border-l-4 ${ACCENT[provider.priority] ?? "border-l-border"}`}>
      <CardContent className="space-y-3 p-4">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold">{provider.display_name}</span>
            <span className="text-xs text-muted-foreground">{provider.vendor_label}</span>
          </div>
          <div className="flex items-center gap-1.5">
            <Badge variant="secondary" className="text-[10px]">
              Priority #{provider.priority} ({ORDINAL[provider.priority] ?? provider.priority})
            </Badge>
            <span className={`h-2 w-2 rounded-full ${provider.configured ? "bg-success" : "bg-muted-foreground/40"}`} />
          </div>
        </div>

        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
          <dt className="text-muted-foreground">Active Model:</dt>
          <dd className="text-right font-medium text-primary">{provider.active_model}</dd>
          <dt className="text-muted-foreground">Credential:</dt>
          <dd className="text-right text-code">{provider.configured ? provider.masked_credential : "not configured"}</dd>
          <dt className="text-muted-foreground">Endpoint:</dt>
          <dd className="truncate text-right text-code" title={provider.endpoint}>
            {provider.endpoint.replace(/^https:\/\//, "").replace(/\/chat\/completions$/, "/")}
          </dd>
        </dl>

        <div className="flex gap-2">
          <Button
            size="sm"
            variant="outline"
            className="h-8 flex-1 gap-1.5 text-xs"
            onClick={() => testMutation.mutate()}
            disabled={testMutation.isPending || !provider.configured}
          >
            {testMutation.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Zap className="h-3.5 w-3.5" />}
            Test Connection
          </Button>
          <Button size="sm" variant="outline" className="h-8 flex-1 gap-1.5 text-xs" onClick={() => setEditOpen(true)}>
            <KeyRound className="h-3.5 w-3.5" /> Edit Credentials
          </Button>
        </div>
      </CardContent>

      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit {provider.display_name} credentials</DialogTitle>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor={`api-key-${provider.name}`}>API key</Label>
            <Input
              id={`api-key-${provider.name}`}
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="Paste the new key — never shown again after saving"
            />
            <p className="text-xs text-muted-foreground">
              Stored server-side only; this platform never displays a raw credential once saved. Requires the
              lead-sre role or above.
            </p>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditOpen(false)}>Cancel</Button>
            <Button
              onClick={() => credentialMutation.mutate()}
              disabled={!apiKey.trim() || credentialMutation.isPending}
            >
              {credentialMutation.isPending ? "Saving…" : "Save"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

export function SettingsPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["llm-providers"],
    queryFn: listLlmProviders,
  });

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">LLM Providers</h1>
        <p className="text-sm text-muted-foreground">
          Configured AI inference endpoints with 4-way failover routing (Primary → Secondary → Tertiary → Quaternary).
        </p>
      </div>

      {isLoading && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Skeleton className="h-40" />
          <Skeleton className="h-40" />
          <Skeleton className="h-40" />
          <Skeleton className="h-40" />
        </div>
      )}

      {error && <p className="text-sm text-destructive">Could not load LLM provider settings: {(error as Error).message}</p>}

      {data && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {data.providers.map((provider) => (
            <ProviderCard key={provider.name} provider={provider} />
          ))}
        </div>
      )}
    </div>
  );
}
