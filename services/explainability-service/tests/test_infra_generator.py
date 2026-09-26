"""
services/explainability-service/tests/test_infra_generator.py

Never touches a real Groq account — the httpx call is monkeypatched, same
pattern as test_pipeline_generator.py. Covers the success path, every
failure mode (no API key, timeout, malformed response), and that the
archetype actually reaches the system prompt (the whole point of Ground
Rule 0a's "archetype-bounded, never invents a topology" guarantee).
"""
import asyncio
import json

import httpx
import pytest

import src.infra_generator as infra_generator
from src.infra_generator import InfraGenerationError, generate_infra_proposal

SAMPLE_INTENT_SPEC = {
    "environment_tier": "production",
    "needs_database": True,
    "database_type": "postgres",
    "multi_az": True,
    "needs_cache": False,
    "needs_object_storage": False,
    "aws_region": "us-east-1",
    "archetype": "web_service_with_database",
}


class _FakeResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200, text: str = ""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._payload


def _groq_payload(result: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(result)}}]}


def _valid_result(name: str = "orders-service-infra") -> dict:
    return {
        "name": name,
        "topology": {
            "nodes": [
                {"id": "alb", "type": "alb", "label": "Shared ALB"},
                {"id": "ecs", "type": "ecs_service", "label": "orders-service"},
                {"id": "rds", "type": "rds", "label": "orders-db"},
            ],
            "edges": [{"source": "alb", "target": "ecs"}, {"source": "ecs", "target": "rds"}],
        },
        "iac_terraform": "resource \"aws_db_instance\" \"orders_db\" {}\n",
        "cloudformation_template": "{\"Resources\": {\"OrdersDb\": {\"Type\": \"AWS::RDS::DBInstance\"}}}",
        "estimated_monthly_cost_usd": 45.50,
        "cost_breakdown": [
            {"resource": "RDS db.t4g.micro Multi-AZ", "monthly_usd": 30.0},
            {"resource": "ECS Fargate task", "monthly_usd": 15.5},
        ],
        "policy_checks": [
            {"check": "encryption_at_rest", "passed": True, "detail": "RDS storage encryption enabled by default."},
        ],
    }


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse | None = None, exc: Exception | None = None):
        self._response = response
        self._exc = exc
        self.last_json_body = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        self.last_json_body = json
        if self._exc:
            raise self._exc
        return self._response


def test_generates_a_valid_proposal_from_a_successful_groq_call(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(_FakeResponse(_groq_payload(_valid_result())))
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    result = asyncio.run(
        generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database")
    )

    assert result["name"] == "orders-service-infra"
    assert result["estimated_monthly_cost_usd"] == 45.50
    assert len(result["topology"]["nodes"]) == 3
    assert result["policy_checks"][0]["check"] == "encryption_at_rest"


def test_archetype_and_intent_spec_reach_the_prompt(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(_FakeResponse(_groq_payload(_valid_result())))
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database"))

    system_prompt = fake_client.last_json_body["messages"][0]["content"]
    user_prompt = fake_client.last_json_body["messages"][1]["content"]
    assert "web_service_with_database" in system_prompt
    assert "RDS" in system_prompt  # the archetype's resource shape description
    assert "us-east-1" in user_prompt


def test_uses_native_structured_output_not_plain_json_mode(monkeypatch):
    # Real distinction from pipeline_generator.py (§R.6) — this must use
    # json_schema mode, not json_object, since it's a materially more
    # complex nested object.
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(_FakeResponse(_groq_payload(_valid_result())))
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database"))

    response_format = fake_client.last_json_body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True


def test_max_tokens_is_set_explicitly(monkeypatch):
    # Real bug found live: this object requires the model to generate TWO
    # full IaC artifacts (Terraform + a CloudFormation template) plus
    # topology/cost/policy data. Without an explicit ceiling, real-world
    # generation was getting cut off mid-JSON — Groq's strict json_schema
    # mode reports that as a plain 400 ("json_validate_failed"), not a
    # truncation-specific error, so this is easy to silently regress.
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(_FakeResponse(_groq_payload(_valid_result())))
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database"))

    assert fake_client.last_json_body["max_tokens"] >= 8192


def test_schema_sets_additional_properties_false_everywhere_strict_mode_requires_it(monkeypatch):
    # Real bug found live: Groq's strict json_schema mode returns a bare
    # 400 with no field-level detail when "additionalProperties": false is
    # missing anywhere in the schema — not just the root object, every
    # nested $defs entry too (a mock-only test suite never caught this
    # since a fake Groq response doesn't validate the outgoing schema).
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(_FakeResponse(_groq_payload(_valid_result())))
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database"))

    schema = fake_client.last_json_body["response_format"]["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["$defs"], "expected nested $defs for topology/cost/policy sub-objects"
    for name, definition in schema["$defs"].items():
        assert definition.get("additionalProperties") is False, f"{name} is missing additionalProperties: false"


def test_unrecognized_archetype_falls_back_to_a_conservative_description(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(_FakeResponse(_groq_payload(_valid_result())))
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="some_future_archetype"))

    system_prompt = fake_client.last_json_body["messages"][0]["content"]
    assert "conservatively" in system_prompt


def test_raises_clear_error_when_groq_api_key_not_configured(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(InfraGenerationError, match="GROQ_API_KEY"):
        asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="stateless_web_service"))


def test_raises_clear_error_on_groq_timeout(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    fake_client = _FakeAsyncClient(exc=httpx.TimeoutException("timed out"))
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    with pytest.raises(InfraGenerationError, match="timed out"):
        asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="stateless_web_service"))


def test_raises_clear_error_on_malformed_groq_response(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    bad_response = _FakeResponse({"choices": [{"message": {"content": "not valid json"}}]})
    fake_client = _FakeAsyncClient(bad_response)
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    with pytest.raises(InfraGenerationError):
        asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="stateless_web_service"))


def test_double_escaped_cloudformation_template_is_repaired(monkeypatch):
    # Real bug found live: the model sometimes double-escapes its own JSON
    # string content (literal \" instead of ") — this parses fine as an
    # outer JSON string but the CONTENT isn't valid JSON until repaired.
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    result = _valid_result()
    # Simulate what actually came back from Groq: a template string whose
    # own content contains literal backslash-quote sequences.
    result["cloudformation_template"] = '{\\"Resources\\":{\\"Foo\\":{\\"Type\\":\\"AWS::S3::Bucket\\"}}}'
    fake_client = _FakeAsyncClient(_FakeResponse(_groq_payload(result)))
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    output = asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database"))

    parsed_template = json.loads(output["cloudformation_template"])
    assert parsed_template["Resources"]["Foo"]["Type"] == "AWS::S3::Bucket"


def test_already_valid_cloudformation_template_passes_through_unchanged(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    result = _valid_result()
    result["cloudformation_template"] = '{"Resources": {"Foo": {"Type": "AWS::S3::Bucket"}}}'
    fake_client = _FakeAsyncClient(_FakeResponse(_groq_payload(result)))
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    output = asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database"))

    assert output["cloudformation_template"] == '{"Resources": {"Foo": {"Type": "AWS::S3::Bucket"}}}'


def test_retries_once_and_succeeds_after_an_irreparable_template_on_first_attempt(monkeypatch):
    # Proves the retry actually works, not just that failures still fail:
    # first attempt returns an unfixable cloudformation_template, second
    # attempt (with the error fed back) returns a good one.
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    bad_result = _valid_result()
    bad_result["cloudformation_template"] = "not json at all {{{"
    good_result = _valid_result()

    responses = [_FakeResponse(_groq_payload(bad_result)), _FakeResponse(_groq_payload(good_result))]

    class _TwoAttemptClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            return responses.pop(0)

    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", _TwoAttemptClient)

    output = asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database"))

    assert output["cloudformation_template"] == good_result["cloudformation_template"]
    assert responses == []  # both attempts were actually consumed, second one is what won


def test_irreparably_malformed_cloudformation_template_raises_clear_error(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    result = _valid_result()
    result["cloudformation_template"] = "this is not json at all, escaped or otherwise {{{"
    fake_client = _FakeAsyncClient(_FakeResponse(_groq_payload(result)))
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    with pytest.raises(InfraGenerationError, match="not valid JSON even after a repair attempt"):
        asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database"))


def test_raises_clear_error_when_response_is_missing_required_fields(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    incomplete = {"name": "x"}  # missing topology, cost, etc.
    fake_client = _FakeAsyncClient(_FakeResponse(_groq_payload(incomplete)))
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    with pytest.raises(InfraGenerationError):
        asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="stateless_web_service"))


def test_surfaces_groqs_actual_error_body_on_http_error(monkeypatch):
    # Real bug found live (2026-09-25): a Groq 4xx used to fall into the
    # generic `except Exception` branch, which only ever captured httpx's
    # own paraphrase ("Client error '400 Bad Request' for url ...") —
    # Groq's actual reason, in the response body, was silently discarded,
    # making every real failure undiagnosable without a live repro.
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    error_body = '{"error":{"message":"something specific Groq rejected","type":"invalid_request_error"}}'
    fake_client = _FakeAsyncClient(_FakeResponse(status_code=400, text=error_body))
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    with pytest.raises(InfraGenerationError, match="something specific Groq rejected"):
        asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="stateless_web_service"))


def test_retries_after_a_rate_limit_and_succeeds(monkeypatch):
    # Real bug found live: this Groq API key's plan caps at 8000 tokens/
    # minute, and a single successful call already uses ~5000 tokens (a
    # reasoning model burning tokens on hidden reasoning) — two attempts or
    # two quick clicks reliably exhaust that budget within 60s. A 429 used
    # to be treated as an immediate hard failure with no backoff at all.
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(infra_generator.asyncio, "sleep", fake_sleep)
    responses = [
        _FakeResponse(status_code=429, text="Rate limit reached... Please try again in 0.01s. more info"),
        _FakeResponse(_groq_payload(_valid_result())),
    ]

    class _TwoAttemptClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            return responses.pop(0)

    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", _TwoAttemptClient)

    output = asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database"))

    assert output["name"] == "orders-service-infra"
    assert sleep_calls == [0.01]  # backed off for the exact window Groq reported


def test_rate_limit_exhausted_after_retry_raises_clear_error(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(infra_generator.asyncio, "sleep", fake_sleep)
    fake_client = _FakeAsyncClient(
        _FakeResponse(status_code=429, text="Rate limit reached... Please try again in 0.01s. more info")
    )
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", lambda **kw: fake_client)

    with pytest.raises(InfraGenerationError, match="rate limit exceeded after retry"):
        asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="stateless_web_service"))


# ─────────── AI_INFRA_IMPORT_AND_PROMPT_EDIT_PLAN.md: existing-resource import + prompt edits ───────────

EXISTING_DB = {"database": {"id": "orders-db", "details": {"engine": "postgres", "instance_class": "db.t3.micro"}}}


def _template(db_policy: str | None = "Retain", extra: dict | None = None) -> str:
    db = {"Type": "AWS::RDS::DBInstance", "Properties": {"DBInstanceIdentifier": "orders-db"}}
    if db_policy:
        db["DeletionPolicy"] = db_policy
    resources = {"OrdersDb": db}
    resources.update(extra or {})
    return json.dumps({"Resources": resources})


def _result_with_template(template: str) -> dict:
    result = _valid_result()
    result["cloudformation_template"] = template
    return result


class _SeqClient:
    """Returns queued responses in order and records every request body."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.bodies = []

    def __call__(self, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self.bodies.append(json)
        return self.responses.pop(0)


def test_existing_resources_reach_the_prompt_and_retain_template_passes(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    client = _SeqClient([_FakeResponse(_groq_payload(_result_with_template(_template("Retain"))))])
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", client)

    out = asyncio.run(generate_infra_proposal(
        intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database", existing_resources=EXISTING_DB))

    system_prompt = client.bodies[0]["messages"][0]["content"]
    assert "ALREADY EXIST" in system_prompt and "orders-db" in system_prompt
    assert json.loads(out["cloudformation_template"])["Resources"]["OrdersDb"]["DeletionPolicy"] == "Retain"


def test_imported_resource_without_retain_is_rejected_then_corrected_on_retry(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    client = _SeqClient([
        _FakeResponse(_groq_payload(_result_with_template(_template(None)))),
        _FakeResponse(_groq_payload(_result_with_template(_template("Retain")))),
    ])
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", client)

    asyncio.run(generate_infra_proposal(
        intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database", existing_resources=EXISTING_DB))

    # The corrective retry fed the exact reason back to the model.
    assert "DeletionPolicy: Retain" in client.bodies[1]["messages"][1]["content"]


def test_imported_resource_never_retained_raises(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    client = _SeqClient([_FakeResponse(_groq_payload(_result_with_template(_template("Delete")))) for _ in range(2)])
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", client)

    with pytest.raises(InfraGenerationError, match="Retain"):
        asyncio.run(generate_infra_proposal(
            intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database", existing_resources=EXISTING_DB))


def test_edit_that_removes_a_retained_resource_is_rejected(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    current = _result_with_template(_template("Retain"))
    edited = _result_with_template(json.dumps({"Resources": {"Cache": {"Type": "AWS::ElastiCache::CacheCluster"}}}))
    client = _SeqClient([_FakeResponse(_groq_payload(edited)) for _ in range(2)])
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", client)

    with pytest.raises(InfraGenerationError, match="removed OrdersDb"):
        asyncio.run(generate_infra_proposal(
            intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database",
            edit={"current_proposal": current, "instruction": "add a redis cache"}))


def test_edit_that_adds_a_resource_and_keeps_the_retained_one_succeeds(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    current = _result_with_template(_template("Retain"))
    edited = _result_with_template(_template("Retain", {"Cache": {"Type": "AWS::ElastiCache::CacheCluster"}}))
    client = _SeqClient([_FakeResponse(_groq_payload(edited))])
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", client)

    out = asyncio.run(generate_infra_proposal(
        intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database",
        edit={"current_proposal": current, "instruction": "add a redis cache"}))

    assert "Cache" in json.loads(out["cloudformation_template"])["Resources"]
    body = client.bodies[0]["messages"]
    assert "EDITING" in body[0]["content"]
    assert "add a redis cache" in body[1]["content"] and "Current proposal" in body[1]["content"]


def test_edit_prompt_lets_the_human_instruction_override_the_archetype_shape_restriction(monkeypatch):
    # Found live: the base "never add resources the archetype's shape does not call for" rule made the
    # model ignore an "add a cache" edit entirely.
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    current = _result_with_template(_template("Retain"))
    edited = _result_with_template(_template("Retain", {"Cache": {"Type": "AWS::ElastiCache::CacheCluster"}}))
    client = _SeqClient([_FakeResponse(_groq_payload(edited))])
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", client)

    asyncio.run(generate_infra_proposal(
        intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database",
        edit={"current_proposal": current, "instruction": "add a redis cache"}))

    system_prompt = client.bodies[0]["messages"][0]["content"]
    assert "applies ONLY when designing from scratch" in system_prompt
    assert "returning the proposal unchanged is a failure" in system_prompt


def test_an_edit_that_changes_nothing_is_rejected_then_corrected_on_retry(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    current = _result_with_template(_template("Retain"))
    changed = _result_with_template(_template("Retain", {"Cache": {"Type": "AWS::ElastiCache::CacheCluster"}}))
    client = _SeqClient([_FakeResponse(_groq_payload(current)), _FakeResponse(_groq_payload(changed))])
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", client)

    out = asyncio.run(generate_infra_proposal(
        intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database",
        edit={"current_proposal": current, "instruction": "add a redis cache"}))

    assert "Cache" in json.loads(out["cloudformation_template"])["Resources"]
    assert "no change at all" in client.bodies[1]["messages"][1]["content"]


def test_an_edit_that_never_changes_anything_raises(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    current = _result_with_template(_template("Retain"))
    client = _SeqClient([_FakeResponse(_groq_payload(current)) for _ in range(2)])
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", client)

    with pytest.raises(InfraGenerationError, match="no change at all"):
        asyncio.run(generate_infra_proposal(
            intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database",
            edit={"current_proposal": current, "instruction": "add a redis cache"}))


def test_rate_limit_waits_are_independent_of_the_correction_retry_budget(monkeypatch):
    # Found live: an edit right after a create needed MORE than one backoff. Two 429s then a
    # success must still succeed (the old single-backoff logic raised on the second 429).
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(infra_generator.asyncio, "sleep", fake_sleep)
    limited = lambda hint: _FakeResponse(status_code=429, text=f"Rate limit reached... Please try again in {hint}s. more")
    client = _SeqClient([limited("2.5"), limited("0.75"), _FakeResponse(_groq_payload(_valid_result()))])
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", client)

    out = asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database"))

    assert out["name"] == "orders-service-infra"
    assert sleeps == [2.5, 0.75]


def test_rate_limit_sleep_is_capped_and_exhaustion_is_not_retried_by_the_correction_loop(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(infra_generator.asyncio, "sleep", fake_sleep)
    always_limited = _FakeResponse(status_code=429, text="Please try again in 30s.")
    client = _SeqClient([always_limited] * 10)
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", client)

    with pytest.raises(InfraGenerationError, match="rate limit exceeded after retry"):
        asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="stateless_web_service"))

    assert sleeps == [15.0, 15.0, 15.0]  # capped, and exactly _MAX_RATE_LIMIT_WAITS of them
    assert len(client.bodies) == 4  # 1 call + 3 waits, NOT doubled by the 2-attempt correction loop


def test_edit_prompt_is_slimmed_to_fit_a_small_token_budget(monkeypatch):
    # Found live: sending the whole proposal made ONE edit request exceed the plan's entire
    # 8,000 tokens/minute cap - a hard 413, not a retryable 429.
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    current = _result_with_template(_template("Retain"))
    current["iac_terraform"] = "TERRAFORM-AUDIT-TEXT " * 500
    edited = _result_with_template(_template("Retain", {"Cache": {"Type": "AWS::ElastiCache::CacheCluster"}}))
    client = _SeqClient([_FakeResponse(_groq_payload(edited))])
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", client)

    asyncio.run(generate_infra_proposal(
        intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database",
        edit={"current_proposal": current, "instruction": "add a redis cache"}))

    user_prompt = client.bodies[0]["messages"][1]["content"]
    assert "TERRAFORM-AUDIT-TEXT" not in user_prompt
    assert '\\"Resources\\"' not in user_prompt  # template sent as an object, not an escaped string
    assert "OrdersDb" in user_prompt and "Retain" in user_prompt  # the authoritative template stays intact


def test_a_json_validate_failed_400_is_retried_once_then_succeeds(monkeypatch):
    # Found live: Groq answers 400 json_validate_failed when a generation can't finish a valid object.
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    bad = _FakeResponse(status_code=400, text='{"error":{"code":"json_validate_failed","message":"Failed to generate JSON"}}')
    client = _SeqClient([bad, _FakeResponse(_groq_payload(_valid_result()))])
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", client)

    out = asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database"))

    assert out["name"] == "orders-service-infra"
    assert "concise" in client.bodies[1]["messages"][1]["content"]


def test_other_400s_are_not_retried(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    bad = _FakeResponse(status_code=400, text='{"error":{"code":"invalid_request_error","message":"nope"}}')
    client = _SeqClient([bad, bad])
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", client)

    with pytest.raises(InfraGenerationError, match="nope"):
        asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="stateless_web_service"))
    assert len(client.bodies) == 1


def test_a_template_over_escaped_by_two_levels_is_still_repaired():
    from src.infra_generator import _repair_or_reject_cloudformation_json
    good = json.dumps({"Resources": {"Db": {"Type": "AWS::RDS::DBInstance", "Properties": {"Note": "line1\nline2 \"q\""}}}})
    one = json.dumps(good)[1:-1]   # string-escaped once
    two = json.dumps(one)[1:-1]    # ...and again
    for over_escaped in (one, two):
        assert json.loads(_repair_or_reject_cloudformation_json(over_escaped)) == json.loads(good)


def test_genuinely_broken_template_json_is_still_rejected():
    from src.infra_generator import _repair_or_reject_cloudformation_json
    with pytest.raises(InfraGenerationError, match="not valid JSON"):
        _repair_or_reject_cloudformation_json('{"Resources": {"Db": ')


def test_the_prompt_states_the_security_requirements_the_independent_policy_engine_enforces(monkeypatch):
    # Found live: with no stated requirements the model's templates routinely omitted StorageEncrypted
    # (while its own self-reported checks claimed encryption passed), so OPA blocked nearly every draft.
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    client = _SeqClient([_FakeResponse(_groq_payload(_valid_result()))])
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", client)

    asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="web_service_with_database"))

    system_prompt = client.bodies[0]["messages"][0]["content"]
    for requirement in ("StorageEncrypted true", "PubliclyAccessible false", "ManageMasterUserPassword",
                        "0.0.0.0/0", "PublicAccessBlockConfiguration"):
        assert requirement in system_prompt


def test_retry_after_parsing_handles_every_groq_duration_shape():
    from src.infra_generator import _parse_retry_after_seconds as parse
    assert parse("Please try again in 14.94s.") == pytest.approx(14.94)
    assert parse("Please try again in 750ms.") == pytest.approx(0.75)
    assert parse("Please try again in 56m57.552s. Need more") == pytest.approx(56 * 60 + 57.552)  # the daily cap
    assert parse("Please try again in 1h2m3s.") == 3723
    assert parse("no hint here") == 15.0


def test_a_daily_quota_reset_fails_fast_instead_of_burning_useless_waits(monkeypatch):
    # Found live: with the daily token cap exhausted ("try again in 56m57s") the request sat through three
    # 15s sleeps and then failed anyway.
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(infra_generator.asyncio, "sleep", fake_sleep)
    client = _SeqClient([_FakeResponse(status_code=429, text="Rate limit ... tokens per day (TPD) ... Please try again in 56m57.552s.")] * 5)
    monkeypatch.setattr(infra_generator.httpx, "AsyncClient", client)

    with pytest.raises(InfraGenerationError, match="quota resets in about 57m"):
        asyncio.run(generate_infra_proposal(intent_spec=SAMPLE_INTENT_SPEC, archetype="stateless_web_service"))

    assert sleeps == [] and len(client.bodies) == 1


# ───────── "Extra data" repair (found live, intermittent) ─────────

import json as _json
import pytest as _pytest

from src.infra_generator import InfraGenerationError as _Err, _repair_or_reject_cloudformation_json as _repair

_TEMPLATE = {"Resources": {"A": {"Type": "AWS::S3::Bucket"}, "B": {"Type": "AWS::SQS::Queue"}}}


def test_stray_closing_brackets_after_a_complete_template_are_dropped():
    out = _repair(_json.dumps(_TEMPLATE) + "}}]")
    assert _json.loads(out) == _TEMPLATE


def test_trailing_whitespace_and_commas_are_dropped():
    assert _json.loads(_repair(_json.dumps(_TEMPLATE) + " ,\n }")) == _TEMPLATE


def test_trailing_content_that_could_be_dropped_resources_is_still_rejected():
    """A continuation like `,"C":{...}` means resources may have been cut off - never silently accept that."""
    with _pytest.raises(_Err) as e:
        _repair(_json.dumps(_TEMPLATE) + ',"C":{"Type":"AWS::SNS::Topic"}}')
    assert "trailing text" in str(e.value)


def test_a_trailing_word_is_rejected():
    with _pytest.raises(_Err):
        _repair(_json.dumps(_TEMPLATE) + " and more")


def test_a_non_template_object_with_stray_closers_is_rejected():
    with _pytest.raises(_Err):
        _repair('{"foo": 1}}}')


def test_a_valid_template_is_returned_unchanged():
    raw = _json.dumps(_TEMPLATE)
    assert _repair(raw) == raw
