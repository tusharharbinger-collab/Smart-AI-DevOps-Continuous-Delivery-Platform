"""
services/pipeline-worker/src/tasks/build_task.py

Executes build and unit test stages for progressive deployment.
"""
import os
import subprocess

import docker
import structlog

from src.tasks.git_clone import GitCloneError, clone_repo_for_run, cleanup_workspace
from src.tasks.kind_loader import KindLoadError, kind_load_image

logger = structlog.get_logger(__name__)


def _push_to_registry(client: "docker.DockerClient", image_name: str, tag: str, credential: dict) -> None:
    """
    Real `docker push`, authenticated with the project's stored registry
    credential (see registry_router.py) — the counterpart to kind_load_image
    for anyone NOT running against the local dev Kind cluster. docker-py's
    `push()` streams newline-delimited JSON progress events; a failed push
    reports success at the HTTP level (the connection completes normally)
    with the actual failure only visible inside one of those event objects,
    so every line must be inspected rather than trusting a clean return.
    """
    auth_config = {"username": credential["username"], "password": credential["secret"]}
    last_error: str | None = None
    for line in client.images.push(image_name, tag=tag, auth_config=auth_config, stream=True, decode=True):
        if "error" in line:
            last_error = line["error"]
    if last_error:
        raise RuntimeError(f"Registry push failed for {image_name}:{tag}: {last_error}")


def run_build_task(
    pipeline_run_id: str,
    dockerfile_path: str,
    image_tag: str,
    repo_config: dict | None = None,
    image_name: str | None = None,
    registry_credential: dict | None = None,
) -> dict:
    """
    Builds the target service version via the `docker` Python SDK (talks to
    `/var/run/docker.sock` directly).

    Real bug found live (while adding preflight.py's registry check): this
    used to shell out to a `docker` CLI subprocess, but this container's
    base image (Debian trixie) installs the `docker.io` package for the
    daemon/proxy binaries only — there is no `/usr/bin/docker` CLI on this
    release (`dpkg -L docker.io` confirms it), so every real build stage
    would have failed with "executable file not found," never a real build
    error. Never caught before because the seeded demo pipelines skip
    straight to progressive_verify and never exercise this stage.

    `repo_config` (Phase 2 hardening, §2.1's real gap): when a pipeline's
    build stage declares `repoUrl` (see git_clone.py), the actual onboarded
    service's own repo is cloned into an isolated per-run workspace and
    used as the build context instead of `dockerfile_path`'s fixed local
    directory — `dockerfile_path` is then interpreted as relative to that
    clone's root. `repo_config` is None for every pipeline that doesn't set
    `repoUrl` (including the seeded demo pipelines), which keeps building
    from the fixed local path exactly as before.

    `image_name` (Phase 8 follow-up — real bug found live): a project's
    ACTUAL configured registry image, e.g. `registry.internal/orders-api`.
    Before this, every build ignored it and hardcoded
    `localhost:5001/payments:{tag}` regardless — the canary Deployment
    (created at onboarding with the CORRECT name) could then never pull
    what this stage had just built under a completely different name.
    `image_name` is None only for the original hand-wired demo pipeline
    (which declares no `image` in its build config), preserving that
    pipeline's existing behavior exactly.

    Once built, the image reaches somewhere runnable one of two ways,
    chosen by whether a registry credential is configured:
      - `registry_credential` present → real `docker push` (see
        `_push_to_registry`) — works against any real cluster.
      - absent → `kind_load_image` — loads straight into the local Kind
        cluster's containerd, no registry involved. This is what local
        development actually uses today, and is the same mechanism
        `make deploy-sample-app` already performs by hand for the seeded
        demo images.
    Neither runs when `image_name` is None (the legacy demo path), so that
    pipeline's reliance on `make deploy-sample-app`'s one-time manual load
    is completely unaffected.

    Real bug found live (first time a real onboarded GitHub repo's pipeline
    ever reached its own `test` stage, rather than the pre-seeded demo
    pipelines whose test commands point at a path bind-mounted directly
    into this container): this used to delete `cloned_workspace` in a
    `finally` block before returning, and `run_test_task` had no way to run
    inside it anyway (no `cwd` parameter) — so a repo-based project's test
    command could never find the very files this stage just cloned and
    built from; it silently ran against pipeline-worker's OWN `/app`
    instead. The workspace is now returned in the result dict so the
    caller (worker.py) can run the test stage inside it, and is cleaned up
    once, at the end of the whole pipeline run, instead of immediately here.
    """
    logger.info("build_task_started", pipeline_run_id=pipeline_run_id, image_tag=image_tag, image_name=image_name)
    image_ref = f"{image_name}:{image_tag}" if image_name else f"localhost:5001/payments:{image_tag}"

    cloned_workspace: str | None = None
    if repo_config and repo_config.get("url"):
        try:
            cloned_workspace = clone_repo_for_run(
                pipeline_run_id,
                repo_url=repo_config["url"],
                ref=repo_config.get("ref", "main"),
                credentials_env_var=repo_config.get("credentialsEnvVar"),
                credentials_token=repo_config.get("credentialsToken"),
            )
        except GitCloneError as e:
            logger.error("build_task_clone_failed", error=str(e), pipeline_run_id=pipeline_run_id)
            raise RuntimeError(f"Build failed: {e}") from e

    try:
        base_dir = cloned_workspace or "."
        context_dir = os.path.join(base_dir, os.path.dirname(dockerfile_path) or ".")
        dockerfile_name = os.path.basename(dockerfile_path)

        client = docker.from_env()
        try:
            client.images.build(path=context_dir, dockerfile=dockerfile_name, tag=image_ref, rm=True)

            if image_name:
                if registry_credential:
                    try:
                        _push_to_registry(client, image_name, image_tag, registry_credential)
                        logger.info("build_task_pushed", pipeline_run_id=pipeline_run_id, image=image_ref)
                    except Exception as e:
                        logger.error("build_task_push_failed", error=str(e), pipeline_run_id=pipeline_run_id)
                        raise RuntimeError(f"Build succeeded but registry push failed: {e}") from e
                else:
                    try:
                        kind_load_image(image_ref, pipeline_run_id)
                    except KindLoadError as e:
                        logger.error("build_task_kind_load_failed", error=str(e), pipeline_run_id=pipeline_run_id)
                        raise RuntimeError(f"Build succeeded but loading into Kind failed: {e}") from e
        except docker.errors.BuildError as e:
            logger.error("build_task_failed", error=str(e), pipeline_run_id=pipeline_run_id)
            raise RuntimeError(f"Build failed: {e}") from e
        except docker.errors.DockerException as e:
            logger.error("build_task_failed", error=str(e), pipeline_run_id=pipeline_run_id)
            raise RuntimeError(f"Could not reach the local Docker daemon to build {image_ref}: {e}") from e
        finally:
            client.close()
    except Exception:
        if cloned_workspace:
            cleanup_workspace(pipeline_run_id)
        raise

    logger.info("build_task_completed", pipeline_run_id=pipeline_run_id)
    return {"status": "success", "image": image_ref, "workspace": cloned_workspace}


def _find_requirements_file(repo_dir: str) -> str | None:
    """Shallowest `requirements.txt` under the clone wins — prefers repo-root
    or near-root over one that happens to be vendored/nested deeper."""
    import glob

    matches = glob.glob(os.path.join(repo_dir, "**", "requirements.txt"), recursive=True)
    if not matches:
        return None
    return min(matches, key=lambda p: p.count(os.sep))


def _create_test_venv_python(pipeline_run_id: str, repo_dir: str, requirements_file: str) -> str:
    """
    Real bug found live: the test stage used to run directly inside
    pipeline-worker's OWN Python environment — it "worked" for the first
    real repo ever onboarded only because that repo's requirements.txt
    happened to pin the EXACT same fastapi/uvicorn/pytest/httpx versions
    pipeline-worker already carries for itself. Any project with different
    or conflicting dependencies (a different pytest version, Django,
    pandas, anything pipeline-worker doesn't already have) would fail with
    a ModuleNotFoundError or version clash that has nothing to do with the
    project's actual tests. Creates a real per-run virtualenv and installs
    the repo's OWN requirements.txt into it — the same test isolation
    principle the build stage already gets for real via a Docker image.
    """
    import sys

    venv_dir = os.path.join(repo_dir, ".pipeline-venv")
    logger.info("test_venv_creating", pipeline_run_id=pipeline_run_id, requirements_file=requirements_file)
    result = subprocess.run(
        [sys.executable, "-m", "venv", venv_dir], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create test virtualenv: {result.stderr}")

    venv_python = os.path.join(venv_dir, "bin", "python") if os.name != "nt" else os.path.join(venv_dir, "Scripts", "python.exe")
    result = subprocess.run(
        [venv_python, "-m", "pip", "install", "-q", "-r", requirements_file],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to install {requirements_file} into test virtualenv: {result.stderr}")

    logger.info("test_venv_ready", pipeline_run_id=pipeline_run_id)
    return venv_python


def run_test_task(pipeline_run_id: str, test_command: str, cwd: str | None = None) -> dict:
    """
    Executes unit test command for stage validation.

    `cwd` (Phase 8 follow-up — real bug found live): a repo-based project's
    test command references paths inside its OWN cloned repo (e.g.
    `pytest test/tests/ -v`), not pipeline-worker's own `/app` — see
    build_task.py's `run_build_task` docstring. worker.py passes the same
    workspace directory the build stage just cloned/built from; `None` for
    every pipeline with no `repoUrl` (including the seeded demo pipelines),
    which keeps running relative to `/app` exactly as before.

    When `cwd` is set and the clone carries its own `requirements.txt`,
    tests run inside a fresh virtualenv built from THAT file, not
    pipeline-worker's own environment — see `_create_test_venv_python`'s
    docstring for the real gap this closes. Falls back to pipeline-worker's
    own interpreter only when there's no requirements.txt to install (or no
    `cwd` at all), matching prior behavior exactly for the legacy demo
    pipelines.
    """
    import sys

    python_bin = sys.executable
    if cwd:
        requirements_file = _find_requirements_file(cwd)
        if requirements_file:
            python_bin = _create_test_venv_python(pipeline_run_id, cwd, requirements_file)

    cmd_to_run = test_command
    if cmd_to_run.startswith("pytest"):
        cmd_to_run = f'"{python_bin}" -m ' + cmd_to_run
    logger.info("test_task_started", pipeline_run_id=pipeline_run_id, command=cmd_to_run, cwd=cwd)
    result = subprocess.run(cmd_to_run, shell=True, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        logger.error("test_stage_failed", error=result.stderr, pipeline_run_id=pipeline_run_id)
        raise RuntimeError(f"Tests failed: {result.stderr or result.stdout}")

    logger.info("test_stage_passed", pipeline_run_id=pipeline_run_id)
    return {"status": "success", "output": result.stdout}
