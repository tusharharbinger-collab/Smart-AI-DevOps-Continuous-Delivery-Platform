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
import { useEffect, useState } from "react";
import { Info } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { InfraDraft } from "@/api/infraDrafts";
import { InfraBuildFlow } from "@/components/wizard/InfraBuildFlow";
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

function DetectedBadge() {
  return (
    <Badge variant="secondary" className="ml-1.5 gap-1 text-[10px]">
      <Info className="h-2.5 w-2.5" /> Detected
    </Badge>
  );
}

export function RequirementsForm({ detection, value, onChange, onDraftChange }: RequirementsFormProps) {
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
              // Real gap found live (Phase D): a hint can now be a dependency name ("psycopg2-binary") OR
              // a cited code-scan match ("src/db.py:12 — create_engine(...)") — "Found dependency:" read
              // wrong for the latter, so this reads generically for both.
              <span className="text-[11px] text-muted-foreground">Detected: {value.detected_database_hint}</span>
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
          <div>
            <span className="flex items-center text-sm">
              Needs a cache
              {infra?.needs_cache && <DetectedBadge />}
            </span>
            {value.detected_cache_hint && (
              <span className="text-[11px] text-muted-foreground">Detected: {value.detected_cache_hint}</span>
            )}
          </div>
          <Switch checked={value.needs_cache} onCheckedChange={(checked) => onChange({ ...value, needs_cache: checked })} />
        </div>

        <div className="flex items-center justify-between rounded-md border p-3">
          <div>
            <span className="flex items-center text-sm">
              Needs object storage
              {infra?.needs_object_storage && <DetectedBadge />}
            </span>
            {value.detected_storage_hint && (
              <span className="text-[11px] text-muted-foreground">Detected: {value.detected_storage_hint}</span>
            )}
          </div>
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

      {/* AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §7 (Phase F) - the chat -> confirm -> build flow. */}
      <InfraBuildFlow value={value} onDraftChange={onDraftChange} />
    </div>
  );
}
