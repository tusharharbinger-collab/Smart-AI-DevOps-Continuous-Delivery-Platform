/**
 * frontend/src/components/wizard/AwsAccountPicker.tsx
 *
 * Backlog #3 - choose WHICH AWS account infrastructure is built in: the platform's own (default) or one of the
 * tenant's own verified accounts - and connect a new one. Connecting is the standard SaaS cross-account-role
 * flow: the platform generates an ExternalId and a CloudFormation template, the customer runs it in their
 * account, pastes back the role ARN, and the platform proves it by really assuming the role.
 */
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { CheckCircle2, Copy, Download, Loader2, ShieldCheck, XCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  createAwsConnection, listAwsConnections, verifyAwsConnection,
  type AwsConnection, type AwsConnectionSetup,
} from "@/api/awsConnections";

interface AwsAccountPickerProps {
  /** The chosen connection id; null means the platform's own AWS account. */
  value: string | null;
  onChange: (connectionId: string | null) => void;
  region: string;
}

export function AwsAccountPicker({ value, onChange, region }: AwsAccountPickerProps) {
  const [connections, setConnections] = useState<AwsConnection[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [name, setName] = useState("");
  const [setup, setSetup] = useState<AwsConnectionSetup | null>(null);
  const [roleArn, setRoleArn] = useState("");
  const [busy, setBusy] = useState(false);
  const [verifyError, setVerifyError] = useState<string | null>(null);

  async function refresh() {
    try {
      setConnections(await listAwsConnections());
      setLoadError(null);
    } catch (err) {
      setLoadError((err as Error).message);
    }
  }
  useEffect(() => {
    refresh();
  }, []);

  const verified = connections.filter((c) => c.status === "VERIFIED");
  const selected = connections.find((c) => c.connection_id === value) ?? null;

  async function handleCreate() {
    if (!name.trim()) return;
    setBusy(true);
    try {
      setSetup(await createAwsConnection(name.trim(), region));
      await refresh();
    } catch (err) {
      toast.error("Could not create the connection", { description: (err as Error).message });
    } finally {
      setBusy(false);
    }
  }

  async function handleVerify() {
    if (!setup) return;
    setBusy(true);
    setVerifyError(null);
    try {
      const result = await verifyAwsConnection(setup.connection_id, roleArn.trim());
      await refresh();
      if (result.status === "VERIFIED") {
        toast.success(`Connected to AWS account ${result.aws_account_id}`);
        onChange(result.connection_id);
        setConnecting(false);
        setSetup(null);
        setName("");
        setRoleArn("");
      } else {
        setVerifyError(result.status_reason ?? "The platform could not assume that role.");
      }
    } catch (err) {
      setVerifyError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function download() {
    if (!setup?.cloudformation_template) return;
    const url = URL.createObjectURL(new Blob([setup.cloudformation_template], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "smartcd-role.json";
    a.click();
    URL.revokeObjectURL(url);
  }

  async function copy(text: string, what: string) {
    try {
      await navigator.clipboard.writeText(text);
      toast.success(`${what} copied`);
    } catch {
      toast.error("Could not copy - select the text and copy it manually");
    }
  }

  return (
    <div className="space-y-2 rounded-md border p-3" data-testid="aws-account-picker">
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor="aws-account" className="text-xs font-semibold uppercase text-muted-foreground">
          AWS account
        </Label>
        {selected && (
          <Badge variant="success" className="gap-1 text-[10px]">
            <ShieldCheck className="h-3 w-3" /> {selected.aws_account_id}
          </Badge>
        )}
      </div>
      <select
        id="aws-account"
        className="h-9 w-full rounded-md border bg-background px-2 text-xs"
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value || null)}
      >
        <option value="">The platform's AWS account (default)</option>
        {verified.map((c) => (
          <option key={c.connection_id} value={c.connection_id}>
            Your account — {c.name} ({c.aws_account_id})
          </option>
        ))}
      </select>
      {loadError && <p className="text-xs text-destructive">{loadError}</p>}
      <p className="text-[11px] text-muted-foreground">
        {value
          ? "Infrastructure, discovery and status all run inside your account through a role you control. Deleting that role's stack revokes access instantly."
          : "Built in the platform's own AWS account. Connect your own account to provision there instead."}
      </p>

      {!connecting ? (
        <Button size="sm" variant="ghost" onClick={() => setConnecting(true)}>
          Connect your own AWS account…
        </Button>
      ) : (
        <div className="space-y-3 rounded-md border border-dashed p-3">
          {!setup ? (
            <div className="space-y-1.5">
              <Label htmlFor="aws-conn-name" className="text-xs">1. Name this connection</Label>
              <div className="flex gap-2">
                <Input id="aws-conn-name" placeholder="e.g. production" value={name} onChange={(e) => setName(e.target.value)} />
                <Button size="sm" onClick={handleCreate} disabled={busy || !name.trim()}>
                  {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />} Generate setup
                </Button>
              </div>
            </div>
          ) : (
            <>
              <div className="space-y-1.5">
                <Label className="text-xs">2. In your AWS account, create the role from this template</Label>
                <p className="text-[11px] text-muted-foreground">
                  It trusts only this platform and only with the ExternalId below, is scoped to <code>smartcd-*</code>{" "}
                  resources, and can be revoked by deleting its stack.
                </p>
                <div className="flex flex-wrap gap-2">
                  <Button size="sm" variant="outline" onClick={download}><Download className="h-3.5 w-3.5" /> Download template</Button>
                  <Button size="sm" variant="outline" onClick={() => copy(setup.external_id, "ExternalId")}>
                    <Copy className="h-3.5 w-3.5" /> Copy ExternalId
                  </Button>
                  {setup.cli_command && (
                    <Button size="sm" variant="outline" onClick={() => copy(setup.cli_command!, "Command")}>
                      <Copy className="h-3.5 w-3.5" /> Copy CLI command
                    </Button>
                  )}
                </div>
                <details className="text-xs">
                  <summary className="cursor-pointer text-muted-foreground">Review the template before running it</summary>
                  <pre className="mt-1 max-h-48 overflow-auto rounded bg-muted p-2 text-[10px]">{setup.cloudformation_template}</pre>
                </details>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="aws-role-arn" className="text-xs">3. Paste the role ARN from the stack's Outputs</Label>
                <div className="flex gap-2">
                  <Input
                    id="aws-role-arn"
                    placeholder="arn:aws:iam::123456789012:role/smartcd-platform-access"
                    value={roleArn}
                    onChange={(e) => setRoleArn(e.target.value)}
                  />
                  <Button size="sm" onClick={handleVerify} disabled={busy || roleArn.trim().length < 20}>
                    {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />} Verify
                  </Button>
                </div>
                {verifyError && (
                  <p className="flex items-start gap-1 text-xs text-destructive">
                    <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {verifyError}
                  </p>
                )}
              </div>
            </>
          )}
          <Button size="sm" variant="ghost" onClick={() => { setConnecting(false); setSetup(null); setVerifyError(null); }}>
            Cancel
          </Button>
        </div>
      )}
    </div>
  );
}
