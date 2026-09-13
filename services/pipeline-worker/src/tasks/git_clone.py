"""
services/pipeline-worker/src/tasks/git_clone.py

Clones a pipeline's own GitHub repo into an isolated per-run workspace
before the build stage runs `docker build` against it. Real gap this
closes: build_task.py previously only ever built from a fixed local path
(`sample-app/v1.1.0`) baked into the pipeline YAML — there was no way for
an onboarded service's OWN repo to actually get fetched, so every "build"
stage was really just rebuilding the platform's own demo app regardless of
what a real user's pipeline pointed at.

A pipeline's build stage opts into this by adding `repoUrl` (and optionally
`ref`, `credentialsEnvVar`) to its config — a stage with no `repoUrl` keeps
building from `dockerfilePath` exactly as before (see build_task.py), so
every existing pipeline (including the seeded demo ones) is unaffected.
"""
import os
import shutil
import subprocess

import structlog

logger = structlog.get_logger(__name__)

WORKSPACE_ROOT = os.environ.get("PIPELINE_BUILD_WORKSPACE_ROOT", "/tmp/pipeline-builds")
_CLONE_TIMEOUT_SECONDS = 120


class GitCloneError(Exception):
    pass


def _authenticated_url(
    repo_url: str, credentials_env_var: str | None, credentials_token: str | None = None
) -> str:
    """
    Embeds a token into the clone URL using the same x-access-token pattern
    GitHub's own API recommends for PAT auth.

    `credentials_token` (Phase 8 follow-up — the per-run GitHub OAuth
    broker) takes priority when supplied: worker.py resolves it from
    `clone_token:{run_id}`, a short-lived Redis key the trigger endpoint
    copies from the CALLING USER's own connected GitHub account — so a
    private clone uses their access, not a token every tenant shares.

    Falls back to `credentials_env_var`, a token already present in
    pipeline-worker's OWN environment (never a literal secret in pipeline
    YAML or code) — the pipeline config only ever carries that var's
    *name*, never a token value. This remains the only path for anyone who
    hasn't connected a GitHub account, or for a non-GitHub git host.
    """
    if not credentials_token and not credentials_env_var:
        return repo_url
    if not repo_url.startswith("https://"):
        raise GitCloneError("A clone credential is only supported for https:// repo URLs")

    token = credentials_token
    if not token:
        token = os.environ.get(credentials_env_var)
        if not token:
            raise GitCloneError(
                f"repo config names credentialsEnvVar='{credentials_env_var}' but that environment "
                f"variable is empty/unset on this pipeline-worker instance."
            )
    return repo_url.replace("https://", f"https://x-access-token:{token}@", 1)


def clone_repo_for_run(
    pipeline_run_id: str,
    repo_url: str,
    ref: str = "main",
    credentials_env_var: str | None = None,
    credentials_token: str | None = None,
) -> str:
    """
    Clones `repo_url` at `ref` into an isolated workspace directory named
    after this run, returning that directory's path. The caller must call
    cleanup_workspace() once done with it (build success OR failure) — see
    build_task.py's run_build_task, which wraps the whole clone-build
    lifecycle in a try/finally so a workspace never leaks disk space across
    runs.
    """
    workspace_dir = os.path.join(WORKSPACE_ROOT, pipeline_run_id)
    if os.path.exists(workspace_dir):
        shutil.rmtree(workspace_dir)
    os.makedirs(os.path.dirname(workspace_dir) or WORKSPACE_ROOT, exist_ok=True)

    clone_url = _authenticated_url(repo_url, credentials_env_var, credentials_token)
    cmd = ["git", "clone", "--depth", "1", "--branch", ref, clone_url, workspace_dir]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_CLONE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as e:
        shutil.rmtree(workspace_dir, ignore_errors=True)
        raise GitCloneError(
            f"git clone of {repo_url} (ref={ref}) timed out after {_CLONE_TIMEOUT_SECONDS}s"
        ) from e

    if result.returncode != 0:
        shutil.rmtree(workspace_dir, ignore_errors=True)
        # Never log/raise the token-bearing clone_url — only the caller-
        # supplied, credential-free repo_url.
        raise GitCloneError(f"git clone of {repo_url} (ref={ref}) failed: {result.stderr.strip()}")

    logger.info("repo_cloned_for_build", pipeline_run_id=pipeline_run_id, repo_url=repo_url, ref=ref)
    return workspace_dir


def cleanup_workspace(pipeline_run_id: str) -> None:
    workspace_dir = os.path.join(WORKSPACE_ROOT, pipeline_run_id)
    shutil.rmtree(workspace_dir, ignore_errors=True)
