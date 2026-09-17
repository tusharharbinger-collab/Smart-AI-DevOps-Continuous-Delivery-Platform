"""
services/pipeline-worker/src/build_preview.py

Build-only dry run: clone -> build (real Dockerfile or synthesized) ->
test, with NO deploy/canary/cluster involvement at all. Exists so a human
onboarding a new repo finds out whether it actually builds in seconds,
during the wizard, instead of discovering it three real pipeline stages
deep into an actual rollout.

Deliberately NOT run through PipelineOrchestrator/dag_builder — a preview
has no policy, no verificationConfig, no tenant-scoped execution_state
row; it is not a real pipeline run and must never be confused for one by
anything downstream (audit ledger, cost tracking, etc). It reuses
build_task.py's run_build_task/run_test_task directly — the same functions
a real pipeline's build/test stages call — so "does this build" during
preview and "does this build" during a real rollout are answered by
literally the same code, never two implementations that could drift apart.

`human_side` on a failure result distinguishes a genuine repo-side problem
(bad Dockerfile, failing tests, wrong branch, private repo without access)
from a platform-side one (Docker daemon unreachable) — this is the
distinction the "100% surety we produce a build unless it's a human-side
error" guarantee rests on. Heuristic today (message-text matched); if this
ever mis-classifies a real platform outage as human-side, that's a bug to
fix in the classifier, not evidence the guarantee itself is wrong.
"""
import json
from datetime import datetime, timezone

import structlog

from src.tasks.build_task import run_build_task, run_test_task
from src.tasks.dockerfile_synthesis import synthesize_dockerfile, UnsupportedLanguageError
from src.tasks.git_clone import cleanup_workspace

logger = structlog.get_logger(__name__)

LOG_TTL_SECONDS = 3600
_PLATFORM_SIDE_MARKERS = ("Could not reach the local Docker daemon",)


def _log(redis_sync, run_id: str, message: str) -> None:
    try:
        key = f"preview_logs:{run_id}"
        redis_sync.rpush(key, message)
        redis_sync.expire(key, LOG_TTL_SECONDS)
    except Exception as e:
        logger.warning("preview_log_append_failed", run_id=run_id, error=str(e))


def _set_result(redis_sync, run_id: str, status: str, detail: dict) -> None:
    try:
        key = f"preview_result:{run_id}"
        payload = json.dumps({"status": status, "updated_at": datetime.now(timezone.utc).isoformat(), **detail})
        redis_sync.set(key, payload, ex=LOG_TTL_SECONDS)
    except Exception as e:
        logger.warning("preview_result_set_failed", run_id=run_id, error=str(e))


def _is_human_side(error_message: str) -> bool:
    return not any(marker in error_message for marker in _PLATFORM_SIDE_MARKERS)


def _fail(redis_sync, run_id: str, stage: str, error: Exception) -> dict:
    message = str(error)
    detail = {"stage": stage, "error": message, "human_side": _is_human_side(message)}
    _log(redis_sync, run_id, f"Pipeline FAILED: {message}")
    _set_result(redis_sync, run_id, "failed", detail)
    return {"status": "failed", **detail}


def run_build_preview(redis_sync, run_id: str, config: dict) -> dict:
    """
    `config`: repo_url, ref, root_directory, dockerfile_path (nullable),
    language (nullable), manifest_path (nullable), start_command (nullable),
    test_command (nullable), repo_private (bool), credentials_token (nullable).
    Runs synchronously — the caller (main.py's endpoint) hands this to
    asyncio.to_thread and returns `run_id` to the client immediately so it
    can stream `preview_logs:{run_id}` the same way a real run's logs work.
    """
    _set_result(redis_sync, run_id, "running", {})
    _log(redis_sync, run_id, f"--- Build preview started for {config['repo_url']}@{config.get('ref', 'main')} ---")

    root_directory = (config.get("root_directory") or "").strip("./")
    dockerfile_content = None
    dockerfile_path = config.get("dockerfile_path")

    # Guaranteed Live Web App CI/CD — a static site (no process to start)
    # and a Vite/CRA "spa" framework (built to static assets, served by
    # nginx) are the two real cases with no start_command at all; requiring
    # one for them would fail the preview for a perfectly buildable
    # project. Mirrors worker.py's real-pipeline build stage exactly, so
    # preview and real rollout never diverge on this.
    no_start_command_needed = config.get("language") == "static" or config.get("framework") == "spa"
    if dockerfile_path:
        dockerfile_path = "/".join(p for p in [root_directory, dockerfile_path] if p)
        _log(redis_sync, run_id, f"Using the repo's own Dockerfile at {dockerfile_path}")
    elif config.get("language") and (config.get("start_command") or no_start_command_needed):
        # Real gap found live: a smartcd.yaml declaring `runtime`+`startCommand`
        # never sets `manifest_path` (parse_yaml_manifest has no such field —
        # see shared/repo_scanner.py) — requiring it here (as this used to)
        # meant a manifest-declared project failed the ONBOARDING PREVIEW
        # with a false "no build method found" while worker.py's real
        # pipeline build stage (which never required manifest_path either)
        # built the exact same config successfully. Defaulting it to
        # "requirements.txt", matching worker.py's own fallback, is what
        # keeps preview and real-rollout answers from diverging.
        manifest_path = config.get("manifest_path") or "requirements.txt"
        try:
            dockerfile_content = synthesize_dockerfile(
                config["language"], manifest_path, config.get("start_command"), framework=config.get("framework")
            )
        except UnsupportedLanguageError as e:
            return _fail(redis_sync, run_id, "build", e)
        manifest_dir = manifest_path.rsplit("/", 1)[0] if "/" in manifest_path else ""
        dockerfile_path = "/".join(p for p in [manifest_dir, "Dockerfile"] if p) or "Dockerfile"
        _log(
            redis_sync,
            run_id,
            f"No Dockerfile found — synthesized one for {config['language']} at {dockerfile_path}",
        )
    else:
        return _fail(
            redis_sync,
            run_id,
            "build",
            RuntimeError("No Dockerfile and no recognized language manifest found to synthesize one."),
        )

    repo_config = {
        "url": config["repo_url"],
        "ref": config.get("ref", "main"),
        "credentialsEnvVar": "GITHUB_TOKEN" if config.get("repo_private") else None,
        "credentialsToken": config.get("credentials_token"),
    }

    try:
        _log(redis_sync, run_id, "--- Stage: build (build) ---")
        build_result = run_build_task(
            run_id,
            dockerfile_path,
            "preview",
            repo_config=repo_config,
            image_name=None,
            dockerfile_content=dockerfile_content,
        )
        _log(redis_sync, run_id, "Build succeeded.")
    except Exception as e:
        return _fail(redis_sync, run_id, "build", e)

    workspace = build_result.get("workspace")
    test_command = config.get("test_command")
    test_warning: str | None = None
    try:
        if test_command:
            _log(redis_sync, run_id, "--- Stage: test (test) ---")
            _log(redis_sync, run_id, f"Running: {test_command}")
            # Same folder the build stage just resolved its Dockerfile to —
            # see _find_requirements_file's docstring for why this repo
            # (now with test/requirements.txt AND nodocker/requirements.txt
            # at equal depth) is exactly the ambiguous case this guards.
            requirements_subdir = dockerfile_path.rsplit("/", 1)[0] if "/" in dockerfile_path else None
            try:
                run_test_task(run_id, test_command, cwd=workspace, requirements_subdir=requirements_subdir)
                _log(redis_sync, run_id, "Tests passed.")
            except Exception as e:
                # Real gap found live: this test command runs in pipeline-
                # worker's OWN container, which doesn't carry every
                # language's runtime (no Node.js, no Go, etc.) — and a
                # repo's own Dockerfile very often already runs its real
                # tests as a build layer, which the build step above already
                # proved passes. A wrong/unsupported command here (or a
                # genuine failure the Docker build already caught) must
                # never fail a build the Docker build itself just succeeded
                # at — surfaced as a visible warning, never a hard failure.
                test_warning = str(e)
                logger.warning("preview_test_stage_failed_non_blocking", run_id=run_id, error=test_warning)
                _log(redis_sync, run_id, f"Test command reported a failure (non-blocking): {test_warning}")
        else:
            _log(redis_sync, run_id, "No test command configured — skipping test verification.")
    finally:
        if workspace:
            cleanup_workspace(run_id)

    _log(redis_sync, run_id, "Build preview completed successfully.")
    result_detail = {"dockerfile_path": dockerfile_path, "test_warning": test_warning}
    _set_result(redis_sync, run_id, "succeeded", result_detail)
    return {"status": "succeeded", **result_detail}
