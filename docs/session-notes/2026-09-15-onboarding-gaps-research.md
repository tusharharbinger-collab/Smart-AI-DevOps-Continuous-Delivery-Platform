# Research: 4 real onboarding gaps vs. how mature platforms solve them

Written 2026-09-15, after onboarding a real GitHub repo (`test-`) surfaced
several structural gaps in the project wizard (see
`2026-09-14-eks-and-onboarding-fixes.md`). This asks: how do Harness,
Railway/Render, and Argo Rollouts/Flagger actually solve these, and is it
buildable here? Short answer: yes to all four, none require new
infrastructure — just real engineering time.

## Gap 1 & 2 — Building without a Dockerfile / how Harness does zero-input onboarding

**Current state:** this platform can ONLY build via a Dockerfile
(`build_task.py`'s `run_build_task` calls the Docker SDK directly). No
buildpack support, no language auto-detection. If a repo has no Dockerfile
at the path the wizard was told about, the build simply fails.

**How real platforms do it:**
- **Cloud Native Buildpacks** (CNCF-graduated) — a builder ships an ordered
  list of buildpack groups; each buildpack's `detect` script checks for a
  signal file (e.g. `requirements.txt` → Python buildpack). First group
  that all-detects wins and builds the image. `pack build <name>` is the
  reference CLI.
- **Nixpacks / Railpack** (Railway's own tooling, now Railpack) — same
  idea, simpler: inspects the repo, detects ~20 languages/frameworks by
  file signature, and produces an optimized image with zero config.
- **Harness specifically** (confirmed from their own docs): their
  pipeline-generation AI "scans project files to auto-detect language and
  runtime from source files (`package.json`→Node, `go.mod`→Go), and build
  tools from build configuration (`Dockerfile`, `webpack.config.js`,
  `pom.xml`)" — i.e. Harness runs **heuristic file-signature detection**,
  not a full buildpacks runtime, then generates a tailored pipeline from
  what it finds.
- **Important caveat found in research**: buildpack auto-detection
  genuinely fails for roughly 1 in 5 real repos — languages outside the
  standard set (PHP, Ruby, some JVM setups), or apps needing extra system
  packages (ffmpeg, imagemagick, etc.) not in the base image. The
  recommended pattern isn't "always auto-detect" — it's a **three-tier
  fallback**:
  1. Tier 0 — zero-config detection for the common case (~80-85% of repos)
  2. Tier 1 — a narrow escape hatch to declare extra system packages
     without losing auto-detection's benefits
  3. Tier 2 — full Dockerfile fallback for anything unusual or unsupported

**Recommendation for this codebase:** skip integrating the full Buildpacks
`pack` CLI (heavy — needs builder base images, its own runtime) and build
Harness's actual approach instead: a lightweight detector inside
`pipeline-worker` that checks for `requirements.txt`/`Pipfile` →
synthesizes a minimal Python Dockerfile on the fly, `package.json` → Node,
`go.mod` → Go, etc. Same practical result (build without a human-written
Dockerfile), far smaller implementation than a real buildpacks runtime,
and keeps the existing Dockerfile path as the Tier 2 fallback exactly as
it works today. Multi-day effort — worth planning as its own piece of
work, not a quick patch.

## Gap 3 — Enforcing/validating required files before onboarding

**Current state:** the wizard accepts any `dockerfile_path`/`test_command`
with zero validation that the files actually exist. The user only finds
out when the pipeline actually runs and fails — exactly what happened with
`test-` (three separate rounds of "trigger, discover it's broken, fix").

**How real platforms do it:** GitHub's own tooling ecosystem has a
standard pattern — "validate-file-exists"-style actions check file
existence via the GitHub API directly (no clone needed). The general CI
best practice: run a fast pre-flight check before the pipeline's first
real iteration (does the declared file exist, does the test command
resolve, etc.) and fail fast with a specific diagnostic — never discover a
structural problem three stages deep.

**Recommendation:** small, high-value, and the smallest of the four to
build. We already have a working GitHub OAuth connection with real API
access (`github_router.py`). Add one call —
`GET /repos/{owner}/{repo}/contents/{dockerfile_path}` — at project-creation
time, before the wizard lets the user finish. 404 → reject with a clear
message naming exactly which file is missing. Roughly half a day of work.

## Gap 4 — What acts as "baseline" before any version has ever been deployed

**Current state:** onboarding always creates a `baseline` Deployment
pinned to `active_production_tag` (default `v1.0.0`) and a `canary`
Deployment pinned to `canary_tag` (default `v1.1.0`) — it assumes TWO
already-built versions exist before onboarding even happens. A project
with only one version ever built (exactly `test-`'s situation) gets a
baseline Deployment referencing a tag that was never pushed, and the pod
fails immediately.

**How real platforms do it:** this is explicit, documented behavior in
Argo Rollouts (the reference progressive-delivery controller): *"an
initial Rollout immediately scales to 100%, skipping canary steps and
analysis because no upgrade has occurred... progressive behavior begins
when `.spec.template` changes after the initial Rollout is healthy."* In
plain terms: the first deployment is never a canary — there's nothing to
compare it against yet, so it ships straight to 100%. Canary-vs-baseline
comparison only becomes meaningful starting from the SECOND deployment
onward, once a real "previous known-good" version exists to protect.

**Recommendation — being built now (2026-09-15):** track whether a
project has ever completed a real first deployment (new
`projects.first_deployment_completed_at` column). If NULL, skip
`canary_loop`'s statistical comparison entirely for this run — deploy the
built image to BOTH baseline and canary immediately, mark the run
COMPLETED, and set the timestamp. Every deployment after that runs the
normal stepped canary flow exactly as today. Small, contained, most
directly fixes the exact failure mode `test-` hit.

## Sources

- [Getting Started with Cloud Native Buildpacks - CODE Magazine](https://www.codemag.com/Article/2209091/Getting-Started-with-Cloud-Native-Buildpacks)
- [Cloud Native Buildpacks reaches CNCF graduation](https://dev.to/leobaniak/cloud-native-buildpacks-reaches-cncf-graduation-and-the-no-dockerfile-path-gets-its-stamp-55bh)
- [The Buildpack Escape Hatch: Why "No Dockerfile" Breaks for About 1 in 5 Repos](https://bex.co/blog/2026/07/31/buildpacks-vs-dockerfile-auto-detect-escape-hatch)
- [Nixpacks vs Buildpacks vs Dockerfile 2026 - DevOpsBoys](https://devopsboys.com/blog/nixpacks-vs-buildpacks-vs-dockerfile-review-2026)
- [Nixpacks — Grokipedia](https://grokipedia.com/page/Nixpacks)
- [Railpack | Railway Docs](https://docs.railway.com/reference/nixpacks)
- [Why Argo Rollouts Skips Canary or Blue-Green Steps on the First Deployment](https://oneuptime.com/blog/post/2026-08-02-argo-rollouts-first-deployment-skips-steps/view)
- [Canary Deployment Strategy - Argo Rollouts docs](https://argo-rollouts.readthedocs.io/en/stable/features/canary/)
- [Configure codebase | Harness Developer Hub](https://developer.harness.io/docs/continuous-integration/use-ci/codebase-configuration/create-and-configure-a-codebase/)
- [Pipeline AI for CI Pipelines | Harness](https://www.harness.io/products/continuous-integration/pipeline-intelligence)
- [validate-file-exists GitHub Action](https://github.com/chrisreddington/validate-file-exists)
