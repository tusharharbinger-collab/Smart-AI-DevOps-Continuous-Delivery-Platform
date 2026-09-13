"""
services/api-gateway/src/config.py

Centralized environment-driven configuration for the API Gateway.
"""
import os


class Settings:
    SERVICE_NAME: str = "api-gateway"

    POSTGRES_DSN: str = os.environ.get(
        "POSTGRES_DSN",
        "postgresql+asyncpg://platform:platform@localhost:5432/platform",
    )
    REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    OPA_URL: str = os.environ.get("OPA_URL", "http://localhost:8181")
    JWT_SECRET_KEY: str = os.environ.get("JWT_SECRET_KEY", "change-me-in-prod")
    VERDICT_SIGNING_KEY: str = os.environ.get(
        "VERDICT_SIGNING_KEY", "dev-secret-key-change-in-prod"
    )
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    CORS_ALLOW_ORIGINS: list[str] = os.environ.get(
        "CORS_ALLOW_ORIGINS", "http://localhost:3000"
    ).split(",")

    # ── GitHub OAuth (Phase 8) ───────────────────────────────────
    # Registered at GitHub → Settings → Developer settings → OAuth Apps.
    # The callback URL registered there MUST match GITHUB_OAUTH_REDIRECT_URI
    # exactly, or GitHub rejects the authorization with redirect_uri_mismatch.
    GITHUB_CLIENT_ID: str = os.environ.get("GITHUB_CLIENT_ID", "")
    GITHUB_CLIENT_SECRET: str = os.environ.get("GITHUB_CLIENT_SECRET", "")
    GITHUB_OAUTH_REDIRECT_URI: str = os.environ.get(
        "GITHUB_OAUTH_REDIRECT_URI",
        "http://localhost:8000/api/v1/integrations/github/callback",
    )
    # Where the OAuth callback sends the browser back to once the token is
    # stored — the wizard reads ?github=connected / ?github=error there.
    FRONTEND_BASE_URL: str = os.environ.get("FRONTEND_BASE_URL", "http://localhost:3000")
    # A personal access token configured on the server, used only as a
    # fallback when the signed-in user has not connected their own account.
    GITHUB_TOKEN: str = os.environ.get("GITHUB_TOKEN", "")


settings = Settings()
