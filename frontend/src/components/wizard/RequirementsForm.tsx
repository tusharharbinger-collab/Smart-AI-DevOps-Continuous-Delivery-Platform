/**
 * frontend/src/components/wizard/RequirementsForm.tsx
 *
 * The Requirements Form (AI_AGENTIC_ORCHESTRATION_PLAN.md §2.1/§2.5) — the
 * actual start of the (future) AI infra-generation flow. Pre-filled from
 * Phase 1's repo detection (`detection.infra_signals`/`detection.archetype`,
 * shared/repo_scanner.py), but every pre-filled value stays editable: a
 * detected signal means "found a real dependency," never "confirmed and
 * locked." Submitting this form is what locks the IntentSpec — nothing
 * downstream (the not-yet-built Infra Architect Agent) is allowed to guess
 * a field this form already asked for.
 */
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { CheckCircle2, Info, Loader2, ShieldAlert, ShieldCheck, ShieldX, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  approveInfraDraft, createInfraChangeSet, createInfraDraft, discoverExistingInfra, editInfraDraft,
  executeInfraChangeSet, getInfraProvisioningStatus,
  type DiscoveredResources, type InfraDraft, type InfraSource,
} from "@/api/infraDrafts";
import { InfraTopologyGraph } from "@/components/wizard/InfraTopologyGraph";
import { AwsAccountPicker } from "@/components/wizard/AwsAccountPicker";
import { InfraFailureAnalysisPanel } from "@/components/wizard/InfraFailureAnalysisPanel";
import { synthesizeRealInfraTopology } from "@/lib/syntheticInfraTopology";
import type { BuildDetection } from "@/api/github";
import type { DatabaseType, EnvironmentTier, IntentSpec } from "@/api/intentSpec";

interface RequirementsFormProps {
  detection: BuildDetection | null;
  value: IntentSpec;
  onChange: (next: IntentSpec) => void;
  /**
   * Lifts the infra draft (created via "Preview Infrastructure (AI)") up to
   * NewProject.tsx so its draft_id can be threaded into the final
   * POST /projects call (CreateProjectInput.infra_draft_id) — without this,
   * the draft this form produces is discarded once the wizard advances past
   * this step, and the persistent project view has no way to ever find its
   * infra topology again.
   */
  onDraftChange?: (draft: InfraDraft | null) => void;
  /** "kubernetes" | "aws_ecs" — lets "Compare with standard topology" synthesize the right default resource shape. */
  deployTarget?: string;
  /** Used only to label the synthesized "standard topology" comparison — falls back to a generic placeholder when not yet typed. */
  projectNameHint?: string;
}

const ARCHETYPE_LABELS: Record<string, string> = {
  static_site: "Static site",
  stateless_web_service: "Stateless web service",
  web_service_with_database: "Web service + database",
  web_service_with_database_and_cache: "Web service + database + cache",
  background_worker: "Background worker",
  multi_service: "Multi-service",
};

const SLOT_LABELS: Record<string, string> = {
  database: "Database (RDS)",
  cache: "Cache (ElastiCache)",
  ecs_cluster: "ECS cluster",
  load_balancer: "Load balancer",
};

function DetectedBadge() {
  return (
    <Badge variant="secondary" className="ml-1.5 gap-1 text-[10px]">
      <Info className="h-2.5 w-2.5" /> Detected
    </Badge>
  );
}

export function RequirementsForm({ detection, value, onChange, onDraftChange, deployTarget, projectNameHint }: RequirementsFormProps) {
  // Phase 4/5 — a real call to the Infra Architect Agent (Groq, via
  // POST /infra-drafts), not a mock. State lives here (it isn't part of
  // IntentSpec — it's a separate, later artifact the human reviews before
  // anything is approved) but is also mirrored up via onDraftChange so
  // NewProject.tsx can carry its draft_id into project creation.
  const [draft, setDraftState] = useState<InfraDraft | null>(null);
  const setDraft = (next: InfraDraft | null) => {
    setDraftState(next);
    onDraftChange?.(next);
  };
  const [drafting, setDrafting] = useState(false);
  const [draftError, setDraftError] = useState<string | null>(null);

  // AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md: "AI-created" designs brand-new resources; "existing"
  // attaches real AWS resources the human picks (imported with DeletionPolicy: Retain - the
  // platform can never delete something you said already existed).
  // Backlog #3: which AWS account to build in (null = the platform's own).
  const [awsConnectionId, setAwsConnectionId] = useState<string | null>(null);
  const [infraSource, setInfraSource] = useState<InfraSource>("ai_created");
  const [discovered, setDiscovered] = useState<DiscoveredResources | null>(null);
  const [discovering, setDiscovering] = useState(false);
  const [discoverError, setDiscoverError] = useState<string | null>(null);
  const [selection, setSelection] = useState<Record<string, string>>({});
  const selectedCount = Object.keys(selection).length;

  async function handleDiscover() {
    if (!value.archetype) return;
    setDiscovering(true);
    setDiscoverError(null);
    try {
      setDiscovered(await discoverExistingInfra(value.archetype, value.aws_region, awsConnectionId));
      setSelection({});
    } catch (err) {
      const message = (err as Error).message;
      setDiscoverError(message);
      toast.error("Could not list existing AWS resources", { description: message });
    } finally {
      setDiscovering(false);
    }
  }

  function pickExisting(slot: string, id: string) {
    setSelection((prev) => {
      const next = { ...prev };
      if (id) next[slot] = id;
      else delete next[slot];
      return next;
    });
  }

  // Prompt-driven edit: returns a NEW draft that re-enters the approval gate (never auto-applied).
  const [editInstruction, setEditInstruction] = useState("");
  const [editing, setEditing] = useState(false);
  async function handleEditInfrastructure() {
    if (!draft || editInstruction.trim().length < 3) return;
    setEditing(true);
    try {
      const result = await editInfraDraft(draft.draft_id, editInstruction.trim());
      setDraft(result);
      setEditInstruction("");
      setExecuteConfirmed(false);
      toast.success("Revised proposal ready — review it below before using it.");
    } catch (err) {
      toast.error("Could not apply that edit", { description: (err as Error).message });
    } finally {
      setEditing(false);
    }
  }

  async function handlePreviewInfrastructure() {
    setDrafting(true);
    setDraftError(null);
    try {
      const result = await createInfraDraft(value, {
        source: infraSource,
        existingSelection: infraSource === "existing" ? selection : {},
        awsConnectionId,
      });
      setDraft(result);
    } catch (err) {
      const message = (err as Error).message;
      setDraftError(message);
      toast.error("Could not generate an infrastructure proposal", { description: message });
    } finally {
      setDrafting(false);
    }
  }

  // Real gap found live: a warn/require_approval-tier proposal (Phase 3's
  // §2.4 tiered gate) had NO visible action anywhere in this form to move
  // past INFRA_PENDING_APPROVAL — approveInfraDraft() existed on the
  // backend and was even documented, but nothing in this component ever
  // called it. An auto_advance proposal reaches INFRA_APPROVED silently
  // with no human click at all, so there was also no single, explicit
  // "I choose this infrastructure" moment for EITHER path — this is that
  // moment, unified across both.
  const [approvingDraft, setApprovingDraft] = useState(false);
  async function handleUseThisInfrastructure() {
    if (!draft) return;
    setApprovingDraft(true);
    try {
      const result = await approveInfraDraft(draft.draft_id);
      setDraft(result);
      toast.success("This infrastructure is now selected for the project.");
    } catch (err) {
      const message = (err as Error).message;
      toast.error("Could not select this infrastructure", { description: message });
    } finally {
      setApprovingDraft(false);
    }
  }

  // "There should be an option to compare this" — shows the platform's
  // deterministic default topology (what gets created WITHOUT any AI call
  // at all: a shared ALB/HTTPRoute + baseline/canary services, see
  // synthesizeRealInfraTopology.ts) next to the AI proposal, so the human
  // can see exactly what the AI added (a database, a cache, etc.) rather
  // than taking the proposal on faith.
  // Only data stores can be attached (the platform builds the ALB/cluster/services itself), and only the ones this app needs.
  const nothingToAttach = !value.needs_database && !value.needs_cache;
  const visibleSlots = Object.entries(discovered ?? {}).filter(
    ([slot]) => (slot === "database" && value.needs_database) || (slot === "cache" && value.needs_cache),
  );
  const policyBlockers = draft?.infra_proposal?.policy_evaluation?.allowed === false
    ? draft.infra_proposal.policy_evaluation.deny
    : [];
  const [showComparison, setShowComparison] = useState(false);
  const standardTopology = useMemo(
    () => synthesizeRealInfraTopology(projectNameHint || "your-service", deployTarget),
    [projectNameHint, deployTarget]
  );

  // Phase 7 (AI_INFRA_PROVISIONING_EXECUTION_PLAN.md) — real provisioning
  // execution. Three distinct states: previewing (real, zero-risk AWS
  // Change Set), the second human approval to actually execute it, and
  // polling status while CloudFormation provisions for real.
  const [changeSetLoading, setChangeSetLoading] = useState(false);
  const [executeLoading, setExecuteLoading] = useState(false);
  const [executeConfirmed, setExecuteConfirmed] = useState(false);
  const [provisioningError, setProvisioningError] = useState<string | null>(null);

  async function handleCreateChangeSet() {
    if (!draft) return;
    setChangeSetLoading(true);
    setProvisioningError(null);
    try {
      const result = await createInfraChangeSet(draft.draft_id);
      setDraft(result);
    } catch (err) {
      const message = (err as Error).message;
      setProvisioningError(message);
      toast.error("Could not preview real AWS changes", { description: message });
    } finally {
      setChangeSetLoading(false);
    }
  }

  async function handleExecute() {
    if (!draft) return;
    setExecuteLoading(true);
    setProvisioningError(null);
    try {
      let result = await executeInfraChangeSet(draft.draft_id);
      setDraft(result);
      // Poll until CloudFormation settles (succeeded or failed) — provisioning
      // is asynchronous and can take anywhere from seconds to minutes.
      while (result.status === "INFRA_PROVISIONING") {
        await new Promise((resolve) => setTimeout(resolve, 3000));
        result = await getInfraProvisioningStatus(draft.draft_id);
        setDraft(result);
      }
      if (result.status === "INFRA_PROVISIONED") {
        toast.success("Real AWS resources provisioned successfully.");
      } else if (result.status === "INFRA_PROVISIONING_FAILED") {
        toast.error("Provisioning failed", { description: result.provisioning_error ?? undefined });
      }
    } catch (err) {
      const message = (err as Error).message;
      setProvisioningError(message);
      toast.error("Could not execute the change set", { description: message });
    } finally {
      setExecuteLoading(false);
    }
  }

  // Applies Phase 1's detected signals exactly once per repo detection
  // result — never overwrites a field the human has already touched, and
  // never re-fires on every render (only when a NEW detection arrives).
  const [appliedFor, setAppliedFor] = useState<string | null>(null);
  useEffect(() => {
    if (!detection?.archetype) return;
    const key = `${detection.archetype}:${detection.manifest_path ?? ""}`;
    if (appliedFor === key) return;
    setAppliedFor(key);
    const infra = detection.infra_signals;
    onChange({
      ...value,
      archetype: detection.archetype,
      needs_database: value.needs_database || Boolean(infra?.needs_database),
      needs_cache: value.needs_cache || Boolean(infra?.needs_cache),
      needs_object_storage: value.needs_object_storage || Boolean(infra?.needs_object_storage),
      detected_database_hint: infra?.database_hint ?? value.detected_database_hint,
      detected_cache_hint: infra?.cache_hint ?? value.detected_cache_hint,
      detected_storage_hint: infra?.storage_hint ?? value.detected_storage_hint,
      detected_confidence: detection.confidence ?? value.detected_confidence,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detection?.archetype, detection?.manifest_path]);

  const infra = detection?.infra_signals;

  return (
    <div className="space-y-6">
      {value.archetype && (
        <div className="flex items-center gap-2 rounded-md border bg-muted/40 p-3 text-xs">
          <Info className="h-3.5 w-3.5 text-muted-foreground" />
          <span>
            Matched golden-path archetype: <span className="font-medium">{ARCHETYPE_LABELS[value.archetype] ?? value.archetype}</span>
            {" "}— every field below is pre-filled from your repo where possible, but you can change any of them.
          </span>
        </div>
      )}

      {/* ── Scale & traffic ── */}
      <div className="space-y-3">
        <Label className="text-xs font-semibold uppercase text-muted-foreground">Scale &amp; traffic</Label>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Label htmlFor="req-tier">Environment tier</Label>
            <Select
              value={value.environment_tier}
              onValueChange={(v) => onChange({ ...value, environment_tier: v as EnvironmentTier })}
            >
              <SelectTrigger id="req-tier"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="dev">Dev</SelectItem>
                <SelectItem value="staging">Staging</SelectItem>
                <SelectItem value="production">Production</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div>
            <Label htmlFor="req-rps">Expected requests/sec (optional)</Label>
            <Input
              id="req-rps"
              type="number"
              min={0}
              placeholder="unsure — leave blank"
              value={value.expected_rps ?? ""}
              onChange={(e) => onChange({ ...value, expected_rps: e.target.value ? Number(e.target.value) : null })}
            />
          </div>
        </div>
      </div>

      {/* ── Compute ── */}
      <div className="space-y-3">
        <Label className="text-xs font-semibold uppercase text-muted-foreground">Compute</Label>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Label htmlFor="req-min">Min instances</Label>
            <Input
              id="req-min"
              type="number"
              min={1}
              placeholder="tier default"
              value={value.min_instances ?? ""}
              onChange={(e) => onChange({ ...value, min_instances: e.target.value ? Number(e.target.value) : null })}
            />
          </div>
          <div>
            <Label htmlFor="req-max">Max instances</Label>
            <Input
              id="req-max"
              type="number"
              min={1}
              placeholder="tier default"
              value={value.max_instances ?? ""}
              onChange={(e) => onChange({ ...value, max_instances: e.target.value ? Number(e.target.value) : null })}
            />
          </div>
        </div>
        <p className="text-[11px] text-muted-foreground">
          Leave blank to use a safe default for the selected tier — Fargate sizing is a small, fixed set of
          AWS-supported CPU/memory combinations, not a value worth guessing at manually.
        </p>
      </div>

      {/* ── Data ── */}
      <div className="space-y-3">
        <Label className="text-xs font-semibold uppercase text-muted-foreground">Data</Label>
        <div className="flex items-center justify-between rounded-md border p-3">
          <div>
            <span className="flex items-center text-sm">
              Needs a database
              {infra?.needs_database && <DetectedBadge />}
            </span>
            {value.detected_database_hint && (
              <span className="text-[11px] text-muted-foreground">Found dependency: {value.detected_database_hint}</span>
            )}
          </div>
          <Switch
            checked={value.needs_database}
            onCheckedChange={(checked) =>
              onChange({ ...value, needs_database: checked, database_type: checked ? value.database_type ?? "postgres" : null, multi_az: checked ? value.multi_az : false })
            }
          />
        </div>
        {value.needs_database && (
          <div className="grid grid-cols-2 gap-3 pl-3">
            <div>
              <Label htmlFor="req-db-type">Database type</Label>
              <Select
                value={value.database_type ?? "postgres"}
                onValueChange={(v) => onChange({ ...value, database_type: v as DatabaseType })}
              >
                <SelectTrigger id="req-db-type"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="postgres">PostgreSQL</SelectItem>
                  <SelectItem value="mysql">MySQL</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex items-center gap-2 pt-6">
              <Switch checked={Boolean(value.multi_az)} onCheckedChange={(checked) => onChange({ ...value, multi_az: checked })} />
              <Label className="!mt-0">Multi-AZ (high availability)</Label>
            </div>
          </div>
        )}

        <div className="flex items-center justify-between rounded-md border p-3">
          <span className="flex items-center text-sm">
            Needs a cache
            {infra?.needs_cache && <DetectedBadge />}
          </span>
          <Switch checked={value.needs_cache} onCheckedChange={(checked) => onChange({ ...value, needs_cache: checked })} />
        </div>

        <div className="flex items-center justify-between rounded-md border p-3">
          <span className="flex items-center text-sm">
            Needs object storage
            {infra?.needs_object_storage && <DetectedBadge />}
          </span>
          <Switch
            checked={value.needs_object_storage}
            onCheckedChange={(checked) => onChange({ ...value, needs_object_storage: checked })}
          />
        </div>
      </div>

      {/* ── Networking & security ── */}
      <div className="space-y-3">
        <Label className="text-xs font-semibold uppercase text-muted-foreground">Networking &amp; security</Label>
        <div className="flex items-center justify-between rounded-md border p-3">
          <span className="text-sm">Public-facing</span>
          <Switch checked={value.public_facing} onCheckedChange={(checked) => onChange({ ...value, public_facing: checked })} />
        </div>
        <div className="flex items-center justify-between rounded-md border p-3">
          <span className="text-sm">Private-subnet only</span>
          <Switch
            checked={value.private_subnet_only}
            onCheckedChange={(checked) => onChange({ ...value, private_subnet_only: checked })}
          />
        </div>
        <div className="flex items-center justify-between rounded-md border p-3">
          <span className="text-sm">Encryption at rest required</span>
          <Switch
            checked={value.encryption_at_rest_required}
            onCheckedChange={(checked) => onChange({ ...value, encryption_at_rest_required: checked })}
          />
        </div>
      </div>

      {/* ── Budget & region ── */}
      <div className="space-y-3">
        <Label className="text-xs font-semibold uppercase text-muted-foreground">Budget &amp; region</Label>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Label htmlFor="req-budget">Monthly budget ceiling, USD (optional)</Label>
            <Input
              id="req-budget"
              type="number"
              min={0}
              placeholder="no ceiling"
              value={value.monthly_budget_usd ?? ""}
              onChange={(e) => onChange({ ...value, monthly_budget_usd: e.target.value ? Number(e.target.value) : null })}
            />
          </div>
          <div>
            <Label htmlFor="req-region">AWS region</Label>
            <Input
              id="req-region"
              value={value.aws_region}
              onChange={(e) => onChange({ ...value, aws_region: e.target.value })}
            />
          </div>
        </div>
      </div>

      {/* ── AI infra proposal (Phase 4/5) — a real Groq-backed call, not a mock ── */}
      <div className="space-y-3 border-t pt-4">
        <div className="flex items-center justify-between">
          <Label className="text-xs font-semibold uppercase text-muted-foreground">Infrastructure proposal</Label>
          <Button size="sm" variant="secondary" onClick={handlePreviewInfrastructure} disabled={drafting || !value.archetype || (infraSource === "existing" && selectedCount === 0)}>
            {drafting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
            Preview Infrastructure (AI)
          </Button>
        </div>
        <AwsAccountPicker
          value={awsConnectionId}
          onChange={(id) => {
            setAwsConnectionId(id);
            setDiscovered(null);
            setSelection({});
          }}
          region={value.aws_region}
        />
        <div className="space-y-2 rounded-md border p-3">
          <Label className="text-xs font-semibold uppercase text-muted-foreground">Infrastructure source</Label>
          <div className="grid gap-2 sm:grid-cols-2">
            {([
              ["ai_created", "AI-created", "The agent designs new AWS resources for this project."],
              ["existing", "Existing", "Attach resources that already exist in your AWS account instead of creating duplicates."],
            ] as const).map(([key, title, blurb]) => (
              <button
                type="button"
                key={key}
                onClick={() => setInfraSource(key)}
                className={`rounded-md border p-2 text-left text-xs transition-colors ${
                  infraSource === key ? "border-primary bg-primary/5" : "hover:bg-accent"
                }`}
              >
                <div className="font-medium">{title}</div>
                <div className="text-muted-foreground">{blurb}</div>
              </button>
            ))}
          </div>

          {infraSource === "existing" && (
            <div className="space-y-2 pt-1">
              <div className="flex items-center justify-between gap-2">
                <p className="text-[11px] text-muted-foreground">
                  Read-only lookup of your account in {value.aws_region}. Imported resources are kept
                  (DeletionPolicy: Retain) — this platform can never delete them.
                </p>
                <Button size="sm" variant="outline" onClick={handleDiscover} disabled={discovering || !value.archetype || nothingToAttach}>
                  {discovering && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                  {discovered ? "Refresh" : "Find existing resources"}
                </Button>
              </div>
              {nothingToAttach && (
                <p className="rounded-md border border-dashed p-2 text-[11px] text-muted-foreground" data-testid="nothing-to-attach">
                  Nothing to attach: this app needs no database or cache, and the platform builds the load balancer, cluster
                  and services itself. Turn on "Needs a database" or "Needs a cache" above if you want to attach an
                  existing one.
                </p>
              )}
              {discoverError && <p className="text-xs text-destructive">{discoverError}</p>}
              {discovered && !nothingToAttach && visibleSlots.length === 0 && (
                <p className="text-xs text-muted-foreground">No attachable resource types for this setup.</p>
              )}
              {discovered && !nothingToAttach && visibleSlots.length > 0 && visibleSlots.every(([, c]) => c.length === 0) && (
                <p className="rounded-md border border-dashed p-2 text-xs text-muted-foreground" data-testid="nothing-found">
                  Nothing found in {value.aws_region}: your AWS account has no matching resources there, so there is nothing to
                  attach. Switch back to AI-created and the agent will design a new one.
                </p>
              )}
              {discovered && !nothingToAttach && (
                <div className="grid gap-2 sm:grid-cols-2">
                  {visibleSlots.map(([slot, candidates]) => (
                    <div key={slot} className="space-y-1">
                      <Label htmlFor={`existing-${slot}`} className="text-xs">{SLOT_LABELS[slot] ?? slot}</Label>
                      <select
                        id={`existing-${slot}`}
                        className="h-9 w-full rounded-md border bg-background px-2 text-xs"
                        value={selection[slot] ?? ""}
                        onChange={(e) => pickExisting(slot, e.target.value)}
                      >
                        <option value="">
                          {candidates.length === 0 ? "None found — the agent will create one" : "Create new (don't attach)"}
                        </option>
                        {candidates.map((c) => (
                          <option key={c.id} value={c.id}>
                            {c.label}
                            {c.details.engine ? ` · ${String(c.details.engine)}` : ""}
                            {c.details.instance_class ? ` · ${String(c.details.instance_class)}` : ""}
                          </option>
                        ))}
                      </select>
                    </div>
                  ))}
                </div>
              )}
              {discovered && selectedCount === 0 && (
                <p className="text-[11px] text-muted-foreground">Pick at least one resource to attach, or switch back to AI-created.</p>
              )}
            </div>
          )}
        </div>
        {!value.archetype && (
          <p className="text-[11px] text-muted-foreground">
            Waiting for a matched golden-path archetype from repo detection before this can generate a proposal.
          </p>
        )}
        {draftError && !drafting && (
          <p className="text-xs text-destructive">{draftError}</p>
        )}
        {draft?.infra_proposal && draft.source !== infraSource && draft.status !== "INFRA_APPROVED" && (
          <div className="rounded-md border border-warning/40 bg-warning/5 p-2.5 text-xs" data-testid="stale-source-notice">
            <span className="font-medium">This proposal is out of date.</span> It was generated for{" "}
            <b>{draft.source === "existing" ? "Existing resources" : "AI-created"}</b>, but you have since selected{" "}
            <b>{infraSource === "existing" ? "Existing resources" : "AI-created"}</b>.{" "}
            {infraSource === "existing"
              ? "Find and pick your existing resources, then click Preview Infrastructure (AI) to generate a proposal that attaches them."
              : "Click Preview Infrastructure (AI) to generate a fresh AI-created proposal."}
          </div>
        )}
        {draft?.infra_proposal?.no_additional_infrastructure && (
          <div className="space-y-3" data-testid="no-extra-infra">
            <div className="flex items-center gap-2 rounded-md border border-success/40 bg-success/5 p-3 text-xs">
              <ShieldCheck className="h-4 w-4 shrink-0 text-success" />
              <div className="flex-1">
                <div className="text-sm font-medium">Nothing extra to build</div>
                <p className="mt-0.5 text-muted-foreground">{draft.infra_proposal.needs_summary}</p>
              </div>
              {draft.status === "INFRA_APPROVED" ? (
                <span className="flex items-center gap-1 text-[11px] font-medium text-success">
                  <CheckCircle2 className="h-3.5 w-3.5" /> Selected for this project
                </span>
              ) : (
                <Button size="sm" onClick={handleUseThisInfrastructure} disabled={approvingDraft || draft.source !== infraSource}>
                  {approvingDraft ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
                  Use this infrastructure
                </Button>
              )}
            </div>
            <div className="rounded-md border p-3 text-xs">
              <div className="mb-1 font-medium">Already provided by the platform</div>
              <ul className="list-disc space-y-0.5 pl-5 text-muted-foreground">
                {(draft.infra_proposal.platform_provides ?? []).map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
              <p className="mt-2 text-muted-foreground">
                No additional infrastructure cost. If the app also needs a database, cache or object storage, turn it on in
                the Requirements above and generate again — only then is anything designed.
              </p>
            </div>
          </div>
        )}
        {draft?.infra_proposal && !draft.infra_proposal.no_additional_infrastructure && (
          <div className="space-y-3">
            {draft.infra_proposal.needs_summary && (
              <p className="text-[11px] text-muted-foreground" data-testid="needs-summary">
                {draft.infra_proposal.needs_summary}
              </p>
            )}
            <div
              className={`flex items-center gap-1.5 rounded-md border p-2 text-xs ${
                draft.readiness_outcome === "require_approval"
                  ? "border-destructive/40 bg-destructive/5"
                  : draft.readiness_outcome === "warn"
                  ? "border-warning/40 bg-warning/5"
                  : "border-success/40 bg-success/5"
              }`}
            >
              {draft.readiness_outcome === "require_approval" ? (
                <ShieldX className="h-3.5 w-3.5 text-destructive" />
              ) : draft.readiness_outcome === "warn" ? (
                <ShieldAlert className="h-3.5 w-3.5 text-warning" />
              ) : (
                <ShieldCheck className="h-3.5 w-3.5 text-success" />
              )}
              <span className="flex-1">
                {draft.status === "INFRA_APPROVED" ? "Selected" : "Pending approval"} — est.{" "}
                <span className="font-medium">${draft.infra_proposal.estimated_monthly_cost_usd.toFixed(2)}/mo</span>
                {draft.infra_proposal.cost_estimate?.source === "aws-price-list" ? (
                  <span className="ml-1 text-[10px] text-muted-foreground">from AWS prices</span>
                ) : draft.infra_proposal.cost_estimate?.source === "ai_estimate_unverified" ? (
                  <span className="ml-1 text-[10px] text-warning" title={draft.infra_proposal.cost_estimate.reason}>
                    unverified AI estimate
                  </span>
                ) : null}
              </span>
              {draft.status === "INFRA_PENDING_APPROVAL" && (
                <Button size="sm" onClick={handleUseThisInfrastructure} disabled={approvingDraft || policyBlockers.length > 0 || draft.source !== infraSource}
                  title={policyBlockers.length > 0 ? "Blocked by infrastructure policy - edit the proposal to fix it" : undefined}>
                  {approvingDraft ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
                  Use this infrastructure
                </Button>
              )}
              {draft.status === "INFRA_APPROVED" && (
                <span className="flex items-center gap-1 text-[11px] font-medium text-success">
                  <CheckCircle2 className="h-3.5 w-3.5" /> Selected for this project
                </span>
              )}
            </div>

            {policyBlockers.length > 0 && (
              <div className="space-y-1 rounded-md border border-destructive/40 bg-destructive/5 p-2 text-xs">
                <div className="flex items-center gap-1.5 font-medium text-destructive">
                  <ShieldX className="h-3.5 w-3.5" /> Blocked by infrastructure policy — this can't be approved as is
                </div>
                <ul className="list-disc space-y-0.5 pl-5 text-muted-foreground">
                  {policyBlockers.map((f) => (
                    <li key={`${f.rule}-${f.resource}`}>{f.message}</li>
                  ))}
                </ul>
                <p className="text-muted-foreground">Use "Edit with AI" below (e.g. "enable storage encryption") to fix it.</p>
              </div>
            )}

            <div className="flex items-center justify-between">
              <Label className="text-xs font-semibold uppercase text-muted-foreground">
                {showComparison ? "AI proposal vs. standard topology" : "Topology"}
                {draft.source === "existing" && (
                  <Badge variant="secondary" className="ml-2 text-[10px]">
                    {Object.keys(draft.existing_resources).length} existing attached · retained
                  </Badge>
                )}
                {draft.parent_draft_id && (
                  <Badge variant="outline" className="ml-2 text-[10px]">Revised</Badge>
                )}
              </Label>
              <Button size="sm" variant="ghost" onClick={() => setShowComparison((v) => !v)}>
                {showComparison ? "Hide comparison" : "Compare with standard topology"}
              </Button>
            </div>
            {showComparison ? (
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-1">
                  <p className="text-[11px] font-medium text-muted-foreground">AI proposal</p>
                  <InfraTopologyGraph topology={draft.infra_proposal.topology} />
                </div>
                <div className="space-y-1">
                  <p className="text-[11px] font-medium text-muted-foreground">
                    Standard (what the platform creates without AI)
                  </p>
                  <InfraTopologyGraph topology={standardTopology} />
                </div>
              </div>
            ) : (
              <InfraTopologyGraph topology={draft.infra_proposal.topology} />
            )}

            <div className="space-y-1.5 rounded-md border p-3">
              <Label htmlFor="infra-edit-instruction" className="text-xs font-semibold uppercase text-muted-foreground">
                Edit with AI
              </Label>
              <div className="flex gap-2">
                <Input
                  id="infra-edit-instruction"
                  placeholder='e.g. "add a Redis cache" or "make the database Multi-AZ"'
                  value={editInstruction}
                  onChange={(e) => setEditInstruction(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") handleEditInfrastructure(); }}
                  disabled={editing || draft.status === "INFRA_PROVISIONING"}
                />
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={handleEditInfrastructure}
                  disabled={editing || editInstruction.trim().length < 3 || draft.status === "INFRA_PROVISIONING"}
                >
                  {editing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                  Apply
                </Button>
              </div>
              <p className="text-[11px] text-muted-foreground">
                Produces a revised proposal you review and approve again — never applied automatically, and it
                can never remove a resource you attached from your existing infrastructure.
              </p>
            </div>

            <div className="grid grid-cols-2 gap-3 text-xs">
              <div>
                <div className="mb-1 font-medium">Cost breakdown</div>
                <ul className="space-y-0.5 text-muted-foreground">
                  {draft.infra_proposal.cost_breakdown.map((item) => (
                    <li key={item.resource} className="flex justify-between">
                      <span>{item.resource}</span>
                      <span>${item.monthly_usd.toFixed(2)}/mo</span>
                    </li>
                  ))}
                </ul>
                {draft.infra_proposal.cost_estimate?.unpriced && draft.infra_proposal.cost_estimate.unpriced.length > 0 && (
                  <p className="mt-1 text-[10px] text-warning">
                    Not included: {draft.infra_proposal.cost_estimate.unpriced.map((u) => u.resource).join(", ")} (no fixed price available).
                  </p>
                )}
                {draft.infra_proposal.cost_estimate?.assumptions && draft.infra_proposal.cost_estimate.assumptions.length > 0 && (
                  <p className="mt-1 text-[10px] text-muted-foreground">{draft.infra_proposal.cost_estimate.assumptions.join(" · ")}</p>
                )}
              </div>
              <div>
                <div className="mb-1 font-medium">
                  Policy checks
                  {draft.infra_proposal.policy_evaluation ? (
                    <span className="ml-1.5 text-[10px] font-normal text-muted-foreground">independently checked (OPA)</span>
                  ) : (
                    <span className="ml-1.5 text-[10px] font-normal text-warning">AI self-reported</span>
                  )}
                </div>
                <ul className="space-y-0.5">
                  {draft.infra_proposal.policy_checks.map((check) => {
                    const status = check.status ?? (check.passed ? "pass" : "fail");
                    return (
                      <li
                        key={check.check}
                        title={check.detail}
                        className={status === "fail" ? "text-destructive" : status === "warn" ? "text-warning" : "text-success"}
                      >
                        {status === "fail" ? "✗" : status === "warn" ? "⚠" : "✓"} {check.check}
                      </li>
                    );
                  })}
                </ul>
              </div>
            </div>

            <details className="text-xs">
              <summary className="cursor-pointer text-muted-foreground">Terraform (documentation/audit only — not executed)</summary>
              <pre className="mt-1 max-h-40 overflow-auto rounded bg-muted p-2 text-[10px]">{draft.infra_proposal.iac_terraform}</pre>
            </details>

            {/* ── Phase 7: real provisioning execution (never auto-applied) ── */}
            <div className="space-y-2 rounded-md border-2 border-dashed p-3">
              <div className="flex items-center justify-between">
                <Label className="text-xs font-semibold uppercase text-muted-foreground">
                  Real AWS provisioning
                </Label>
                {draft.status === "INFRA_APPROVED" && (
                  <Button size="sm" variant="secondary" onClick={handleCreateChangeSet} disabled={changeSetLoading}>
                    {changeSetLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                    Preview Real AWS Changes
                  </Button>
                )}
              </div>

              {draft.status === "INFRA_APPROVED" && (
                <p className="text-[11px] text-muted-foreground">
                  This computes a real AWS CloudFormation Change Set — a zero-risk dry run. No resource is
                  created or modified until you explicitly execute it below.
                </p>
              )}

              {provisioningError && <p className="text-xs text-destructive">{provisioningError}</p>}

              {draft.status === "INFRA_CHANGE_SET_FAILED" && (
                <>
                  <p className="text-xs text-destructive">{draft.provisioning_error}</p>
                  <InfraFailureAnalysisPanel draftId={draft.draft_id} onUseSuggestedEdit={setEditInstruction} />
                </>
              )}

              {draft.status === "INFRA_CHANGE_SET_READY" && (
                <div className="space-y-2">
                  <div className="text-xs font-medium">
                    Real Change Set — exactly what will be created in your AWS account:
                  </div>
                  {draft.change_set_changes.length === 0 ? (
                    <p className="text-xs text-muted-foreground">No changes — this stack already matches the proposal.</p>
                  ) : (
                    <ul className="space-y-0.5 text-xs">
                      {draft.change_set_changes.map((c) => (
                        <li key={c.logical_id} className="flex justify-between">
                          <span className="text-success">{c.action}</span>
                          <span>{c.logical_id}</span>
                          <span className="text-muted-foreground">{c.resource_type}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                  <label className="flex items-start gap-2 text-xs">
                    <input
                      type="checkbox"
                      checked={executeConfirmed}
                      onChange={(e) => setExecuteConfirmed(e.target.checked)}
                      className="mt-0.5 h-3.5 w-3.5 accent-destructive"
                    />
                    I understand this will create real, billable AWS resources.
                  </label>
                  <Button
                    size="sm"
                    variant="destructive"
                    onClick={handleExecute}
                    disabled={!executeConfirmed || executeLoading}
                  >
                    {executeLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                    Execute — Create Real Resources
                  </Button>
                </div>
              )}

              {draft.status === "INFRA_PROVISIONING" && (
                <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" /> Provisioning real AWS resources — this can take a few minutes…
                </p>
              )}

              {draft.status === "INFRA_PROVISIONED" && (
                <div className="space-y-1 text-xs">
                  <p className="flex items-center gap-1.5 text-success">
                    <ShieldCheck className="h-3.5 w-3.5" /> Real AWS resources provisioned successfully.
                  </p>
                  {Object.entries(draft.provisioning_outputs).map(([key, val]) => (
                    <div key={key} className="flex justify-between text-muted-foreground">
                      <span>{key}</span>
                      <span className="text-code">{val}</span>
                    </div>
                  ))}
                </div>
              )}

              {draft.status === "INFRA_PROVISIONING_FAILED" && (
                <>
                  <p className="flex items-center gap-1.5 text-xs text-destructive">
                    <ShieldX className="h-3.5 w-3.5" /> Provisioning failed: {draft.provisioning_error}
                  </p>
                  <InfraFailureAnalysisPanel draftId={draft.draft_id} onUseSuggestedEdit={setEditInstruction} />
                </>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
