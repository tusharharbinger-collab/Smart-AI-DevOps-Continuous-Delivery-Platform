"""
services/explainability-service/tests/test_component_catalog.py

Covers shared/component_catalog.py - AI_INFRA_CONVERSATIONAL_PROVISIONING_PLAN.md §3.2 (Phase C). The
rule table handles every catalog entry deterministically; only a genuinely unknown resource_type reaches
the LLM router (mocked here, never a real provider call).
"""
import asyncio

from shared.component_catalog import (
    COMPONENT_CATALOG,
    build_add_component_instruction,
    check_component_compatibility,
    list_component_catalog,
)


def test_catalog_covers_every_planned_category():
    categories = {e.category for e in COMPONENT_CATALOG}
    assert categories == {"Compute", "Storage", "Database", "Cache", "Messaging", "Networking", "Security/Secrets", "Observability"}


def test_list_component_catalog_returns_plain_dicts_with_params():
    catalog = list_component_catalog()
    ec2 = next(e for e in catalog if e["resource_type"] == "ec2_instance")
    assert ec2["category"] == "Compute"
    assert any(p["name"] == "instance_type" and p["required"] for p in ec2["params"])


def test_ec2_instance_is_always_compatible_with_a_bastion_caveat():
    result = asyncio.run(
        check_component_compatibility("ec2_instance", {"instance_type": "t3.micro"}, "stateless_web_service", "aws_ecs", {})
    )
    assert result["compatible"] is True
    assert result["source"] == "rule_table"
    assert any("baseline/canary" in c for c in result["caveats"])


def test_fargate_service_incompatible_when_deploy_target_is_kubernetes():
    result = asyncio.run(
        check_component_compatibility("fargate_service", {"service_name": "worker"}, "stateless_web_service", "kubernetes", {})
    )
    assert result["compatible"] is False
    assert "Kubernetes Deployment" in result["caveats"][0]


def test_fargate_service_compatible_on_aws_ecs():
    result = asyncio.run(
        check_component_compatibility("fargate_service", {"service_name": "worker"}, "stateless_web_service", "aws_ecs", {})
    )
    assert result["compatible"] is True


def test_cloudfront_flags_caveat_when_not_public_facing():
    result = asyncio.run(
        check_component_compatibility("cloudfront_distribution", {}, "stateless_web_service", "aws_ecs", {"public_facing": False})
    )
    assert result["compatible"] is True
    assert any("NOT public-facing" in c for c in result["caveats"])


def test_route53_record_requires_existing_hosted_zone_caveat():
    result = asyncio.run(
        check_component_compatibility("route53_record", {"hosted_zone_domain": "example.com"}, "stateless_web_service", "aws_ecs", {})
    )
    assert result["compatible"] is True
    assert "example.com" in result["caveats"][0]


def test_default_catalog_entry_is_compatible_with_no_caveats():
    result = asyncio.run(
        check_component_compatibility("sqs_queue", {"fifo": True}, "stateless_web_service", "aws_ecs", {})
    )
    assert result == {
        "compatible": True,
        "explanation": "SQS queue is a standard, self-contained resource with no conflict against this project's setup.",
        "caveats": [],
        "source": "rule_table",
    }


def test_unknown_resource_type_falls_back_to_llm_router(monkeypatch):
    import shared.component_catalog as component_catalog

    async def fake_call_llm(**kwargs):
        assert kwargs["response_format"] == {"type": "json_object"}
        return {"content": '{"compatible": true, "explanation": "Custom type looks fine.", "caveats": []}', "provider": "groq", "model": "x"}

    monkeypatch.setattr(component_catalog, "call_llm", fake_call_llm)

    result = asyncio.run(
        check_component_compatibility("some_custom_resource", {}, "stateless_web_service", "aws_ecs", {})
    )
    assert result == {"compatible": True, "explanation": "Custom type looks fine.", "caveats": [], "source": "llm"}


def test_unknown_resource_type_all_providers_failing_returns_honest_unavailable(monkeypatch):
    import shared.component_catalog as component_catalog
    from shared.llm_router import AllProvidersFailedError

    async def fake_call_llm(**kwargs):
        raise AllProvidersFailedError([])

    monkeypatch.setattr(component_catalog, "call_llm", fake_call_llm)

    result = asyncio.run(
        check_component_compatibility("some_custom_resource", {}, "stateless_web_service", "aws_ecs", {})
    )
    assert result["compatible"] is False
    assert result["source"] == "unavailable"


def test_build_add_component_instruction_includes_params():
    instruction = build_add_component_instruction("ec2_instance", {"instance_type": "t3.micro", "purpose": "bastion"})
    assert instruction == "Add an EC2 instance with the following configuration: instance_type=t3.micro, purpose=bastion."


def test_build_add_component_instruction_with_no_params():
    instruction = build_add_component_instruction("efs_file_system", {})
    assert instruction == "Add an EFS file system."
