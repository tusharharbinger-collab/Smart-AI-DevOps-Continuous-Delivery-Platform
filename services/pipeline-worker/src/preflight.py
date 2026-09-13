"""
services/pipeline-worker/src/preflight.py

Phase 1 hardening: verifies the container image tag a canary deployment is
about to reference actually exists BEFORE applying the Deployment, failing
the stage immediately with a clear error instead of letting Kubernetes
silently sit in ImagePullBackOff.

This dev/demo stack has no real OCI registry — the Makefile's own comment
says so plainly ("Loading images into Kind (no registry needed)..."):
images are built locally, tagged `localhost:5001/payments:<tag>` as a
naming convention only, then injected directly into Kind's containerd
image store via `kind load docker-image` — nothing ever listens on
localhost:5001 or serves a real Docker Registry HTTP API here. So the
check that's actually meaningful in THIS stack is: does the local Docker
daemon (reached via the read-only `/var/run/docker.sock` mount) have this
exact image built? If not, `kind load docker-image` was never run for it
and the canary Deployment is guaranteed to fail.

Uses the `docker` Python SDK (talks to the socket directly over HTTP), not
a `docker` CLI subprocess — a real bug found live: this container's base
image (Debian trixie) installs `docker.io` for the daemon/proxy binaries
only; the actual `docker` CLI client isn't in that package on this release
(`dpkg -L docker.io` has no `/usr/bin/docker`), so a subprocess call to
`docker` would always fail with "executable file not found," never
"image not found" — the wrong failure entirely, and one that would have
silently made every real deploy stage fail (build_task.py's own `docker
build` subprocess call had this exact same bug, fixed alongside this).

`check_remote_registry_tag_exists` is written so a REAL registry (ECR,
GHCR, a real Harbor/Docker Registry instance) can be checked the same way
if this stack is ever pointed at one instead of the local-Kind-load path —
not used by the current deploy_task.py, but exercised directly by tests.
"""
import docker
import httpx
import structlog

logger = structlog.get_logger(__name__)


class ImagePreflightError(Exception):
    """Raised when the image/tag a deploy stage is about to use isn't available anywhere reachable."""


def check_local_docker_image_exists(image_ref: str) -> bool:
    """
    True if the local Docker daemon already has this exact image:tag built —
    the only way an image ever reaches this stack's Kind cluster (see
    module docstring). Raises ImagePreflightError if the daemon itself
    can't be reached at all (a different failure mode than "not found").
    """
    client = docker.from_env()
    try:
        client.images.get(image_ref)
        return True
    except docker.errors.ImageNotFound:
        return False
    except docker.errors.DockerException as e:
        raise ImagePreflightError(f"Could not reach the local Docker daemon to check {image_ref!r}: {e}") from e
    finally:
        client.close()


def check_remote_registry_tag_exists(
    registry_url: str, repository: str, tag: str, timeout_seconds: float = 5.0
) -> bool:
    """
    Checks a real OCI/Docker Registry HTTP API V2 endpoint
    (GET /v2/<repository>/manifests/<tag>) for tag existence. Raises
    ImagePreflightError on an unreachable/unauthorized registry rather than
    returning False, since that's a different failure mode than "the tag
    genuinely doesn't exist" and callers need to tell the two apart.
    """
    url = f"{registry_url}/v2/{repository}/manifests/{tag}"
    try:
        resp = httpx.get(
            url,
            headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as e:
        raise ImagePreflightError(
            f"Could not reach registry {registry_url!r} to check {repository}:{tag}: {e}"
        ) from e

    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    if resp.status_code == 401:
        raise ImagePreflightError(
            f"Registry {registry_url!r} rejected the request as unauthorized checking {repository}:{tag} — "
            f"cannot confirm image existence without valid registry credentials."
        )
    raise ImagePreflightError(
        f"Unexpected response ({resp.status_code}) from registry {registry_url!r} checking {repository}:{tag}"
    )


def preflight_check_image(image_ref: str) -> None:
    """
    Raises ImagePreflightError with a clear message if `image_ref`
    (e.g. "localhost:5001/payments:v1.1.0") isn't available anywhere this
    stack can actually pull it from. Call this before applying/patching a
    canary Deployment (deploy_task.py), not after.
    """
    if not check_local_docker_image_exists(image_ref):
        raise ImagePreflightError(
            f"Image '{image_ref}' was not found in the local Docker daemon. It must be built "
            f"(`docker build -t {image_ref} ...`) and loaded into the Kind cluster "
            f"(`kind load docker-image {image_ref} --name <cluster>`) before this stage can deploy it — "
            f"see the Makefile's `demo-images` target."
        )
    logger.info("image_preflight_passed", image_ref=image_ref)
