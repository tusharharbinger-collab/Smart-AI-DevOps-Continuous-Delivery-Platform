/**
 * frontend/src/pages/NewProject.tsx — Phase 8, deliverable 8.5.
 *
 * Render-style 3-step service creation:
 *   [1. Choose Repository] → [2. Configure Build & Test] → [3. Policy & Deploy]
 *
 * Step 1 connects a real GitHub account over OAuth ("Connect GitHub"), the
 * way Render does — the user authorizes on github.com and comes back with
 * their actual repositories listed. No token is ever typed into, or stored
 * by, the browser: the access token lives server-side in Redis keyed by
 * user id (see services/api-gateway/src/routers/github_router.py).
 *
 * The "Public Git Repository" tab stays as an escape hatch for anyone who
 * does not want to connect an account at all. "Existing Image" is a third
 * option (the way Render's wizard works): deploy a pre-built image straight
 * into the canary loop with no clone/build/test stage at all — see
 * generate_project_pipeline_yaml()'s `source_type == "existing_image"`
 * branch in services/api-gateway/src/routers/projects_router.py.
 */
import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import {
  AlertTriangle, ArrowLeft, ArrowRight, Ban, Check, Container, FolderGit2, Hammer,
  KeyRound, Link2, Loader2, Lock, LogOut, Plus, RefreshCw, Rocket, Search, ShieldCheck, Sparkles,
  Trash2, Unlock, XCircle,
} from "lucide-react";
import {
  disconnectGitHub, getAuthorizeUrl, getBuildDetection, getGitHubStatus, getRepoReport, listBranches, listRepos,
  parseRepoUrl, type BuildDetection, type GitHubRepo, type GitHubStatus, type RepoReport,
} from "@/api/github";
import {
  createRegistryCredential, listRegistryCredentials, parseImageRef, type RegistryCredential,
} from "@/api/registry";
import {
  createProject, getBuildPreviewLogs, getBuildPreviewResult, startBuildPreview,
  type BuildPreviewResult, type CreateProjectInput,
} from "@/api/projects";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";

const STEPS = ["Choose Repository", "Configure Build & Test", "Progressive Policy & Deploy"] as const;
const TRAFFIC_PRESET = [10, 25, 50, 100];
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const APPROVAL_ROLES = ["developer", "lead-sre", "platform-admin"];

type SourceTab = "provider" | "public" | "image";

export function NewProject() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [step, setStep] = useState(0);

  // Step 1 — repository
  const [sourceTab, setSourceTab] = useState<SourceTab>("provider");
  const [status, setStatus] = useState<GitHubStatus | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [repoSearch, setRepoSearch] = useState("");
  const [repos, setRepos] = useState<GitHubRepo[]>([]);
  const [loadingRepos, setLoadingRepos] = useState(false);
  const [repoError, setRepoError] = useState<string | null>(null);
  const [selectedRepo, setSelectedRepo] = useState<GitHubRepo | null>(null);
  const [manualUrl, setManualUrl] = useState("");

  // Step 1 — existing image
  const [imageUrl, setImageUrl] = useState("");
  const [credentials, setCredentials] = useState<RegistryCredential[]>([]);
  const [selectedCredentialId, setSelectedCredentialId] = useState<string>("none");
  const [showAddCredential, setShowAddCredential] = useState(false);
  const [newCred, setNewCred] = useState({ name: "", registry: "", username: "", secret: "" });
  const [savingCred, setSavingCred] = useState(false);

  // Step 2 — build & test
  const [form, setForm] = useState({
    name: "",
    branch: "main",
    root_directory: "./",
    dockerfile_path: "Dockerfile",
    language: "",
    // Guaranteed Live Web App CI/CD — only ever set by auto-detection
    // (see shared/repo_scanner.py's BuildDetection.framework), never a
    // manual dropdown choice: "spa" | "nextjs" | "node-server" | "".
    framework: "",
    start_command: "",
    manifest_path: "",
    // Real gap found live: this used to default to a Python-specific
    // "pytest tests/" regardless of the project's actual language — a JS
    // (or any non-Python) repo would silently submit a command that could
    // never find matching tests, and it ran in pipeline-worker's own
    // container which may not even have the right runtime installed. Test
    // commands are genuinely optional (blank = skip; the repo's own
    // Dockerfile, if it has one, often already runs its real tests as a
    // build layer) — never guessed here.
    test_command: "",
    container_image: "",
    active_production_tag: "v1.0.0",
    canary_tag: "v1.1.0",
    port: 8080,
    health_check_path: "/healthz",
    path_prefix: "",
  });
  const [branches, setBranches] = useState<string[]>([]);

  // Step 2 — build method: which of the two backend-supported build paths
  // (dockerfile vs. language+startCommand synthesis) is currently active,
  // and whether the "Auto-detect" pill drove that choice (see
  // shared/repo_scanner.py's deterministic, non-AI BuildDetection).
  const [buildTab, setBuildTab] = useState<"dockerfile" | "language">("dockerfile");
  const [autoMode, setAutoMode] = useState(true);
  const [detection, setDetection] = useState<BuildDetection | null>(null);
  const [detecting, setDetecting] = useState(false);
  const [detectionError, setDetectionError] = useState<string | null>(null);
  const [detectedFor, setDetectedFor] = useState<string | null>(null);
  const [repoReport, setRepoReport] = useState<RepoReport | null>(null);
  const [loadingReport, setLoadingReport] = useState(false);

  // Build & Test preview — a real, human-triggered clone->build->test dry
  // run (build_preview.py) with live logs, so a failure surfaces during
  // onboarding instead of three real pipeline stages deep into a rollout.
  const [previewRunId, setPreviewRunId] = useState<string | null>(null);
  const [previewLogs, setPreviewLogs] = useState<string[]>([]);
  const [previewResult, setPreviewResult] = useState<BuildPreviewResult | null>(null);
  const [previewStarting, setPreviewStarting] = useState(false);

  // Step 3 — guardrails
  const [confidenceFloor, setConfidenceFloor] = useState(0.8);
  const [minSampleSize, setMinSampleSize] = useState(100);
  const [maxCostDelta, setMaxCostDelta] = useState(15);
  const [provisionCluster, setProvisionCluster] = useState(true);
  // Module 8 — real second deployment target. AWS ECS Fargate is the only
  // one the wizard offers (2026-09-16 scope decision — see BACKLOG.md P3
  // #9): Kind/Kubernetes is deprioritized, not deleted, so the type union
  // and the api-gateway payload field stay "kubernetes" | "aws_ecs" for the
  // pipelines that already run on Kind, but a NEW project always starts on
  // "aws_ecs" with no user-facing toggle back to "kubernetes" anymore.
  const [deployTarget] = useState<"kubernetes" | "aws_ecs">("aws_ecs");
  const [awsRegion, setAwsRegion] = useState("us-east-1");
  const [submitting, setSubmitting] = useState(false);

  // Step 3 — deploy policy: real gap found live — freeze windows and
  // approver roles used to be hardcoded server-side for every project,
  // never actually wizard-configurable. Blue-green is only real for AWS
  // ECS (the server rejects blue_green + kubernetes with a 422) — since
  // deployTarget is currently fixed to "aws_ecs" above, both are shown.
  const [deployMode, setDeployMode] = useState<"canary" | "blue_green">("canary");
  const [blockedWindows, setBlockedWindows] = useState<
    { days: string[]; start_time: string; end_time: string }[]
  >([]);
  const [manualApprovalRequired, setManualApprovalRequired] = useState(true);
  const [manualApprovalRoles, setManualApprovalRoles] = useState<string[]>(["lead-sre", "platform-admin"]);

  const repoUrl = selectedRepo?.clone_url ?? manualUrl;
  const repoPrivate = selectedRepo?.private ?? false;
  const isImageSource = sourceTab === "image";

  async function loadRepos() {
    setLoadingRepos(true);
    setRepoError(null);
    try {
      const res = await listRepos(undefined, repoSearch || undefined);
      setRepos(res.repos);
      if (res.repos.length === 0) setRepoError("This account has no repositories the app can see.");
    } catch (err) {
      setRepoError((err as Error).message);
      setRepos([]);
    } finally {
      setLoadingRepos(false);
    }
  }

  async function refreshStatus() {
    try {
      const s = await getGitHubStatus();
      setStatus(s);
      if (s.connected || s.server_token_fallback) await loadRepos();
    } catch {
      setStatus(null);
    }
  }

  // On mount, and again right after GitHub redirects back here with
  // ?github=connected, so the repo list appears without a manual refresh.
  useEffect(() => {
    const outcome = searchParams.get("github");
    if (outcome === "connected") {
      toast.success("GitHub connected");
    } else if (outcome === "error") {
      toast.error("GitHub connection failed", { description: searchParams.get("reason") ?? undefined });
    }
    if (outcome) {
      const next = new URLSearchParams(searchParams);
      next.delete("github");
      next.delete("reason");
      setSearchParams(next, { replace: true });
    }
    refreshStatus();
    refreshCredentials();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function refreshCredentials() {
    try {
      const res = await listRegistryCredentials();
      setCredentials(res.credentials);
    } catch {
      setCredentials([]);
    }
  }

  function useImageUrl() {
    const parsed = parseImageRef(imageUrl);
    setSelectedRepo(null);
    setManualUrl("");
    setForm((f) => ({
      ...f,
      name: f.name || parsed.image.split("/").pop() || "",
      container_image: parsed.image,
      canary_tag: parsed.tag || f.canary_tag,
    }));
  }

  async function handleSaveCredential() {
    if (!newCred.name.trim() || !newCred.registry.trim() || !newCred.username.trim() || !newCred.secret.trim()) {
      toast.error("Fill in name, registry, username and password/token");
      return;
    }
    setSavingCred(true);
    try {
      const saved = await createRegistryCredential(newCred);
      await refreshCredentials();
      setSelectedCredentialId(saved.id);
      setShowAddCredential(false);
      setNewCred({ name: "", registry: "", username: "", secret: "" });
      toast.success(`Credential "${saved.name}" saved`);
    } catch (err) {
      toast.error("Could not save credential", { description: (err as Error).message });
    } finally {
      setSavingCred(false);
    }
  }

  async function handleConnect() {
    setConnecting(true);
    try {
      const { authorize_url } = await getAuthorizeUrl();
      // Full-page navigation rather than a popup: the callback lands on the
      // api-gateway origin, so a popup would need cross-origin postMessage
      // coordination for no benefit — step 1 holds no form data to lose.
      window.location.href = authorize_url;
    } catch (err) {
      toast.error("Could not start GitHub authorization", { description: (err as Error).message });
      setConnecting(false);
    }
  }

  async function handleDisconnect() {
    try {
      await disconnectGitHub();
      setRepos([]);
      setSelectedRepo(null);
      await refreshStatus();
      toast.success("GitHub disconnected");
    } catch (err) {
      toast.error("Disconnect failed", { description: (err as Error).message });
    }
  }

  async function selectRepo(repo: GitHubRepo) {
    setSelectedRepo(repo);
    setManualUrl("");
    setForm((f) => ({
      ...f,
      name: repo.name,
      branch: repo.default_branch,
      // Real gap found live (2026-09-16): this used to auto-fill
      // "registry.internal/{name}" unconditionally — a placeholder host
      // that only resolves inside the local dev network. AWS ECS is now
      // the only deploy target the wizard offers, and a real ECS Fargate
      // task can never reach that host at all (a project silently shipped
      // with it just retries a DNS lookup forever, never actually
      // deploying). No safe default exists without knowing the caller's
      // real AWS account id, so this is intentionally left for the human
      // to fill in with a real ECR URI — see the field's own helper text.
      container_image: f.container_image,
    }));
    const parsed = parseRepoUrl(repo.clone_url);
    if (parsed) {
      try {
        const res = await listBranches(parsed.owner, parsed.repo);
        setBranches(res.branches.map((b) => b.name));
      } catch {
        setBranches([repo.default_branch]);
      }
    }
    resetDetection();
  }

  function useManualUrl() {
    const parsed = parseRepoUrl(manualUrl);
    setSelectedRepo(null);
    setForm((f) => ({
      ...f,
      name: f.name || parsed?.repo || "",
      // See selectRepo's comment above — never auto-guess a registry host.
      container_image: f.container_image,
    }));
    setBranches([]);
    resetDetection();
  }

  function resetDetection() {
    setDetection(null);
    setDetectionError(null);
    setDetectedFor(null);
    setRepoReport(null);
    setAutoMode(true);
    resetPreview();
  }

  /**
   * Calls shared/repo_scanner.py's deterministic build-method detection
   * (Dockerfile / smartcd.yaml manifest / language-signature match — never
   * an AI guess) and pre-fills whichever of the two backend-supported build
   * paths it found. Every field it fills stays human-editable — a "high"
   * confidence result pre-checks itself, a "low" one (or an outright
   * "unsupported" verdict) still populates what it can and surfaces exactly
   * what's missing via `issues`, matching the human-in-the-loop requirement
   * that AI/automation only ever proposes, never silently finalizes.
   */
  async function runDetection() {
    const parsed = parseRepoUrl(repoUrl);
    if (!parsed) {
      setDetectionError("Could not parse a GitHub owner/repo from this URL — pick a build method manually below.");
      setDetection(null);
      setRepoReport(null);
      return;
    }
    const key = `${parsed.owner}/${parsed.repo}@${form.branch || "main"}`;
    setDetecting(true);
    setLoadingReport(true);
    setDetectionError(null);
    try {
      const [buildRes, reportRes] = await Promise.allSettled([
        getBuildDetection(parsed.owner, parsed.repo, form.branch || "main"),
        getRepoReport(parsed.owner, parsed.repo, form.branch || "main"),
      ]);

      if (buildRes.status === "fulfilled") {
        setDetection(buildRes.value);
        setDetectedFor(key);
        applyDetection(buildRes.value);
      } else {
        setDetectionError(buildRes.reason?.message || "Detection failed");
        setDetection(null);
      }

      if (reportRes.status === "fulfilled") {
        setRepoReport(reportRes.value);
      } else {
        setRepoReport(null);
      }
    } finally {
      setDetecting(false);
      setLoadingReport(false);
    }
  }

  function applyDetection(d: BuildDetection) {
    // Guaranteed Live Web App CI/CD — a static site and a Vite/CRA "spa"
    // have no runtime start command at all (nginx just serves files), so
    // a real detection for either is already complete without one.
    const noStartCommandNeeded = d.language === "static" || d.framework === "spa";
    // Only pre-fill Networking from the suggestion if the user hasn't
    // already moved off the original hardcoded defaults — a real edit
    // (manual or from a PRIOR detection) is never silently overwritten,
    // matching this wizard's existing "never guess over a human's own
    // input" rule elsewhere.
    const networkingUntouched = form.health_check_path === "/healthz" && form.port === 8080;
    if (d.dockerfile_path) {
      setBuildTab("dockerfile");
      setForm((f) => ({
        ...f,
        dockerfile_path: d.dockerfile_path!,
        test_command: d.test_command ?? f.test_command,
      }));
    } else if (d.language && (d.start_command || noStartCommandNeeded)) {
      setBuildTab("language");
      setForm((f) => ({
        ...f,
        language: d.language!,
        framework: d.framework ?? "",
        start_command: d.start_command ?? "",
        manifest_path: d.manifest_path ?? f.manifest_path,
        test_command: d.test_command ?? f.test_command,
        health_check_path: networkingUntouched ? d.suggested_health_check_path : f.health_check_path,
        port: networkingUntouched ? d.suggested_port : f.port,
      }));
    }
    // "unsupported" (or a language synthesis result missing startCommand):
    // leave existing form values as-is and let the issues list explain why
    // — the user picks Dockerfile or Language runtime manually below.
  }

  function addBlockedWindow() {
    setBlockedWindows((w) => [...w, { days: ["Friday"], start_time: "16:00", end_time: "23:59" }]);
  }

  function updateBlockedWindow(index: number, patch: Partial<{ days: string[]; start_time: string; end_time: string }>) {
    setBlockedWindows((w) => w.map((win, i) => (i === index ? { ...win, ...patch } : win)));
  }

  function removeBlockedWindow(index: number) {
    setBlockedWindows((w) => w.filter((_, i) => i !== index));
  }

  function toggleWindowDay(index: number, day: string) {
    setBlockedWindows((w) =>
      w.map((win, i) =>
        i === index
          ? { ...win, days: win.days.includes(day) ? win.days.filter((d) => d !== day) : [...win.days, day] }
          : win
      )
    );
  }

  function toggleApprovalRole(role: string) {
    setManualApprovalRoles((roles) => (roles.includes(role) ? roles.filter((r) => r !== role) : [...roles, role]));
  }

  function resetPreview() {
    setPreviewRunId(null);
    setPreviewLogs([]);
    setPreviewResult(null);
  }

  /**
   * Kicks off the real build_preview.py dry run: clone -> build (real
   * Dockerfile or synthesized) -> test, no deploy/cluster involvement.
   * Human-triggered, never automatic — running `docker build` server-side
   * is real work, unlike the free file-signature scan above. Polling (not
   * SSE) matches this platform's own documented reason for that choice
   * elsewhere: a preview run is short-lived and this keeps the client
   * simple; see useLiveLogs.ts for why a long-running stream uses fetch+SSE
   * instead when one actually needs to.
   */
  async function runBuildTestPreview() {
    resetPreview();
    setPreviewStarting(true);
    try {
      const res = await startBuildPreview({
        repo_url: repoUrl,
        ref: form.branch || "main",
        repo_private: repoPrivate,
        root_directory: form.root_directory,
        dockerfile_path: buildTab === "dockerfile" ? form.dockerfile_path || null : null,
        language: buildTab === "language" ? form.language || null : null,
        framework: buildTab === "language" ? form.framework || null : null,
        manifest_path: buildTab === "language" ? form.manifest_path || null : null,
        start_command: buildTab === "language" ? form.start_command || null : null,
        test_command: form.test_command || null,
      });
      setPreviewRunId(res.run_id);
    } catch (err) {
      toast.error("Could not start the build preview", { description: (err as Error).message });
    } finally {
      setPreviewStarting(false);
    }
  }

  // Poll logs + result every 1.5s while a preview is running; stop the
  // moment it reaches a terminal state (succeeded/failed).
  useEffect(() => {
    if (!previewRunId || previewResult?.status === "succeeded" || previewResult?.status === "failed") return;
    let cancelled = false;
    const tick = async () => {
      try {
        const [logsRes, resultRes] = await Promise.all([
          getBuildPreviewLogs(previewRunId),
          getBuildPreviewResult(previewRunId),
        ]);
        if (cancelled) return;
        setPreviewLogs(logsRes.lines);
        setPreviewResult(resultRes);
      } catch {
        // Transient — the next tick retries; the result endpoint 404ing
        // permanently (expired/never-started) would mean previewRunId
        // itself is stale, not worth surfacing mid-poll as an error.
      }
    };
    tick();
    const interval = setInterval(tick, 1500);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [previewRunId, previewResult?.status]);

  async function handleDeploy() {
    setSubmitting(true);
    try {
      const payload: CreateProjectInput = {
        name: form.name,
        source_type: isImageSource ? "existing_image" : "repository",
        repo_url: isImageSource ? null : repoUrl,
        repo_private: repoPrivate,
        branch: form.branch,
        root_directory: form.root_directory,
        dockerfile_path: isImageSource || buildTab !== "dockerfile" ? null : form.dockerfile_path || null,
        language: isImageSource || buildTab !== "language" ? null : form.language || null,
        framework: isImageSource || buildTab !== "language" ? null : form.framework || null,
        start_command: isImageSource || buildTab !== "language" ? null : form.start_command || null,
        manifest_path: isImageSource || buildTab !== "language" ? null : form.manifest_path || null,
        test_command: isImageSource ? null : form.test_command || null,
        container_image: form.container_image,
        active_production_tag: form.active_production_tag,
        canary_tag: form.canary_tag,
        registry_credential_id:
          isImageSource && selectedCredentialId !== "none" ? selectedCredentialId : null,
        port: form.port,
        health_check_path: form.health_check_path,
        path_prefix: form.path_prefix.trim() || null,
        guardrails: {
          confidence_floor: confidenceFloor,
          min_sample_size: minSampleSize,
          max_cost_delta_percent: maxCostDelta,
        },
        deploy_policy: {
          deploy_mode: deployMode,
          blocked_deploy_windows: blockedWindows,
          manual_approval_required: manualApprovalRequired,
          manual_approval_roles: manualApprovalRequired ? manualApprovalRoles : [],
        },
        traffic_steps: TRAFFIC_PRESET,
        provision_cluster: provisionCluster,
        deploy_target: deployTarget,
        aws_region: awsRegion,
      };
      const res = await createProject(payload);
      if (res.cluster_provisioning.attempted && !res.cluster_provisioning.succeeded) {
        toast.warning("Project created, cluster objects not applied", {
          description: res.cluster_provisioning.detail ?? "pipeline-worker could not reach the cluster.",
        });
      } else {
        toast.success(`${form.name} created`);
      }
      navigate(`/projects/${res.project_id}`);
    } catch (err) {
      toast.error("Could not create service", { description: (err as Error).message });
    } finally {
      setSubmitting(false);
    }
  }

  const canContinueStep0 = isImageSource ? Boolean(form.container_image.trim()) : Boolean(repoUrl);
  // Guaranteed Live Web App CI/CD — a static site or a Vite/CRA "spa" has
  // no start command at all (nginx just serves files), so requiring one
  // here would block a perfectly buildable project from continuing.
  const languageNeedsNoStartCommand = form.language === "static" || form.framework === "spa";
  const hasBuildConfig =
    isImageSource ||
    (buildTab === "dockerfile" && Boolean(form.dockerfile_path.trim())) ||
    (buildTab === "language" &&
      Boolean(form.language.trim() && (form.start_command.trim() || languageNeedsNoStartCommand)));
  const canContinueStep1 = Boolean(form.name.trim() && form.container_image.trim()) && hasBuildConfig;

  // Auto-detect once per repo+branch combo the moment the user reaches
  // step 2, so the checklist is already there instead of an empty form —
  // a manual "Re-analyze" button below covers a branch change afterwards.
  useEffect(() => {
    if (step !== 1 || isImageSource || detecting) return;
    const parsed = parseRepoUrl(repoUrl);
    if (!parsed) return;
    const key = `${parsed.owner}/${parsed.repo}@${form.branch || "main"}`;
    if (autoMode && detectedFor !== key) {
      runDetection();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, repoUrl, form.branch, autoMode]);

  return (
    <div className="mx-auto max-w-4xl space-y-5">
      <div>
        <Button variant="ghost" size="sm" onClick={() => navigate("/projects")} className="mb-2 -ml-2">
          <ArrowLeft className="h-3.5 w-3.5" /> Back to overview
        </Button>
        <h1 className="text-xl font-semibold tracking-tight">Create a new service</h1>
        <p className="text-sm text-muted-foreground">
          Connect a repository, describe how it builds and tests, then set the verification policy.
        </p>
      </div>

      {/* Breadcrumb stepper */}
      <div className="grid grid-cols-3 overflow-hidden rounded-md border text-center text-code text-[11px]">
        {STEPS.map((label, i) => (
          <div
            key={label}
            className={`flex items-center justify-center gap-1.5 border-b-2 px-2 py-2 transition-colors ${
              i === step
                ? "border-primary bg-primary/10 font-semibold text-primary"
                : i < step
                ? "border-success/40 text-success"
                : "border-transparent text-muted-foreground"
            }`}
          >
            <span
              className={`flex h-4 w-4 items-center justify-center rounded-full text-[10px] ${
                i === step ? "bg-primary text-primary-foreground" : i < step ? "bg-success/20" : "bg-muted"
              }`}
            >
              {i < step ? <Check className="h-2.5 w-2.5" /> : i + 1}
            </span>
            <span className="hidden sm:inline">{label}</span>
          </div>
        ))}
      </div>

      <Card>
        <CardContent className="space-y-4 p-5">
          {/* ── Step 1 ── */}
          {step === 0 && (
            <div className="space-y-4">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">Source Code</span>
                <div className="ml-auto inline-flex overflow-hidden rounded-md border">
                  <button
                    onClick={() => setSourceTab("provider")}
                    className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                      sourceTab === "provider" ? "bg-primary text-primary-foreground" : "hover:bg-accent"
                    }`}
                  >
                    Git Provider
                  </button>
                  <button
                    onClick={() => setSourceTab("public")}
                    className={`border-l px-3 py-1.5 text-xs font-medium transition-colors ${
                      sourceTab === "public" ? "bg-primary text-primary-foreground" : "hover:bg-accent"
                    }`}
                  >
                    Public Git Repository
                  </button>
                  <button
                    onClick={() => setSourceTab("image")}
                    className={`border-l px-3 py-1.5 text-xs font-medium transition-colors ${
                      sourceTab === "image" ? "bg-primary text-primary-foreground" : "hover:bg-accent"
                    }`}
                  >
                    Existing Image
                  </button>
                </div>
              </div>

              {sourceTab === "image" ? (
                <div className="space-y-4">
                  <div className="space-y-1">
                    <Label htmlFor="new-project-image-url">Image URL</Label>
                    <p className="text-[11px] text-muted-foreground">Deploy an image from a Docker registry.</p>
                    <div className="flex gap-2">
                      <div className="relative flex-1">
                        <Container className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                        <Input
                          id="new-project-image-url"
                          value={imageUrl}
                          onChange={(e) => setImageUrl(e.target.value)}
                          placeholder="docker.io/library/nginx:latest"
                          className="pl-8"
                        />
                      </div>
                      <Button variant="outline" onClick={useImageUrl} disabled={!imageUrl.trim()}>
                        <Link2 className="h-3.5 w-3.5" /> Use this
                      </Button>
                    </div>
                    <p className="text-[11px] text-muted-foreground">
                      A trailing <span className="text-code">:tag</span> is read as the canary tag — you can still
                      change it in the next step.
                    </p>
                  </div>

                  <div className="space-y-1">
                    <Label>Credential (Optional)</Label>
                    {showAddCredential ? (
                      <div className="space-y-2 rounded-md border p-3">
                        <div className="grid gap-2 sm:grid-cols-2">
                          <Input
                            placeholder="Credential name"
                            value={newCred.name}
                            onChange={(e) => setNewCred({ ...newCred, name: e.target.value })}
                          />
                          <Input
                            placeholder="Registry (e.g. docker.io, ghcr.io)"
                            value={newCred.registry}
                            onChange={(e) => setNewCred({ ...newCred, registry: e.target.value })}
                          />
                          <Input
                            placeholder="Username"
                            value={newCred.username}
                            onChange={(e) => setNewCred({ ...newCred, username: e.target.value })}
                          />
                          <Input
                            type="password"
                            placeholder="Password or access token"
                            value={newCred.secret}
                            onChange={(e) => setNewCred({ ...newCred, secret: e.target.value })}
                          />
                        </div>
                        <div className="flex justify-end gap-2">
                          <Button variant="ghost" size="sm" onClick={() => setShowAddCredential(false)}>
                            Cancel
                          </Button>
                          <Button size="sm" onClick={handleSaveCredential} disabled={savingCred}>
                            {savingCred ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <KeyRound className="h-3.5 w-3.5" />}
                            Save credential
                          </Button>
                        </div>
                        <p className="text-[11px] text-muted-foreground">
                          Stored server-side and never shown again — the platform uses it only to create the pull
                          secret this service's pods need.
                        </p>
                      </div>
                    ) : (
                      <div className="flex gap-2">
                        <Select value={selectedCredentialId} onValueChange={setSelectedCredentialId}>
                          <SelectTrigger className="flex-1"><SelectValue /></SelectTrigger>
                          <SelectContent>
                            <SelectItem value="none">No credential</SelectItem>
                            {credentials.map((c) => (
                              <SelectItem key={c.id} value={c.id}>
                                {c.name} · {c.registry} ({c.username})
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                        <Button variant="outline" onClick={() => setShowAddCredential(true)}>
                          <Plus className="h-3.5 w-3.5" /> Add credential
                        </Button>
                      </div>
                    )}
                  </div>
                </div>
              ) : sourceTab === "provider" ? (
                status?.connected || status?.server_token_fallback ? (
                  <div className="space-y-3">
                    <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-muted/30 p-3">
                      <span className="flex items-center gap-2">
                        {status.avatar_url ? (
                          <img src={status.avatar_url} alt="" className="h-6 w-6 rounded-full" />
                        ) : (
                          <FolderGit2 className="h-4 w-4 text-muted-foreground" />
                        )}
                        <span className="text-xs">
                          {status.connected ? (
                            <>
                              Connected as{" "}
                              <span className="font-semibold">@{status.login ?? "github user"}</span>
                            </>
                          ) : (
                            <>Using the server&apos;s configured GitHub token</>
                          )}
                        </span>
                      </span>
                      <span className="flex items-center gap-2">
                        <Button variant="ghost" size="sm" onClick={loadRepos} disabled={loadingRepos}>
                          {loadingRepos ? (
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                          ) : (
                            <FolderGit2 className="h-3.5 w-3.5" />
                          )}
                          Refresh
                        </Button>
                        {status.connected && (
                          <Button variant="ghost" size="sm" onClick={handleDisconnect}>
                            <LogOut className="h-3.5 w-3.5" /> Disconnect
                          </Button>
                        )}
                      </span>
                    </div>

                    <div className="relative">
                      <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                      <Input
                        value={repoSearch}
                        onChange={(e) => setRepoSearch(e.target.value)}
                        placeholder="Search your repositories…"
                        className="pl-8"
                      />
                    </div>

                    {loadingRepos ? (
                      <div className="space-y-2">
                        <Skeleton className="h-12" />
                        <Skeleton className="h-12" />
                        <Skeleton className="h-12" />
                      </div>
                    ) : repos.length > 0 ? (
                      <div className="max-h-72 space-y-1.5 overflow-y-auto rounded-md border p-1.5">
                        {repos
                          .filter((r) =>
                            repoSearch ? r.full_name.toLowerCase().includes(repoSearch.toLowerCase()) : true
                          )
                          .map((repo) => (
                            <button
                              key={repo.id}
                              onClick={() => selectRepo(repo)}
                              className={`flex w-full items-start gap-2 rounded-md border p-2 text-left transition-colors hover:bg-accent ${
                                selectedRepo?.id === repo.id ? "border-primary bg-primary/5" : "border-transparent"
                              }`}
                            >
                              {repo.private ? (
                                <Lock className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
                              ) : (
                                <Unlock className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                              )}
                              <span className="min-w-0 flex-1">
                                <span className="block truncate text-xs font-medium">{repo.full_name}</span>
                                <span className="block truncate text-[11px] text-muted-foreground">
                                  {repo.description ?? "No description"} · default: {repo.default_branch}
                                </span>
                              </span>
                              {selectedRepo?.id === repo.id && (
                                <Check className="h-3.5 w-3.5 shrink-0 text-primary" />
                              )}
                            </button>
                          ))}
                      </div>
                    ) : repoError ? (
                      <p className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
                        {repoError}
                      </p>
                    ) : null}
                  </div>
                ) : (
                  <div className="rounded-md border p-8 text-center">
                    <h3 className="text-sm font-semibold">Connect Git provider</h3>
                    <p className="mx-auto mt-1 max-w-md text-xs text-muted-foreground">
                      Connect your Git provider to deploy from your existing repositories.
                    </p>
                    <div className="mt-4 flex justify-center">
                      <Button variant="outline" onClick={handleConnect} disabled={connecting || !status?.oauth_configured}>
                        {connecting ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <FolderGit2 className="h-3.5 w-3.5" />
                        )}
                        GitHub
                      </Button>
                    </div>
                    {status && !status.oauth_configured && (
                      <p className="mx-auto mt-4 max-w-lg rounded-md border border-dashed p-3 text-left text-[11px] text-muted-foreground">
                        GitHub OAuth isn&apos;t configured on this server yet. Register an OAuth App at{" "}
                        <span className="text-code">GitHub → Settings → Developer settings → OAuth Apps</span> with
                        callback URL{" "}
                        <span className="text-code text-foreground">
                          http://localhost:8000/api/v1/integrations/github/callback
                        </span>
                        , then set <span className="text-code">GITHUB_CLIENT_ID</span> and{" "}
                        <span className="text-code">GITHUB_CLIENT_SECRET</span> in <span className="text-code">.env</span>{" "}
                        and restart the api-gateway. You can use the <strong>Public Git Repository</strong> tab meanwhile.
                      </p>
                    )}
                  </div>
                )
              ) : (
                <div className="space-y-1">
                  <Label htmlFor="new-project-manual-repo-url">Public Git repository URL</Label>
                  <div className="flex gap-2">
                    <Input
                      id="new-project-manual-repo-url"
                      value={manualUrl}
                      onChange={(e) => setManualUrl(e.target.value)}
                      placeholder="https://github.com/org/repo"
                    />
                    <Button variant="outline" onClick={useManualUrl} disabled={!manualUrl.trim()}>
                      <Link2 className="h-3.5 w-3.5" /> Use this
                    </Button>
                  </div>
                  <p className="text-[11px] text-muted-foreground">
                    Anything cloneable without credentials. Private repositories need a connected account.
                  </p>
                </div>
              )}

              {isImageSource
                ? form.container_image && (
                    <p className="rounded-md border bg-muted/40 p-2 text-code text-[11px]">
                      Selected image: <span className="font-medium text-foreground">{form.container_image}</span>
                      {selectedCredentialId !== "none" && " · with registry credential"}
                    </p>
                  )
                : repoUrl && (
                    <p className="rounded-md border bg-muted/40 p-2 text-code text-[11px]">
                      Selected: <span className="font-medium text-foreground">{repoUrl}</span>
                      {repoPrivate ? " (private)" : " (public)"}
                    </p>
                  )}
            </div>
          )}

          {/* ── Step 2 ── */}
          {step === 1 && (
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor="new-project-service-name">Service name</Label>
                <Input
                  id="new-project-service-name"
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="new-project-environment">Environment</Label>
                <Input id="new-project-environment" value="Docker / OCI Container" disabled />
              </div>
              {isImageSource && (
                <div className="space-y-1 sm:col-span-2">
                  <p className="rounded-md border border-dashed p-2 text-[11px] text-muted-foreground">
                    Deploying an existing image — there is nothing to clone, build or test. This service goes
                    straight into the progressive canary loop.
                  </p>
                </div>
              )}
              {!isImageSource && (
                <div className="space-y-1">
                  <Label htmlFor="new-project-branch">Branch</Label>
                  {branches.length > 0 ? (
                    <select
                      id="new-project-branch"
                      value={form.branch}
                      onChange={(e) => setForm({ ...form, branch: e.target.value })}
                      className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                    >
                      {branches.map((b) => (
                        <option key={b} value={b}>{b}</option>
                      ))}
                    </select>
                  ) : (
                    <Input
                      id="new-project-branch"
                      value={form.branch}
                      onChange={(e) => setForm({ ...form, branch: e.target.value })}
                    />
                  )}
                </div>
              )}
              {!isImageSource && (
                <div className="space-y-1">
                  <Label htmlFor="new-project-root-directory">Root directory</Label>
                  <Input
                    id="new-project-root-directory"
                    value={form.root_directory}
                    onChange={(e) => setForm({ ...form, root_directory: e.target.value })}
                  />
                </div>
              )}
              {!isImageSource && (
                <div className="space-y-3 sm:col-span-2 rounded-md border p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-xs font-medium">How does this repository build?</span>
                    <div className="inline-flex overflow-hidden rounded-md border">
                      <button
                        type="button"
                        onClick={() => { setAutoMode(true); if (!detecting) runDetection(); }}
                        className={`inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium transition-colors ${
                          autoMode ? "bg-primary text-primary-foreground" : "hover:bg-accent"
                        }`}
                      >
                        <Sparkles className="h-3 w-3" /> Auto-detect
                      </button>
                      <button
                        type="button"
                        onClick={() => { setAutoMode(false); setBuildTab("dockerfile"); }}
                        className={`border-l px-2.5 py-1 text-[11px] font-medium transition-colors ${
                          !autoMode && buildTab === "dockerfile" ? "bg-primary text-primary-foreground" : "hover:bg-accent"
                        }`}
                      >
                        Dockerfile
                      </button>
                      <button
                        type="button"
                        onClick={() => { setAutoMode(false); setBuildTab("language"); }}
                        className={`border-l px-2.5 py-1 text-[11px] font-medium transition-colors ${
                          !autoMode && buildTab === "language" ? "bg-primary text-primary-foreground" : "hover:bg-accent"
                        }`}
                      >
                        Language / smartcd.yaml
                      </button>
                    </div>
                  </div>

                  {autoMode && (
                    <div className="rounded-md border bg-muted/30 p-2.5 text-[11px]">
                      {detecting ? (
                        <span className="flex items-center gap-1.5 text-muted-foreground">
                          <Loader2 className="h-3 w-3 animate-spin" /> Scanning the repository for a Dockerfile,
                          a smartcd.yaml manifest, or a known language signature…
                        </span>
                      ) : detectionError ? (
                        <span className="flex items-start gap-1.5 text-destructive">
                          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                          {detectionError}
                        </span>
                      ) : detection ? (
                        <div className="space-y-1.5">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span
                              className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${
                                detection.confidence === "high"
                                  ? "bg-success/20 text-success"
                                  : "bg-warning/20 text-warning"
                              }`}
                            >
                              {detection.confidence === "high" ? "HIGH CONFIDENCE" : "NEEDS YOUR INPUT"}
                            </span>
                            <span className="font-medium text-foreground">
                              {detection.method === "yaml_manifest"
                                ? "smartcd.yaml manifest found"
                                : detection.method === "dockerfile"
                                ? "Dockerfile found"
                                : detection.method === "synthesized"
                                ? "No Dockerfile — we'll generate one from the detected language"
                                : "Could not determine a build method"}
                            </span>
                          </div>
                          {detection.dockerfile_path && (
                            <div className="flex items-center gap-1.5 text-muted-foreground">
                              <Check className="h-3 w-3 text-success" /> Dockerfile at{" "}
                              <span className="text-code">{detection.dockerfile_path}</span>
                            </div>
                          )}
                          {detection.language && (
                            <div className="flex items-center gap-1.5 text-muted-foreground">
                              <Check className="h-3 w-3 text-success" /> Language:{" "}
                              <span className="text-code">{detection.language}</span>
                              {detection.framework && (
                                <>
                                  {" "}(<span className="text-code">{detection.framework}</span>)
                                </>
                              )}
                              {detection.start_command && (
                                <>
                                  {" "}· start: <span className="text-code">{detection.start_command}</span>
                                </>
                              )}
                              {!detection.start_command && detection.language === "static" && <> · served by nginx</>}
                            </div>
                          )}
                          <div className="flex items-center gap-1.5 text-muted-foreground">
                            {detection.test_config_found || detection.test_command ? (
                              <Check className="h-3 w-3 text-success" />
                            ) : (
                              <AlertTriangle className="h-3 w-3 text-warning" />
                            )}
                            {detection.test_command
                              ? `Test command declared: ${detection.test_command}`
                              : detection.test_config_found
                              ? "Test configuration detected"
                              : "No test configuration detected — pre-flight tests will be skipped unless you add one"}
                          </div>
                          {detection.issues.map((issue) => (
                            <div key={issue} className="flex items-start gap-1.5 text-warning">
                              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" /> {issue}
                            </div>
                          ))}
                          <button
                            type="button"
                            onClick={runDetection}
                            className="flex items-center gap-1 pt-1 font-medium text-primary hover:underline"
                          >
                            <RefreshCw className="h-3 w-3" /> Re-analyze
                          </button>
                        </div>
                      ) : (
                        <span className="text-muted-foreground">
                          Select a repository above, then this will scan it automatically.
                        </span>
                      )}

                      {repoReport && (
                        <div className="mt-3 rounded-md border border-border/80 bg-background/60 p-3 space-y-2 text-[11px]">
                          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/50 pb-2">
                            <div className="flex items-center gap-2">
                              <span className="font-semibold text-foreground text-xs">Repo Health & Cost Prediction</span>
                              <span
                                className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${
                                  repoReport.risk.risk_level === "low"
                                    ? "bg-success/20 text-success"
                                    : repoReport.risk.risk_level === "medium"
                                    ? "bg-warning/20 text-warning"
                                    : "bg-destructive/20 text-destructive"
                                }`}
                              >
                                {repoReport.risk.risk_level} Risk ({Math.round((1 - repoReport.risk.risk_score) * 100)}% readiness)
                              </span>
                            </div>
                            <div className="flex items-center gap-3 text-muted-foreground font-mono text-[11px]">
                              <span>
                                Steady: <strong className="text-foreground">${repoReport.cost.steady_state_monthly_usd}/mo</strong>
                              </span>
                              <span>
                                Canary: <strong className="text-foreground">+${repoReport.cost.estimated_rollout_window_usd}</strong>
                              </span>
                            </div>
                          </div>

                          {repoReport.narrative && (
                            <p className="text-muted-foreground italic text-[11px] leading-relaxed">
                              "{repoReport.narrative}"
                            </p>
                          )}

                          {repoReport.risk.risk_flags.length > 0 ? (
                            <div className="space-y-1 pt-1">
                              <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">Readiness Observations</div>
                              {repoReport.risk.risk_flags.map((flag) => (
                                <div key={flag} className="flex items-start gap-1.5 text-warning text-[11px]">
                                  <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                                  <span>{flag}</span>
                                </div>
                              ))}
                            </div>
                          ) : (
                            <div className="flex items-center gap-1.5 text-success text-[11px] pt-1">
                              <Check className="h-3 w-3" /> All baseline hygiene standards met (tests, lockfile, CI configuration present).
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  )}

                  {buildTab === "dockerfile" ? (
                    <div className="space-y-1">
                      <Label htmlFor="new-project-dockerfile-path">Dockerfile path</Label>
                      <Input
                        id="new-project-dockerfile-path"
                        value={form.dockerfile_path}
                        onChange={(e) => setForm({ ...form, dockerfile_path: e.target.value })}
                      />
                    </div>
                  ) : (
                    <div className="grid gap-3 sm:grid-cols-3">
                      <div className="space-y-1">
                        <Label htmlFor="new-project-language">Language</Label>
                        <select
                          id="new-project-language"
                          value={form.language}
                          onChange={(e) => setForm({ ...form, language: e.target.value, framework: "" })}
                          className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                        >
                          <option value="">Select…</option>
                          <option value="python">Python</option>
                          <option value="node">Node.js</option>
                          <option value="go">Go</option>
                          <option value="static">Static HTML</option>
                        </select>
                      </div>
                      <div className="space-y-1">
                        <Label htmlFor="new-project-start-command">
                          Start command{form.language === "static" ? " (not needed)" : ""}
                        </Label>
                        <Input
                          id="new-project-start-command"
                          value={form.start_command}
                          onChange={(e) => setForm({ ...form, start_command: e.target.value })}
                          placeholder={form.language === "static" ? "nginx serves the files directly" : "python main.py"}
                          disabled={form.language === "static"}
                        />
                      </div>
                      <div className="space-y-1">
                        <Label htmlFor="new-project-manifest-path">
                          {form.language === "static" ? "Path to index.html" : "Manifest path"}
                        </Label>
                        <Input
                          id="new-project-manifest-path"
                          value={form.manifest_path}
                          onChange={(e) => setForm({ ...form, manifest_path: e.target.value })}
                          placeholder={form.language === "static" ? "index.html" : "requirements.txt"}
                        />
                      </div>
                      <p className="text-[11px] text-muted-foreground sm:col-span-3">
                        No Dockerfile needed — a Dockerfile is generated for you from this language at build time.
                        Python, Node.js (including Vite/React and Next.js, auto-detected), Go, and static HTML are
                        supported.
                      </p>
                    </div>
                  )}

                  <div className="space-y-1">
                    <Label htmlFor="new-project-test-command">Pre-flight test command (optional)</Label>
                    <Input
                      id="new-project-test-command"
                      value={form.test_command}
                      onChange={(e) => setForm({ ...form, test_command: e.target.value })}
                      placeholder="pytest tests/  ·  npm test"
                    />
                    <p className="text-[11px] text-muted-foreground">
                      Leave blank if your Dockerfile already runs your tests as a build step — that already
                      gates the build for real. If set, this runs separately and never blocks build/deploy on
                      its own (it may not match your project's language/runtime).
                    </p>
                  </div>

                  {/*
                    Build & Test preview — a real clone->build->test dry run
                    (build_preview.py), the same code path a real rollout's
                    build/test stages call. Human-triggered on purpose: this
                    runs a real `docker build`, unlike the free scan above.
                    Never blocks Continue — a project can still be created
                    without running this first — but this is the fastest way
                    to find out "does this actually build" before committing
                    to guardrails in step 3.
                  */}
                  <div className="space-y-2 rounded-md border border-dashed p-2.5">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="text-xs font-medium">Build &amp; Test preview</span>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={runBuildTestPreview}
                        disabled={!hasBuildConfig || !repoUrl || previewStarting || previewResult?.status === "running" || (!!previewRunId && !previewResult)}
                      >
                        {previewStarting || previewResult?.status === "running" || (!!previewRunId && !previewResult) ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <Hammer className="h-3.5 w-3.5" />
                        )}
                        {previewResult ? "Run again" : "Run build & test now"}
                      </Button>
                    </div>

                    {!previewRunId && !previewStarting && (
                      <p className="text-[11px] text-muted-foreground">
                        Actually clones the repo and runs a real <span className="text-code">docker build</span> plus
                        your test command — no deploy, no cluster touched. Optional, but the fastest way to catch a
                        broken build before you finish onboarding.
                      </p>
                    )}

                    {previewRunId && (
                      <div className="space-y-2">
                        <div className="max-h-40 overflow-y-auto rounded bg-muted/50 p-2 font-mono text-[10.5px] leading-relaxed">
                          {previewLogs.length === 0 ? (
                            <span className="text-muted-foreground">Waiting for the first log line…</span>
                          ) : (
                            previewLogs.map((line, i) => <div key={i}>{line}</div>)
                          )}
                        </div>

                        {(!previewResult || previewResult.status === "running") && (
                          <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                            <Loader2 className="h-3 w-3 animate-spin" /> Running…
                          </span>
                        )}
                        {previewResult?.status === "succeeded" && (
                          <div className="flex items-start gap-1.5 rounded bg-success/10 p-2 text-[11px] text-success">
                            <Check className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                            <span>
                              Build succeeded
                              {previewResult.dockerfile_path && (
                                <> — built from <span className="text-code">{previewResult.dockerfile_path}</span></>
                              )}
                              .
                            </span>
                          </div>
                        )}
                        {previewResult?.status === "succeeded" && previewResult.test_warning && (
                          <div className="flex items-start gap-1.5 rounded bg-warning/10 p-2 text-[11px] text-warning">
                            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                            <span>
                              <span className="font-medium">Test command reported a failure (non-blocking):</span>{" "}
                              {previewResult.test_warning} — this never blocks the build; leave the test command
                              blank if your Dockerfile already runs your real tests as a build step.
                            </span>
                          </div>
                        )}
                        {previewResult?.status === "failed" && (
                          <div
                            className={`flex items-start gap-1.5 rounded p-2 text-[11px] ${
                              previewResult.human_side ? "bg-warning/10 text-warning" : "bg-destructive/10 text-destructive"
                            }`}
                          >
                            <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                            <span>
                              <span className="font-medium">
                                {previewResult.human_side
                                  ? `Failed at the ${previewResult.stage} stage — this looks fixable in your repo/config:`
                                  : `Failed at the ${previewResult.stage} stage — this looks like a platform-side problem, not your repo:`}
                              </span>{" "}
                              {previewResult.error}
                            </span>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              )}
              <div className="space-y-1 sm:col-span-2">
                <Label htmlFor="new-project-container-image">Container image registry</Label>
                <Input
                  id="new-project-container-image"
                  value={form.container_image}
                  onChange={(e) => setForm({ ...form, container_image: e.target.value })}
                  placeholder="123456789012.dkr.ecr.us-east-1.amazonaws.com/checkout-service"
                />
                <p className="text-[11px] text-muted-foreground">
                  Use a real ECR repository URI in your own AWS account (
                  <span className="text-code">&lt;account-id&gt;.dkr.ecr.&lt;region&gt;.amazonaws.com/&lt;name&gt;</span>
                  ) — the build stage authenticates automatically and creates the repository itself if it
                  doesn&apos;t exist yet, but the account id and region must be real. There is no safe
                  default to suggest here, since that account id is yours, not the platform&apos;s.
                </p>
              </div>
              <div className="space-y-1">
                <Label htmlFor="new-project-baseline-tag">Baseline tag</Label>
                <Input
                  id="new-project-baseline-tag"
                  value={form.active_production_tag}
                  onChange={(e) => setForm({ ...form, active_production_tag: e.target.value })}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="new-project-canary-tag">Canary tag</Label>
                <Input
                  id="new-project-canary-tag"
                  value={form.canary_tag}
                  onChange={(e) => setForm({ ...form, canary_tag: e.target.value })}
                />
              </div>

              <div className="space-y-1 sm:col-span-2">
                <span className="text-xs font-medium text-muted-foreground">Networking</span>
                <p className="text-[11px] text-muted-foreground">
                  Used for the ECS service, its ALB target group, and the listener rule that routes
                  traffic to it.
                </p>
              </div>
              <div className="space-y-1">
                <Label htmlFor="new-project-port">Container port</Label>
                <Input
                  id="new-project-port"
                  type="number"
                  value={form.port}
                  onChange={(e) => setForm({ ...form, port: Number(e.target.value) })}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="new-project-health-check-path">Health-check path</Label>
                <Input
                  id="new-project-health-check-path"
                  value={form.health_check_path}
                  onChange={(e) => setForm({ ...form, health_check_path: e.target.value })}
                />
                <p className="text-[11px] text-muted-foreground">
                  Hit directly by the ECS target group — a static site, a Vite/React app, or any web app without a
                  dedicated health endpoint should use <span className="text-code">/</span>, not{" "}
                  <span className="text-code">/healthz</span>.
                </p>
              </div>
              <div className="space-y-1 sm:col-span-2">
                <Label htmlFor="new-project-path-prefix">Traffic path prefix (routed through the shared ALB)</Label>
                <Input
                  id="new-project-path-prefix"
                  value={form.path_prefix}
                  onChange={(e) => setForm({ ...form, path_prefix: e.target.value })}
                  placeholder={form.name ? `/api/v1/${form.name}` : "/api/v1/your-service"}
                />
                <p className="text-[11px] text-muted-foreground">
                  One ALB is shared across every project, so each one gets its own path prefix instead
                  of its own load balancer. AWS ALBs forward the full request path as-is — they cannot
                  strip a prefix before it reaches your container — so your app will receive requests at{" "}
                  <span className="text-code">{form.path_prefix || "this prefix"}/...</span>, not at{" "}
                  <span className="text-code">/...</span>. It needs to handle that (or match on the last
                  path segment) rather than assume it owns the whole URL root.
                </p>
              </div>
            </div>
          )}

          {/* ── Step 3 ── */}
          {step === 2 && (
            <div className="space-y-5">
              <div>
                <Label>Traffic shifting strategy</Label>
                <div className="mt-1.5 flex items-center gap-1.5">
                  {TRAFFIC_PRESET.map((w, i) => (
                    <span key={w} className="flex items-center gap-1.5">
                      <span className="rounded border bg-muted px-2 py-0.5 text-code text-[11px]">{w}%</span>
                      {i < TRAFFIC_PRESET.length - 1 && <ArrowRight className="h-3 w-3 text-muted-foreground" />}
                    </span>
                  ))}
                </div>
                <p className="mt-1 text-[11px] text-muted-foreground">
                  Each step holds until its sample-size and duration minimums are met, then a statistical
                  verdict decides whether to advance.
                </p>
              </div>

              <div className="space-y-2 rounded-md border p-3">
                <Label>Telemetry assertions (SRE golden signals)</Label>
                {[
                  ["Error rate — Wald SPRT", "critical tier: a breach forces a hard FAILED"],
                  ["Latency — Mann-Whitney U", "non-parametric distribution comparison"],
                ].map(([title, sub]) => (
                  <div key={title} className="flex items-start gap-2">
                    <Check className="mt-0.5 h-3.5 w-3.5 text-success" />
                    <div>
                      <div className="text-xs font-medium">{title}</div>
                      <div className="text-[11px] text-muted-foreground">{sub}</div>
                    </div>
                  </div>
                ))}
                <p className="border-t pt-2 text-[11px] text-muted-foreground">
                  Saturation (CUSUM/BOCPD) and business-metric tests are available to pipelines that declare
                  those metrics — they aren't added by default because a brand-new service has no such metric
                  wired to a real telemetry source yet.
                </p>
              </div>

              <div className="space-y-2 rounded-md border p-3">
                <Label>Deploy mode</Label>
                <div className="grid gap-2 sm:grid-cols-2">
                  <button
                    type="button"
                    onClick={() => setDeployMode("canary")}
                    className={`rounded-md border p-3 text-left transition-colors ${
                      deployMode === "canary" ? "border-primary bg-primary/5" : "border-border"
                    }`}
                  >
                    <div className="text-sm font-medium">Canary (progressive)</div>
                    <div className="mt-1 text-[11px] text-muted-foreground">
                      Gradual traffic ramp, statistically verified at each step against real telemetry. Needs real
                      traffic to accumulate evidence — best once the service has real visitors.
                    </div>
                  </button>
                  <button
                    type="button"
                    onClick={() => setDeployMode("blue_green")}
                    className={`rounded-md border p-3 text-left transition-colors ${
                      deployMode === "blue_green" ? "border-primary bg-primary/5" : "border-border"
                    }`}
                  >
                    <div className="text-sm font-medium">Blue-green (instant cutover)</div>
                    <div className="mt-1 text-[11px] text-muted-foreground">
                      Deploys the new version, confirms it's actually healthy (real ECS + ALB checks), then cuts
                      100% of traffic over instantly. Works with zero real traffic — the guaranteed-live path for a
                      brand-new service.
                    </div>
                  </button>
                </div>
                {deployMode === "blue_green" && (
                  <p className="text-[11px] text-muted-foreground">
                    Confidence floor and min sample size below don't apply to blue-green rollouts — cutover is
                    gated on real infrastructure health, not a statistical verdict.
                  </p>
                )}
              </div>

              <div className="grid gap-4 sm:grid-cols-3">
                <div className="space-y-1">
                  <Label htmlFor="new-project-confidence-floor">Confidence floor: {confidenceFloor.toFixed(2)}</Label>
                  <input
                    id="new-project-confidence-floor"
                    type="range"
                    min={0.5}
                    max={0.99}
                    step={0.01}
                    value={confidenceFloor}
                    onChange={(e) => setConfidenceFloor(Number(e.target.value))}
                    className="w-full accent-primary"
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="new-project-min-sample-size">Min sample size</Label>
                  <Input
                    id="new-project-min-sample-size"
                    type="number"
                    min={1}
                    value={minSampleSize}
                    onChange={(e) => setMinSampleSize(Number(e.target.value))}
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="new-project-max-cost-delta">Max cost delta: +{maxCostDelta}%</Label>
                  <input
                    id="new-project-max-cost-delta"
                    type="range"
                    min={0}
                    max={50}
                    value={maxCostDelta}
                    onChange={(e) => setMaxCostDelta(Number(e.target.value))}
                    className="w-full accent-warning"
                  />
                </div>
              </div>

              <div className="space-y-2 rounded-md border p-3">
                <div className="flex items-center justify-between">
                  <Label className="flex items-center gap-1.5">
                    <Ban className="h-3.5 w-3.5 text-muted-foreground" /> Blocked deploy windows
                  </Label>
                  <Button type="button" variant="outline" size="sm" onClick={addBlockedWindow}>
                    <Plus className="h-3.5 w-3.5" /> Add window
                  </Button>
                </div>
                {blockedWindows.length === 0 ? (
                  <p className="text-[11px] text-muted-foreground">
                    None configured — automated promotions are allowed at any time.
                  </p>
                ) : (
                  <div className="space-y-2">
                    {blockedWindows.map((window, i) => (
                      <div key={i} className="space-y-1.5 rounded border bg-muted/30 p-2">
                        <div className="flex flex-wrap gap-1">
                          {WEEKDAYS.map((day) => (
                            <button
                              type="button"
                              key={day}
                              onClick={() => toggleWindowDay(i, day)}
                              className={`rounded px-1.5 py-0.5 text-[10px] font-medium transition-colors ${
                                window.days.includes(day)
                                  ? "bg-primary text-primary-foreground"
                                  : "bg-background text-muted-foreground hover:bg-accent"
                              }`}
                            >
                              {day.slice(0, 3)}
                            </button>
                          ))}
                        </div>
                        <div className="flex items-center gap-2">
                          <Input
                            type="time"
                            value={window.start_time}
                            onChange={(e) => updateBlockedWindow(i, { start_time: e.target.value })}
                            className="h-7 w-28 text-xs"
                          />
                          <span className="text-[11px] text-muted-foreground">to</span>
                          <Input
                            type="time"
                            value={window.end_time}
                            onChange={(e) => updateBlockedWindow(i, { end_time: e.target.value })}
                            className="h-7 w-28 text-xs"
                          />
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            className="ml-auto h-7 text-destructive"
                            onClick={() => removeBlockedWindow(i)}
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                <p className="text-[11px] text-muted-foreground">
                  No autonomous rollback/promotion decision is ever affected — this only blocks new
                  deployments from starting during the window.
                </p>
              </div>

              <div className="space-y-2 rounded-md border p-3">
                <label className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    checked={manualApprovalRequired}
                    onChange={(e) => setManualApprovalRequired(e.target.checked)}
                    className="mt-0.5 h-3.5 w-3.5 accent-primary"
                  />
                  <span>
                    <span className="flex items-center gap-1.5 text-xs font-medium">
                      <ShieldCheck className="h-3.5 w-3.5 text-muted-foreground" /> Require manual approval before 100% cutover
                    </span>
                    <span className="block text-[11px] text-muted-foreground">
                      The final step pauses and waits for a human sign-off, even after a HEALTHY verdict.
                    </span>
                  </span>
                </label>
                {manualApprovalRequired && (
                  <div className="ml-6 flex flex-wrap gap-3 pt-1">
                    {APPROVAL_ROLES.map((role) => (
                      <label key={role} className="flex items-center gap-1.5 text-xs">
                        <input
                          type="checkbox"
                          checked={manualApprovalRoles.includes(role)}
                          onChange={() => toggleApprovalRole(role)}
                          className="h-3 w-3 accent-primary"
                        />
                        {role}
                      </label>
                    ))}
                  </div>
                )}
              </div>

              <div className="space-y-2 rounded-md border p-3">
                <span className="block text-xs font-medium">Deployment target</span>
                <div className="rounded-md border border-primary bg-primary/5 p-2 text-left text-xs">
                  <span className="block font-medium">AWS (ECS Fargate)</span>
                  <span className="block text-[11px] text-muted-foreground">
                    Real AWS deployment behind a shared Application Load Balancer
                  </span>
                </div>
                <div className="pt-1">
                  <label className="mb-1 block text-[11px] text-muted-foreground">AWS region</label>
                  <input
                    className="w-full rounded-md border bg-background px-2 py-1.5 text-xs"
                    value={awsRegion}
                    onChange={(e) => setAwsRegion(e.target.value)}
                    placeholder="us-east-1"
                  />
                  <p className="mt-1 text-[11px] text-muted-foreground">
                    This provisions real, billable AWS resources (ECS Fargate tasks, a shared ALB, target
                    groups) using the credentials configured on the platform. The image must already be
                    pushed to an ECR repository this account can pull from.
                  </p>
                </div>
              </div>

              <label className="flex items-start gap-2 rounded-md border p-3">
                <input
                  type="checkbox"
                  checked={provisionCluster}
                  onChange={(e) => setProvisionCluster(e.target.checked)}
                  className="mt-0.5 h-3.5 w-3.5 accent-primary"
                />
                <span>
                  <span className="block text-xs font-medium">Apply AWS resources now</span>
                  <span className="block text-[11px] text-muted-foreground">
                    Creates the real ECS services, target groups and ALB listener rule. If it fails, the
                    service is still created and you can retry later.
                  </span>
                </span>
              </label>
            </div>
          )}

          <div className="flex items-center justify-between border-t pt-4">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setStep((s) => Math.max(0, s - 1))}
              disabled={step === 0 || submitting}
            >
              <ArrowLeft className="h-3.5 w-3.5" /> Back
            </Button>
            {step < 2 ? (
              <Button
                size="sm"
                onClick={() => setStep((s) => s + 1)}
                disabled={step === 0 ? !canContinueStep0 : !canContinueStep1}
              >
                Continue <ArrowRight className="h-3.5 w-3.5" />
              </Button>
            ) : (
              <Button size="sm" onClick={handleDeploy} disabled={submitting || !canContinueStep1}>
                {submitting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Rocket className="h-3.5 w-3.5" />}
                Deploy Service &amp; Start Canary Loop
              </Button>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
