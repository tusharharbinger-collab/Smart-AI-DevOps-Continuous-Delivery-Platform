"""
services/api-gateway/src/routers/aws_connections_router.py

Backlog #3 - bring-your-own-AWS-account. A tenant connects THEIR OWN AWS account (instead of every tenant
sharing the platform's) via a cross-account IAM role:

  1. POST   /connections            platform generates the per-connection ExternalId and returns a
                                    CloudFormation template (platform account + ExternalId pre-filled)
  2. (the customer runs that template in their account and copies the role's ARN from its Outputs)
  3. POST   /connections/{id}/verify  the platform actually assumes the role; only then is it VERIFIED and usable
  4. DELETE /connections/{id}       refused while any infra draft still uses it (see migration 0019 for why)

Only a VERIFIED connection can be attached to an infra draft (`load_provisioning_connection`), and every query
here is tenant-scoped (RLS via get_request_db plus an explicit tenant filter).
"""
import os
import secrets
import uuid

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.provisioning.aws_arn import parse_role_arn
from src.auth.rbac import require_role
from src.aws_connection_template import ROLE_NAME, build_role_template_json
from src.db.session import get_request_db

router = APIRouter()
logger = structlog.get_logger(__name__)

PIPELINE_WORKER_URL = os.environ.get("PIPELINE_WORKER_URL", "http://pipeline-worker:8001")
_REGION_RE = r"^[a-z]{2}(?:-[a-z]+)+-\d$"


def _tenant_id(request: Request) -> str:
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=401, detail="Missing tenant context")
    return str(tenant_id)


def _valid_uuid_or_404(value: str) -> None:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="AWS connection not found")


class CreateConnectionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[\w .-]+$")
    default_region: str = Field(default="us-east-1", pattern=_REGION_RE)


class VerifyConnectionRequest(BaseModel):
    role_arn: str = Field(min_length=20, max_length=600)


def _public(row, *, include_setup: bool = False, platform_account_id: str | None = None) -> dict:
    out = {
        "connection_id": str(row["connection_id"]),
        "name": row["name"],
        "status": row["status"],
        "status_reason": row["status_reason"],
        "role_arn": row["role_arn"],
        "aws_account_id": row["aws_account_id"],
        "default_region": row["default_region"],
        "verified_at": row["verified_at"].isoformat() if row["verified_at"] else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }
    if include_setup:
        # The ExternalId is not a secret from the customer (they must put it in their role's trust policy); it is
        # the per-customer identifier that stops one tenant pointing the platform at another tenant's role.
        out["external_id"] = row["external_id"]
        out["role_name"] = ROLE_NAME
        if platform_account_id:
            out["cloudformation_template"] = build_role_template_json(platform_account_id, row["external_id"])
            out["cli_command"] = (
                f"aws cloudformation deploy --stack-name smartcd-platform-access --template-file smartcd-role.json "
                f"--capabilities CAPABILITY_NAMED_IAM --region {row['default_region']}"
            )
    return out


async def _platform_account_id() -> str:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{PIPELINE_WORKER_URL}/aws-connections/platform-identity")
        resp.raise_for_status()
        return resp.json()["account_id"]
    except (httpx.HTTPError, KeyError) as e:
        raise HTTPException(status_code=502, detail=f"Could not determine the platform's AWS account id: {str(e) or type(e).__name__}")


async def _get_row(db: AsyncSession, tenant_id: str, connection_id: str):
    _valid_uuid_or_404(connection_id)
    row = (
        await db.execute(
            text("SELECT * FROM aws_connections WHERE connection_id = :id AND tenant_id = :tenant"),
            {"id": connection_id, "tenant": tenant_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="AWS connection not found")
    return row


@router.post("/connections", dependencies=[Depends(require_role("lead-sre"))])
async def create_connection(body: CreateConnectionRequest, request: Request, db: AsyncSession = Depends(get_request_db)):
    tenant_id = _tenant_id(request)
    platform_account = await _platform_account_id()
    connection_id = str(uuid.uuid4())
    # Platform-generated, never customer-chosen: 256 bits from the OS CSPRNG.
    external_id = secrets.token_urlsafe(32)
    try:
        await db.execute(
            text(
                "INSERT INTO aws_connections (connection_id, tenant_id, name, external_id, default_region) "
                "VALUES (:id, :tenant, :name, :ext, :region)"
            ),
            {"id": connection_id, "tenant": tenant_id, "name": body.name, "ext": external_id, "region": body.default_region},
        )
    except IntegrityError:
        raise HTTPException(status_code=409, detail=f"An AWS connection named '{body.name}' already exists.")
    row = await _get_row(db, tenant_id, connection_id)
    return _public(row, include_setup=True, platform_account_id=platform_account)


@router.get("/connections")
async def list_connections(request: Request, db: AsyncSession = Depends(get_request_db)):
    tenant_id = _tenant_id(request)
    rows = (
        await db.execute(
            text("SELECT * FROM aws_connections WHERE tenant_id = :tenant ORDER BY created_at DESC"), {"tenant": tenant_id}
        )
    ).mappings().all()
    return [_public(r) for r in rows]


@router.get("/connections/{connection_id}")
async def get_connection(connection_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    """Includes the setup material (ExternalId + template) so a not-yet-verified connection can be finished later."""
    tenant_id = _tenant_id(request)
    row = await _get_row(db, tenant_id, connection_id)
    return _public(row, include_setup=True, platform_account_id=await _platform_account_id())


@router.post("/connections/{connection_id}/verify", dependencies=[Depends(require_role("lead-sre"))])
async def verify_connection(
    connection_id: str, body: VerifyConnectionRequest, request: Request, db: AsyncSession = Depends(get_request_db)
):
    """The platform really assumes the role (with this connection's ExternalId). Only success makes it usable."""
    tenant_id = _tenant_id(request)
    row = await _get_row(db, tenant_id, connection_id)
    try:
        parse_role_arn(body.role_arn)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{PIPELINE_WORKER_URL}/aws-connections/verify",
                json={"role_arn": body.role_arn, "external_id": row["external_id"]},
            )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach the provisioning service: {str(e) or type(e).__name__}")

    if resp.status_code == 200:
        ident = resp.json()
        await db.execute(
            text(
                "UPDATE aws_connections SET status = 'VERIFIED', status_reason = NULL, role_arn = :arn, "
                "aws_account_id = :acct, verified_at = now() WHERE connection_id = :id AND tenant_id = :tenant"
            ),
            {"arn": body.role_arn, "acct": ident["account_id"], "id": connection_id, "tenant": tenant_id},
        )
        logger.info("aws_connection_verified", connection_id=connection_id, account_id=ident["account_id"])
    else:
        reason = (resp.json().get("detail") if resp.headers.get("content-type", "").startswith("application/json") else resp.text)[:400]
        await db.execute(
            text(
                "UPDATE aws_connections SET status = 'FAILED', status_reason = :reason, role_arn = :arn "
                "WHERE connection_id = :id AND tenant_id = :tenant"
            ),
            {"reason": reason, "arn": body.role_arn, "id": connection_id, "tenant": tenant_id},
        )
    return _public(await _get_row(db, tenant_id, connection_id))


@router.delete("/connections/{connection_id}", dependencies=[Depends(require_role("lead-sre"))])
async def delete_connection(connection_id: str, request: Request, db: AsyncSession = Depends(get_request_db)):
    tenant_id = _tenant_id(request)
    await _get_row(db, tenant_id, connection_id)
    in_use = (
        await db.execute(
            text("SELECT count(*) AS n FROM infra_build_state WHERE aws_connection_id = :id AND tenant_id = :tenant"),
            {"id": connection_id, "tenant": tenant_id},
        )
    ).mappings().first()["n"]
    if in_use:
        # Deleting would leave those drafts with no way to reach their account (and must never fall back to the
        # platform's account) - the customer removes access by deleting the role's stack in THEIR account.
        raise HTTPException(
            status_code=409,
            detail=f"{in_use} infrastructure draft(s) still use this connection. To revoke access, delete the "
                   f"'smartcd-platform-access' stack in the AWS account itself.",
        )
    await db.execute(
        text("DELETE FROM aws_connections WHERE connection_id = :id AND tenant_id = :tenant"),
        {"id": connection_id, "tenant": tenant_id},
    )
    return {"deleted": connection_id}


async def load_provisioning_connection(db: AsyncSession, tenant_id: str, connection_id: str | None) -> dict | None:
    """
    What the provisioning helpers need ({"role_arn", "external_id", "default_region"}) for a draft's connection, or
    None when the draft uses the platform's own account. A missing, other-tenant or unverified connection is an
    error - never a silent fallback to the platform's account.
    """
    if not connection_id:
        return None
    # asyncpg returns UUID columns as uuid.UUID objects (a draft row's aws_connection_id), not str.
    connection_id = str(connection_id)
    _valid_uuid_or_404(connection_id)
    row = (
        await db.execute(
            text("SELECT * FROM aws_connections WHERE connection_id = :id AND tenant_id = :tenant"),
            {"id": connection_id, "tenant": tenant_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=422, detail="Unknown AWS connection for this tenant.")
    if row["status"] != "VERIFIED" or not row["role_arn"]:
        raise HTTPException(status_code=422, detail=f"AWS connection '{row['name']}' is not verified yet.")
    return {"role_arn": row["role_arn"], "external_id": row["external_id"], "default_region": row["default_region"]}
