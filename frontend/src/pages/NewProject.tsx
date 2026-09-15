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
  ArrowLeft, ArrowRight, Check, Container, FolderGit2, KeyRound, Link2, Loader2, Lock, LogOut,
  Plus, Rocket, Search, Unlock,
} from "lucide-react";
import {
  disconnectGitHub, getAuthorizeUrl, getGitHubStatus, listBranches, listRepos, parseRepoUrl,
  type GitHubRepo, type GitHubStatus,
} from "@/api/github";
import {
  createRegistryCredential, listRegistryCredentials, parseImageRef, type RegistryCredential,
} from "@/api/registry";
import { createProject, type CreateProjectInput } from "@/api/projects";
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

type SourceTab = "provider" | "public" | "image";

// Real bug found live: a GitHub repo named "test-" (trailing hyphen)
// produced the auto-suggested container_image "registry.internal/test-" —
// already lowercase, so it passed the casing check, but Docker repository
// name components must also START and END with an alphanumeric character.
// `docker build -t` rejected it as "invalid reference format" deep inside
// the build stage instead of at onboarding time. Lowercasing alone (the
// original fix for the "RaktDoot" bug) isn't enough — strip any leading/
// trailing non-alphanumeric characters too, matching the same rule
// projects_router.py now validates server-side.
function dockerSafeName(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[._-]+|[._-]+$/g, "") || "app";
}

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
    test_command: "pytest tests/",
    container_image: "",
    active_production_tag: "v1.0.0",
    canary_tag: "v1.1.0",
    port: 8080,
    health_check_path: "/healthz",
    path_prefix: "",
  });
  const [branches, setBranches] = useState<string[]>([]);

  // Step 3 — guardrails
  const [confidenceFloor, setConfidenceFloor] = useState(0.8);
  const [minSampleSize, setMinSampleSize] = useState(100);
  const [maxCostDelta, setMaxCostDelta] = useState(15);
  const [provisionCluster, setProvisionCluster] = useState(true);
  const [submitting, setSubmitting] = useState(false);

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
      // Docker repository names must be lowercase (real bug found live:
      // a GitHub repo named "RaktDoot" produced "registry.internal/RaktDoot",
      // which `docker build -t` rejects outright as an invalid reference).
      container_image: f.container_image || `registry.internal/${dockerSafeName(repo.name)}`,
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
  }

  function useManualUrl() {
    const parsed = parseRepoUrl(manualUrl);
    setSelectedRepo(null);
    setForm((f) => ({
      ...f,
      name: f.name || parsed?.repo || "",
      container_image: f.container_image || (parsed ? `registry.internal/${dockerSafeName(parsed.repo)}` : ""),
    }));
    setBranches([]);
  }

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
        dockerfile_path: form.dockerfile_path,
        test_command: form.test_command,
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
        traffic_steps: TRAFFIC_PRESET,
        provision_cluster: provisionCluster,
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
  const canContinueStep1 = Boolean(form.name.trim() && form.container_image.trim());

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
                <div className="space-y-1">
                  <Label htmlFor="new-project-dockerfile-path">Dockerfile path</Label>
                  <Input
                    id="new-project-dockerfile-path"
                    value={form.dockerfile_path}
                    onChange={(e) => setForm({ ...form, dockerfile_path: e.target.value })}
                  />
                </div>
              )}
              {!isImageSource && (
                <div className="space-y-1">
                  <Label htmlFor="new-project-test-command">Pre-flight test command</Label>
                  <Input
                    id="new-project-test-command"
                    value={form.test_command}
                    onChange={(e) => setForm({ ...form, test_command: e.target.value })}
                    placeholder="pytest tests/  ·  npm test"
                  />
                </div>
              )}
              <div className="space-y-1 sm:col-span-2">
                <Label htmlFor="new-project-container-image">Container image registry</Label>
                <Input
                  id="new-project-container-image"
                  value={form.container_image}
                  onChange={(e) => setForm({ ...form, container_image: e.target.value })}
                  placeholder="registry.internal/checkout-service"
                />
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
                  Used for the generated Deployment, Service and HTTPRoute.
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
              </div>
              <div className="space-y-1 sm:col-span-2">
                <Label htmlFor="new-project-path-prefix">Traffic path prefix (routed through the Gateway)</Label>
                <Input
                  id="new-project-path-prefix"
                  value={form.path_prefix}
                  onChange={(e) => setForm({ ...form, path_prefix: e.target.value })}
                  placeholder={form.name ? `/api/v1/${form.name}` : "/api/v1/your-service"}
                />
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

              <label className="flex items-start gap-2 rounded-md border p-3">
                <input
                  type="checkbox"
                  checked={provisionCluster}
                  onChange={(e) => setProvisionCluster(e.target.checked)}
                  className="mt-0.5 h-3.5 w-3.5 accent-primary"
                />
                <span>
                  <span className="block text-xs font-medium">Apply Kubernetes objects now</span>
                  <span className="block text-[11px] text-muted-foreground">
                    Generates the baseline/canary Deployments, Services and HTTPRoute. Needs a reachable
                    cluster — if it fails, the service is still created and you can retry later.
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
