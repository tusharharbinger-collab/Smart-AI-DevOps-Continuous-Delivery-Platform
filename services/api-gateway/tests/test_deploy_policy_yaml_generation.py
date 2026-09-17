"""
services/api-gateway/tests/test_deploy_policy_yaml_generation.py

Real gap found live: `generate_project_pipeline_yaml` hardcoded
`blockedDeployWindows: []` and a fixed `manualApprovalRequired` block for
EVERY project — the wizard never actually let a human declare a freeze
window or choose approver roles, despite both being explicit rubric items
("policy & gates" configuration). Covers the new DeployPolicy-driven
rendering: default behavior stays byte-for-byte equivalent to the old
hardcoded values (no silent behavior change for an existing caller), and a
custom policy renders correctly as real, parseable YAML.

Also regression-covers a real bug caught while building this: an unquoted
HH:MM value (e.g. 16:00) is valid YAML 1.1 sexagesimal notation — PyYAML's
safe_load silently parsed it as the integer 960 instead of the string
"16:00", which would have made every freeze-window comparison in
policies/delivery_guardrails.rego (a string comparison) silently never
match.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
import yaml
from fastapi import HTTPException

from src.routers.projects_router import (
    BlockedDeployWindow,
    CreateProjectRequest,
    DeployPolicy,
    generate_project_pipeline_yaml,
    _build_method_is_declared,
    _validate_deploy_mode,
)


def _render(body: CreateProjectRequest) -> dict:
    rendered = generate_project_pipeline_yaml(body, "tenant-1", "ns-1", "/api/v1/test-svc")
    return yaml.safe_load(rendered)


def _gates(body: CreateProjectRequest) -> dict:
    return _render(body)["spec"]["gates"]


def _base_request(**overrides) -> CreateProjectRequest:
    defaults = dict(name="test-svc", source_type="existing_image", container_image="registry.internal/test-svc")
    defaults.update(overrides)
    return CreateProjectRequest(**defaults)


def test_default_deploy_policy_matches_the_old_hardcoded_gates():
    gates = _gates(_base_request())
    assert gates["blockedDeployWindows"] == []
    assert gates["manualApprovalRequired"]["beforeStages"] == ["step_100_promotion"]
    assert gates["manualApprovalRequired"]["approverRoles"] == ["lead-sre", "platform-admin"]


def test_custom_blocked_window_renders_as_real_time_strings_not_sexagesimal_ints():
    gates = _gates(
        _base_request(
            deploy_policy=DeployPolicy(
                blocked_deploy_windows=[
                    BlockedDeployWindow(days=["Friday", "Saturday"], start_time="16:00", end_time="23:59")
                ]
            )
        )
    )
    window = gates["blockedDeployWindows"][0]
    assert window["days"] == ["Friday", "Saturday"]
    assert window["startTime"] == "16:00"
    assert window["endTime"] == "23:59"
    assert isinstance(window["startTime"], str) and isinstance(window["endTime"], str)


def test_multiple_blocked_windows_all_render():
    gates = _gates(
        _base_request(
            deploy_policy=DeployPolicy(
                blocked_deploy_windows=[
                    BlockedDeployWindow(days=["Friday"], start_time="16:00", end_time="23:59"),
                    BlockedDeployWindow(days=["Saturday", "Sunday"], start_time="00:00", end_time="23:59"),
                ]
            )
        )
    )
    assert len(gates["blockedDeployWindows"]) == 2


def test_manual_approval_disabled_clears_stages_and_roles():
    gates = _gates(_base_request(deploy_policy=DeployPolicy(manual_approval_required=False)))
    assert gates["manualApprovalRequired"]["beforeStages"] == []
    assert gates["manualApprovalRequired"]["approverRoles"] == []


def test_custom_approver_roles_are_honored():
    gates = _gates(_base_request(deploy_policy=DeployPolicy(manual_approval_roles=["developer"])))
    assert gates["manualApprovalRequired"]["approverRoles"] == ["developer"]


# ─────────────────────────── Module 8 — AWS deploy target ───────────────────────────
#
# Real gap this closes: worker.py's deploy stage and canary_loop first-
# deployment branch, and policy-controller's ongoing actuation, all need to
# know a project's real deploy target — the ONLY place that information can
# come from is this generated YAML (policy-controller never parses pipeline
# YAML itself; worker.py's `_register_actuation_target` reads exactly these
# fields off the canary_loop stage). Covers that both the `deploy` stage AND
# the `canary_loop` stage carry deploymentTarget/awsRegion/pathPrefix
# identically, and that a plain "kubernetes" project (the default, and
# every project onboarded before this feature) emits neither field at all —
# zero behavior change for the existing, already-proven Kubernetes path.


def test_aws_ecs_target_emits_deployment_target_on_both_deploy_and_canary_loop_stages():
    doc = _render(
        _base_request(
            source_type="existing_image",
            deploy_target="aws_ecs",
            aws_region="us-west-2",
            path_prefix="/api/v1/test-svc",
        )
    )
    stages = {s["name"]: s["config"] for s in doc["spec"]["stages"]}
    for stage_name in ("canary_deploy", "canary_verify"):
        cfg = stages[stage_name]
        assert cfg["deploymentTarget"] == "aws_ecs"
        assert cfg["awsRegion"] == "us-west-2"
        assert cfg["pathPrefix"] == "/api/v1/test-svc"


def test_kubernetes_target_emits_no_aws_fields_at_all():
    doc = _render(_base_request())  # deploy_target defaults to "kubernetes"
    stages = {s["name"]: s["config"] for s in doc["spec"]["stages"]}
    for stage_name in ("canary_deploy", "canary_verify"):
        cfg = stages[stage_name]
        assert "deploymentTarget" not in cfg
        assert "awsRegion" not in cfg
        assert "pathPrefix" not in cfg


# ─────────────── Guaranteed Live Web App CI/CD — blue-green deploy_mode ───────────────
#
# Real gap this closes: a genuinely new web app with zero real visitors can
# never accumulate the samples statistical canary verification needs, so it
# would sit DEGRADED forever and never reach its live URL. `deploy_mode:
# blue_green` routes worker.py's canary_loop into a health-gated cutover
# instead (see worker.py's is_blue_green branch and OPA's
# HEALTH_GATED_CUTOVER rule) — `deploymentStrategy: blue_green` in the
# generated YAML is the only signal that connects the two.


def test_blue_green_aws_ecs_emits_deployment_strategy_on_canary_loop_only():
    doc = _render(
        _base_request(
            deploy_target="aws_ecs",
            path_prefix="/api/v1/test-svc",
            deploy_policy=DeployPolicy(deploy_mode="blue_green"),
        )
    )
    stages = {s["name"]: s["config"] for s in doc["spec"]["stages"]}
    assert stages["canary_verify"]["deploymentStrategy"] == "blue_green"
    # Deliberately NOT on the deploy stage — worker.py never reads it there.
    assert "deploymentStrategy" not in stages["canary_deploy"]


def test_canary_deploy_mode_emits_no_deployment_strategy_field():
    doc = _render(_base_request(deploy_target="aws_ecs", path_prefix="/api/v1/test-svc"))
    cfg = {s["name"]: s["config"] for s in doc["spec"]["stages"]}["canary_verify"]
    assert "deploymentStrategy" not in cfg


def test_validate_deploy_mode_accepts_blue_green_on_aws_ecs():
    _validate_deploy_mode("blue_green", "aws_ecs")  # must not raise


def test_validate_deploy_mode_rejects_blue_green_on_kubernetes():
    with pytest.raises(HTTPException) as exc_info:
        _validate_deploy_mode("blue_green", "kubernetes")
    assert exc_info.value.status_code == 422


def test_validate_deploy_mode_rejects_unrecognized_mode():
    with pytest.raises(HTTPException) as exc_info:
        _validate_deploy_mode("rolling", "aws_ecs")
    assert exc_info.value.status_code == 422


def test_validate_deploy_mode_accepts_canary_on_either_target():
    _validate_deploy_mode("canary", "aws_ecs")
    _validate_deploy_mode("canary", "kubernetes")


# ─────────────── Guaranteed Live Web App CI/CD — synthesized build config ───────────────
#
# Real gap this closes: a static site / Vite "spa" has no start_command at
# all, but the build stage config used to always emit
# `startCommand: "{body.start_command}"` unconditionally — a literal,
# useless `startCommand: "None"` line for these. Covers the fix: the line
# is only emitted when a real start_command exists, `framework` is emitted
# only when set (node projects only), and a plain node-server/python
# project's config is completely unaffected.


def _build_stage_config(doc: dict) -> dict:
    return {s["name"]: s["config"] for s in doc["spec"]["stages"]}["build"]


def test_static_site_build_config_has_no_start_command_and_no_framework_line():
    doc = _render(
        _base_request(
            source_type="repository", repo_url="https://github.com/acme/static-site",
            container_image="registry.internal/static-site", language="static", manifest_path="index.html",
        )
    )
    cfg = _build_stage_config(doc)
    assert cfg["language"] == "static"
    assert "startCommand" not in cfg
    assert "framework" not in cfg
    assert cfg["manifestPath"] == "index.html"


def test_spa_build_config_emits_framework_and_no_start_command():
    doc = _render(
        _base_request(
            source_type="repository", repo_url="https://github.com/acme/vite-app",
            container_image="registry.internal/vite-app", language="node", framework="spa",
            manifest_path="package.json",
        )
    )
    cfg = _build_stage_config(doc)
    assert cfg["language"] == "node"
    assert cfg["framework"] == "spa"
    assert "startCommand" not in cfg


def test_build_method_declared_via_real_dockerfile():
    assert _build_method_is_declared("Dockerfile", None, None, None) is True


def test_build_method_declared_via_language_and_start_command():
    assert _build_method_is_declared(None, "python", "uvicorn main:app", None) is True


def test_build_method_not_declared_with_neither_dockerfile_nor_language():
    assert _build_method_is_declared(None, None, None, None) is False


def test_build_method_not_declared_with_language_but_no_start_command_for_a_plain_node_server():
    assert _build_method_is_declared(None, "node", None, None) is False


def test_build_method_declared_for_static_with_no_start_command():
    assert _build_method_is_declared(None, "static", None, None) is True


def test_build_method_declared_for_spa_with_no_start_command():
    assert _build_method_is_declared(None, "node", None, "spa") is True


def test_plain_node_server_build_config_is_unaffected_by_the_static_spa_change():
    doc = _render(
        _base_request(
            source_type="repository", repo_url="https://github.com/acme/api",
            container_image="registry.internal/api", language="node", start_command="npm start",
            manifest_path="package.json",
        )
    )
    cfg = _build_stage_config(doc)
    assert cfg["language"] == "node"
    assert cfg["startCommand"] == "npm start"
    assert "framework" not in cfg
