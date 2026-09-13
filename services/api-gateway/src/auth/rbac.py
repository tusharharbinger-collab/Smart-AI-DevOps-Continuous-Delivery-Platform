"""
services/api-gateway/src/auth/rbac.py

Phase 4 (§04-security-hardening.md), deliverable 4.1 — authorization, not
just authentication. Before this, the JWT's `role` claim was signed and
verified but never CHECKED: any authenticated user, `developer` or
`platform-admin`, could call any endpoint, including ones OPA's own policy
(`policies/delivery_guardrails.rego` Rule 8) says require `platform-admin`.

Role hierarchy (ordinal — a higher role can do everything a lower one can):
  developer (1) < lead-sre (2) < platform-admin (3)

Applied to: pausing/resuming/rolling back a pipeline, saving policy changes,
and onboarding a new service (Phase 3) — all treated as "acting on shared
infrastructure on the team's behalf," not routine day-to-day use. Triggering
an ordinary rollout and reading pipeline/verification/audit data stay open
to any authenticated `developer`, matching this platform's actual daily
workflow.
"""
from fastapi import HTTPException, Request

ROLE_RANK = {"developer": 1, "lead-sre": 2, "platform-admin": 3}


def require_role(min_role: str):
    """
    FastAPI dependency factory: `Depends(require_role("lead-sre"))` rejects
    any caller whose JWT `role` claim ranks below `min_role` with a 403 —
    never a 401 (401 means "not authenticated at all," which tenant_context_
    middleware already handles before this dependency ever runs).
    """
    if min_role not in ROLE_RANK:
        raise ValueError(f"Unknown role in require_role(): {min_role}")

    def _dependency(request: Request) -> str:
        role = getattr(request.state, "role", None)
        if role not in ROLE_RANK:
            raise HTTPException(status_code=403, detail=f"Unrecognized role: {role}")
        if ROLE_RANK[role] < ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=403,
                detail=f"This action requires '{min_role}' or higher (you are '{role}').",
            )
        return role

    return _dependency
