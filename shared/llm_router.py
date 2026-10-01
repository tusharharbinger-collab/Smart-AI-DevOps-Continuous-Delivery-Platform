"""
shared/llm_router.py

4-way LLM provider failover router (AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.5).

Every AI call in this platform used to depend on a single provider (Groq) with, at best, a per-feature
deterministic fallback (e.g. predictive_risk_scorer.py's `_heuristic_risk_assessment`). This module gives
every caller ONE shared, ordered failover chain instead: Gemini (primary) -> Groq (secondary) ->
OpenRouter (tertiary) -> Mistral (quaternary). Every one of these four exposes an OpenAI-compatible
`/chat/completions` endpoint, so a single request shape serves all four - the router changes WHO answers,
never WHAT SHAPE a caller trusts (callers still validate the returned content against their own Pydantic
schema exactly as before; this module never weakens that).

A provider with no credential configured anywhere (no env var, no Redis override) is silently skipped, never
attempted with an empty key. If every configured provider fails, `call_llm` raises `AllProvidersFailedError`
- callers keep their own existing deterministic fallback for that case; this router deliberately never
invents one of its own, so a feature that had no fallback before still has none now (visible, not silently
masked).

Credential resolution order per provider: a Redis override (`llm_provider:api_key_override:{name}`, written
by set_provider_credential_override - mirrors github_router.py's token-in-Redis pattern, never Postgres,
never logged) takes precedence over the provider's env var. Redis being briefly unreachable must never block
falling back to the env var, so a Redis error here is swallowed, not raised.
"""
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

_REDIS_OVERRIDE_KEY_PREFIX = "llm_provider:api_key_override:"


@dataclass(frozen=True)
class LLMProvider:
    name: str
    display_name: str
    vendor_label: str
    priority: int
    env_api_key: str
    env_model: str
    default_model: str
    base_url: str


# Priority order matches AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.5 exactly: Gemini (primary) ->
# Groq (secondary) -> OpenRouter (tertiary) -> Mistral (quaternary).
PROVIDERS: list[LLMProvider] = [
    LLMProvider(
        name="gemini",
        display_name="Gemini",
        vendor_label="Google Gemini",
        priority=1,
        env_api_key="GEMINI_API_KEY",
        env_model="GEMINI_MODEL",
        # Real gap found live: gemini-2.0-flash was retired server-side (Google's error names the
        # replacement) - keep this in sync with whatever Gemini's OpenAI-compat endpoint actually serves.
        default_model="gemini-3.8-flash",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    ),
    LLMProvider(
        name="groq",
        display_name="Groq",
        vendor_label="Groq LPU Accelerator",
        priority=2,
        env_api_key="GROQ_API_KEY",
        env_model="GROQ_MODEL",
        # Matches the model every existing Groq call site in this codebase already defaults to -
        # migrating a call site onto this router must not change its behavior when only Groq is configured.
        default_model="openai/gpt-oss-120b",
        base_url="https://api.groq.com/openai/v1/chat/completions",
    ),
    LLMProvider(
        name="openrouter",
        display_name="OpenRouter",
        vendor_label="OpenRouter Gateway",
        priority=3,
        env_api_key="OPENROUTER_API_KEY",
        env_model="OPENROUTER_MODEL",
        # Real gap found live: this exact slug's free tier was retired (OpenRouter's error names the
        # paid replacement) - OpenRouter's free-model roster genuinely churns, so whatever is set here
        # should be re-verified against GET https://openrouter.ai/api/v1/models (filter for ":free")
        # periodically rather than assumed permanent.
        default_model="google/gemma-4-26b-a4b-it:free",
        base_url="https://openrouter.ai/api/v1/chat/completions",
    ),
    LLMProvider(
        name="mistral",
        display_name="Mistral",
        vendor_label="Mistral Codestral",
        priority=4,
        env_api_key="MISTRAL_API_KEY",
        env_model="MISTRAL_MODEL",
        default_model="codestral-latest",
        base_url="https://api.mistral.ai/v1/chat/completions",
    ),
]

_PROVIDERS_BY_NAME = {p.name: p for p in PROVIDERS}


class AllProvidersFailedError(RuntimeError):
    """Raised when every configured provider failed (or none are configured at all). Callers keep their own
    existing deterministic fallback for this - this router never fabricates a response of its own."""

    def __init__(self, attempts: list[dict[str, str]]):
        self.attempts = attempts
        if not attempts:
            message = "No LLM provider is configured (no API key set for gemini/groq/openrouter/mistral)."
        else:
            joined = "; ".join(f"{a['provider']}: {a['error']}" for a in attempts)
            message = f"All {len(attempts)} configured LLM provider(s) failed: {joined}"
        super().__init__(message)


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * 4 + key[-4:]


async def _resolve_api_key(provider: LLMProvider, redis_client=None) -> str:
    if redis_client is not None:
        try:
            override = await redis_client.get(f"{_REDIS_OVERRIDE_KEY_PREFIX}{provider.name}")
            if override:
                return override
        except Exception as exc:
            logger.warning("llm_router_redis_override_lookup_failed", provider=provider.name, error=str(exc))
    return os.environ.get(provider.env_api_key, "").strip()


def _resolve_model(provider: LLMProvider) -> str:
    return os.environ.get(provider.env_model, "").strip() or provider.default_model


async def _configured_providers(redis_client=None) -> list[tuple[LLMProvider, str, str]]:
    """The real, priority-ordered list of (provider, api_key, model) for every provider that actually has a
    credential right now. A provider with no key anywhere is left out entirely."""
    result: list[tuple[LLMProvider, str, str]] = []
    for provider in sorted(PROVIDERS, key=lambda p: p.priority):
        api_key = await _resolve_api_key(provider, redis_client)
        if api_key:
            result.append((provider, api_key, _resolve_model(provider)))
    return result


async def call_llm(
    messages: list[dict[str, str]],
    *,
    response_format: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.0,
    timeout_seconds: float = 30.0,
    redis_client=None,
) -> dict[str, str]:
    """
    Calls the first configured provider in priority order, advancing to the next on ANY failure (timeout,
    HTTP error status, malformed/non-JSON response) - one retry attempt per provider, not per chain, so a
    single flaky provider doesn't consume the whole request's time budget.

    Returns {"content": <the model's raw text/JSON string>, "provider": <name>, "model": <model>}.
    Raises AllProvidersFailedError if every configured provider failed, or none are configured.
    """
    candidates = await _configured_providers(redis_client)
    attempts: list[dict[str, str]] = []

    for provider, api_key, model in candidates:
        body: dict[str, Any] = {"model": model, "temperature": temperature, "messages": messages}
        if response_format is not None:
            body["response_format"] = response_format
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                resp = await client.post(
                    provider.base_url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=body,
                )
                resp.raise_for_status()
                payload = resp.json()
            content = payload["choices"][0]["message"]["content"]
            logger.info("llm_router_provider_succeeded", provider=provider.name, model=model)
            return {"content": content, "provider": provider.name, "model": model}
        except Exception as exc:
            logger.warning("llm_router_provider_failed", provider=provider.name, model=model, error=str(exc))
            attempts.append({"provider": provider.name, "error": str(exc)})
            continue

    raise AllProvidersFailedError(attempts)


async def list_providers(redis_client=None) -> list[dict[str, Any]]:
    """Real status for every provider, for the Settings UI - a masked credential only, never the raw key."""
    result = []
    for provider in sorted(PROVIDERS, key=lambda p: p.priority):
        api_key = await _resolve_api_key(provider, redis_client)
        result.append(
            {
                "name": provider.name,
                "display_name": provider.display_name,
                "vendor_label": provider.vendor_label,
                "priority": provider.priority,
                "active_model": _resolve_model(provider),
                "endpoint": provider.base_url,
                "masked_credential": _mask(api_key),
                "configured": bool(api_key),
            }
        )
    return result


async def test_provider_connection(name: str, redis_client=None) -> dict[str, Any]:
    """A real, minimal live request (4 max_tokens) to confirm the credential+endpoint actually work right
    now - never a fabricated "ok"."""
    provider = _PROVIDERS_BY_NAME.get(name)
    if provider is None:
        raise ValueError(f"Unknown provider: {name}")

    api_key = await _resolve_api_key(provider, redis_client)
    if not api_key:
        return {"ok": False, "latency_ms": None, "error": "No credential configured for this provider."}

    model = _resolve_model(provider)
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                provider.base_url,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "temperature": 0.0,
                    "max_tokens": 4,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
            resp.raise_for_status()
        return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000), "error": None}
    except Exception as exc:
        return {"ok": False, "latency_ms": round((time.monotonic() - started) * 1000), "error": str(exc)}


async def set_provider_credential_override(name: str, api_key: str, redis_client) -> None:
    """Stores an operator-supplied credential override in Redis, taking precedence over the env var for
    this provider - mirrors github_router.py's token-in-Redis pattern (never Postgres, never logged)."""
    if name not in _PROVIDERS_BY_NAME:
        raise ValueError(f"Unknown provider: {name}")
    trimmed = api_key.strip()
    if not trimmed:
        raise ValueError("API key must not be empty.")
    await redis_client.set(f"{_REDIS_OVERRIDE_KEY_PREFIX}{name}", trimmed)
