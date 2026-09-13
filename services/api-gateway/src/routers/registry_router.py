"""
services/api-gateway/src/routers/registry_router.py

Phase 8 follow-up — "Existing Image" as a third source option in the
project wizard (alongside Git Provider and Public Git Repository), the way
Render lets a service deploy straight from a pre-built image instead of a
repository. A private registry needs a pull credential; this stores one and
hands `pipeline-worker` what it needs to create a real Kubernetes
`kubernetes.io/dockerconfigjson` Secret and reference it via
`imagePullSecrets` — see `pipeline-worker/src/k8s/manifest_generator.py`.

Storage: Redis, keyed by TENANT (not user — a registry credential configures
how the platform pulls images for the whole team, the same scope as a
project). No TTL: unlike a GitHub OAuth token this isn't a session artifact,
it's infrastructure configuration meant to persist. Same caveat as the
GitHub token: this service has no `cryptography` dependency, so the
credential sits in Redis in plaintext, not encrypted at rest — acceptable
for the same reason the GitHub token is (see docs/roadmap/08-project-workspaces.md),
real hardening is a Phase 7 secrets-store problem.

`pipeline-worker` reads credentials directly from the SAME Redis instance
(it already holds `app.state.redis`) rather than having the raw secret
forwarded through an HTTP payload — the credential material never appears
in a request body after the one call that creates it.
"""
import base64
import json
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.auth.rbac import require_role

router = APIRouter()
logger = structlog.get_logger(__name__)

_CRED_KEY_PREFIX = "registry:cred"


class CreateCredentialRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    # e.g. "docker.io", "ghcr.io", "registry.internal:5000" — used as the
    # dockerconfigjson's "auths" key, exactly what `docker login` prompts for.
    registry: str = Field(..., min_length=1)
    username: str
    secret: str = Field(..., min_length=1, description="Password or access token — never echoed back.")


def _get_tenant_id(request: Request) -> str:
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=401, detail="Missing tenant context")
    return str(tenant_id)


def _cred_key(tenant_id: str, cred_id: str) -> str:
    return f"{_CRED_KEY_PREFIX}:{tenant_id}:{cred_id}"


def _cred_index_key(tenant_id: str) -> str:
    return f"{_CRED_KEY_PREFIX}:{tenant_id}:index"


def _build_dockerconfigjson(registry: str, username: str, secret: str) -> str:
    """
    The exact shape Kubernetes expects in a `kubernetes.io/dockerconfigjson`
    Secret's `.dockerconfigjson` key — base64 of this JSON becomes the
    Secret's data value.
    """
    auth_token = base64.b64encode(f"{username}:{secret}".encode()).decode()
    config = {"auths": {registry: {"username": username, "password": secret, "auth": auth_token}}}
    return base64.b64encode(json.dumps(config).encode()).decode()


@router.get("/credentials")
async def list_credentials(request: Request):
    """Never returns secret material — only what the picker needs to display."""
    tenant_id = _get_tenant_id(request)
    redis_client = request.app.state.redis
    ids = await redis_client.smembers(_cred_index_key(tenant_id))

    credentials = []
    for cred_id in ids:
        raw = await redis_client.get(_cred_key(tenant_id, cred_id))
        if not raw:
            # Index and value can drift if a credential was deleted concurrently.
            await redis_client.srem(_cred_index_key(tenant_id), cred_id)
            continue
        stored = json.loads(raw)
        credentials.append(
            {
                "id": cred_id,
                "name": stored["name"],
                "registry": stored["registry"],
                "username": stored["username"],
            }
        )

    credentials.sort(key=lambda c: c["name"].lower())
    return {"credentials": credentials}


@router.post("/credentials", dependencies=[Depends(require_role("lead-sre"))])
async def create_credential(body: CreateCredentialRequest, request: Request):
    tenant_id = _get_tenant_id(request)
    cred_id = str(uuid.uuid4())
    stored = {
        "name": body.name,
        "registry": body.registry,
        "username": body.username,
        # Precomputed at write time so pipeline-worker only ever reads a
        # ready-to-use Secret payload, never re-derives it from raw fields,
        # for the Kubernetes imagePullSecrets path.
        "dockerconfigjson_b64": _build_dockerconfigjson(body.registry, body.username, body.secret),
        # Kept in plaintext too (same Redis-plaintext caveat as above) so
        # build_task.py can authenticate a real `docker push` after a repo
        # build — the Docker SDK's push() takes a username/password pair,
        # not a dockerconfigjson blob, so re-deriving one from the other
        # would mean decoding base64+JSON on every push for no benefit.
        "secret": body.secret,
    }
    redis_client = request.app.state.redis
    await redis_client.set(_cred_key(tenant_id, cred_id), json.dumps(stored))
    await redis_client.sadd(_cred_index_key(tenant_id), cred_id)

    logger.info("registry_credential_created", tenant_id=tenant_id, cred_id=cred_id, registry=body.registry)
    return {"id": cred_id, "name": body.name, "registry": body.registry, "username": body.username}


@router.delete("/credentials/{credential_id}", dependencies=[Depends(require_role("lead-sre"))])
async def delete_credential(credential_id: str, request: Request):
    tenant_id = _get_tenant_id(request)
    redis_client = request.app.state.redis
    deleted = await redis_client.delete(_cred_key(tenant_id, credential_id))
    await redis_client.srem(_cred_index_key(tenant_id), credential_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Credential not found")
    logger.info("registry_credential_deleted", tenant_id=tenant_id, cred_id=credential_id)
    return {"deleted": credential_id}
