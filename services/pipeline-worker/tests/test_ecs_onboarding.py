"""
services/pipeline-worker/tests/test_ecs_onboarding.py

Module 8 — real AWS ECS Fargate as a second deployment target. Covers the
bugs actually found live while proving this end to end against a real AWS
account (not simulated): EC2 security group descriptions must be plain
ASCII (an em dash broke CreateSecurityGroup), describe_load_balancers
raises LoadBalancerNotFoundException instead of returning an empty list
for an ALB that doesn't exist yet (unlike describe_target_groups, which
this module already handled), and ECS's CreateService rejects a target
group that isn't already referenced by a real listener rule — the listener
rule has to exist before the services do, the reverse of what seemed like
the natural build order. Also covers the weighted-traffic-shift function
(the ALB equivalent of the Kubernetes side's HTTPRoute weight patch),
proven live to produce a real, statistically-observable split (50/50
requested landed as 11/9 across two genuinely different app versions).

Fake boto3 clients only — no real AWS calls in this test suite, matching
this repo's established pattern for the equivalent Kubernetes tests
(test_deprovisioning.py, test_httproute_parent_namespace.py).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from src.aws.ecs_manifest import EcsOnboardingSpec
import src.aws.ecs_onboarding as ecs_onboarding


# ─────────────────────────── EcsOnboardingSpec naming ───────────────────────────


def test_naming_convention_mirrors_the_kubernetes_side():
    spec = EcsOnboardingSpec(
        service_name="checkout",
        image="123456789012.dkr.ecr.us-east-1.amazonaws.com/checkout",
        baseline_tag="v1.0.0",
        canary_tag="v1.1.0",
        tenant_id="tenant-x",
    )
    assert spec.baseline_target_group_name == "checkout-baseline"
    assert spec.canary_target_group_name == "checkout-canary"
    assert spec.baseline_service_name == "checkout-baseline"
    assert spec.canary_service_name == "checkout-canary"
    assert spec.path_prefix == "/api/v1/checkout"  # auto-derived, matches manifest_generator.py's own default


def test_target_group_names_are_truncated_to_the_real_aws_32_char_limit():
    spec = EcsOnboardingSpec(
        service_name="a-very-long-service-name-that-exceeds-limits",
        image="123456789012.dkr.ecr.us-east-1.amazonaws.com/x",
        baseline_tag="v1.0.0",
        canary_tag="v1.1.0",
        tenant_id="tenant-x",
    )
    assert len(spec.baseline_target_group_name) <= 32
    assert len(spec.canary_target_group_name) <= 32


# ─────────────────────────── security group descriptions ───────────────────────────


class _FakeEc2:
    def __init__(self):
        self.created_sgs = []
        self.described_calls = 0

    class exceptions:
        class ClientError(Exception):
            pass

    def describe_security_groups(self, Filters):
        return {"SecurityGroups": []}

    def create_security_group(self, GroupName, Description, VpcId):
        # Real bug found live: EC2 rejects non-ASCII characters (e.g. an em
        # dash) in GroupDescription with InvalidParameterValue — this
        # asserts every description this module ever sends is plain ASCII.
        Description.encode("ascii")
        self.created_sgs.append({"GroupName": GroupName, "Description": Description})
        return {"GroupId": f"sg-{GroupName}"}

    def authorize_security_group_ingress(self, **kwargs):
        pass


def test_alb_security_group_description_is_plain_ascii():
    ec2 = _FakeEc2()
    ecs_onboarding.ensure_alb_security_group(ec2, "vpc-123")
    assert len(ec2.created_sgs) == 1
    ec2.created_sgs[0]["Description"].encode("ascii")  # raises if not


def test_task_security_group_description_is_plain_ascii():
    ec2 = _FakeEc2()
    ecs_onboarding.ensure_task_security_group(ec2, "vpc-123", "checkout-sg", 8080, "sg-alb")
    assert len(ec2.created_sgs) == 1
    ec2.created_sgs[0]["Description"].encode("ascii")  # raises if not


# ─────────────── Guaranteed Live Web App CI/CD — target group health check ───────────────


class _FakeElbv2TargetGroup:
    class exceptions:
        class TargetGroupNotFoundException(Exception):
            pass

    def __init__(self):
        self.created_kwargs = None
        self.modified_attributes = None

    def describe_target_groups(self, Names):
        raise self.exceptions.TargetGroupNotFoundException("not found")

    def create_target_group(self, **kwargs):
        self.created_kwargs = kwargs
        return {"TargetGroups": [{"TargetGroupArn": f"arn:aws:elasticloadbalancing:tg/{kwargs['Name']}"}]}

    def modify_target_group_attributes(self, **kwargs):
        self.modified_attributes = kwargs


def test_ensure_target_group_accepts_2xx_and_3xx_as_healthy():
    # Real gap found live: the bare default (HTTP 200 only) marked a target
    # "unhealthy" on any real 3xx redirect (a common nginx/Next.js
    # response) even though the app itself was completely healthy.
    elbv2 = _FakeElbv2TargetGroup()
    ecs_onboarding.ensure_target_group(elbv2, "checkout-baseline", "vpc-123", 8080, "/healthz")
    assert elbv2.created_kwargs["Matcher"] == {"HttpCode": "200-399"}


def test_ensure_target_group_still_passes_through_the_real_health_check_path():
    elbv2 = _FakeElbv2TargetGroup()
    ecs_onboarding.ensure_target_group(elbv2, "checkout-baseline", "vpc-123", 80, "/")
    assert elbv2.created_kwargs["HealthCheckPath"] == "/"
    assert elbv2.created_kwargs["Port"] == 80


def test_ensure_target_group_sets_a_short_deregistration_delay():
    # Real gap found live (2026-09-17): AWS's 300s default deregistration
    # delay left a wide window where an old task stays fully in-service
    # alongside a newly-cutover one, letting the ALB round-robin real
    # traffic to a stale target — see wait_for_target_group_healthy's own
    # "no stray targets" fix for the full story.
    elbv2 = _FakeElbv2TargetGroup()
    arn = ecs_onboarding.ensure_target_group(elbv2, "checkout-baseline", "vpc-123", 8080, "/healthz")
    assert elbv2.modified_attributes["TargetGroupArn"] == arn
    assert elbv2.modified_attributes["Attributes"] == [
        {"Key": "deregistration_delay.timeout_seconds", "Value": "30"}
    ]


# ─────────────────────────── ALB not-found handling ───────────────────────────


class _FakeElbv2NoAlbYet:
    """Simulates a fresh AWS account: describe_load_balancers raises
    LoadBalancerNotFoundException (not an empty list) when the shared ALB
    doesn't exist yet — the real bug found live on the very first call."""

    class exceptions:
        class LoadBalancerNotFoundException(Exception):
            pass

    def describe_load_balancers(self, Names):
        raise self.exceptions.LoadBalancerNotFoundException("not found")

    def create_load_balancer(self, **kwargs):
        self._created = True
        return {
            "LoadBalancers": [
                {
                    "LoadBalancerArn": "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/smartcd-platform-alb/abc",
                    "DNSName": "smartcd-platform-alb-123.us-east-1.elb.amazonaws.com",
                    "State": {"Code": "active"},
                }
            ]
        }

    def describe_listeners(self, LoadBalancerArn):
        return {"Listeners": []}

    def create_listener(self, **kwargs):
        return {"Listeners": [{"ListenerArn": "arn:aws:elasticloadbalancing:...:listener/app/x"}]}


def test_ensure_shared_alb_handles_a_fresh_account_with_no_alb_yet(monkeypatch):
    elbv2 = _FakeElbv2NoAlbYet()
    # Patch describe_load_balancers to return the created ALB on the SECOND
    # call (post-creation poll), matching the real create-then-poll flow.
    call_count = {"n": 0}
    original_describe = elbv2.describe_load_balancers

    def _describe(Names):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise elbv2.exceptions.LoadBalancerNotFoundException("not found")
        return {
            "LoadBalancers": [
                {
                    "LoadBalancerArn": "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/smartcd-platform-alb/abc",
                    "DNSName": "smartcd-platform-alb-123.us-east-1.elb.amazonaws.com",
                    "State": {"Code": "active"},
                }
            ]
        }

    elbv2.describe_load_balancers = _describe
    result = ecs_onboarding.ensure_shared_alb(elbv2, "vpc-123", ["subnet-1", "subnet-2"], "sg-alb")
    assert result["dns_name"] == "smartcd-platform-alb-123.us-east-1.elb.amazonaws.com"
    assert "listener_arn" in result


# ─────────────────────────── listener rule create/update ───────────────────────────


class _FakeElbv2Rules:
    def __init__(self, existing_rules=None):
        self.rules = existing_rules or []
        self.created = []
        self.modified = []

    def describe_rules(self, ListenerArn):
        return {"Rules": self.rules}

    def create_rule(self, ListenerArn, Conditions, Priority, Actions):
        rule = {"RuleArn": f"rule-{Priority}", "Conditions": Conditions, "Actions": Actions, "Priority": str(Priority)}
        self.rules.append(rule)
        self.created.append(rule)
        return {"Rules": [rule]}

    def modify_rule(self, RuleArn, Conditions=None, Actions=None):
        self.modified.append({"RuleArn": RuleArn, "Actions": Actions})


def test_listener_rule_created_with_correct_weights():
    elbv2 = _FakeElbv2Rules()
    ecs_onboarding.ensure_listener_rule(
        elbv2, "listener-arn", "/api/v1/checkout", "tg-baseline", "tg-canary",
        baseline_weight=100, canary_weight=0, priority=1,
    )
    assert len(elbv2.created) == 1
    tgs = elbv2.created[0]["Actions"][0]["ForwardConfig"]["TargetGroups"]
    assert tgs == [{"TargetGroupArn": "tg-baseline", "Weight": 100}, {"TargetGroupArn": "tg-canary", "Weight": 0}]


def test_listener_rule_updates_in_place_when_it_already_exists_for_this_path():
    existing = [
        {
            "RuleArn": "rule-1",
            "Priority": "1",
            "Conditions": [{"Field": "path-pattern", "Values": ["/api/v1/checkout*"]}],
            "Actions": [],
        }
    ]
    elbv2 = _FakeElbv2Rules(existing_rules=existing)
    ecs_onboarding.ensure_listener_rule(
        elbv2, "listener-arn", "/api/v1/checkout", "tg-baseline", "tg-canary",
        baseline_weight=50, canary_weight=50, priority=1,
    )
    assert len(elbv2.created) == 0, "must update the existing rule, not create a duplicate"
    assert len(elbv2.modified) == 1
    tgs = elbv2.modified[0]["Actions"][0]["ForwardConfig"]["TargetGroups"]
    assert tgs[0]["Weight"] == 50 and tgs[1]["Weight"] == 50


# ─────────────────────────── set_traffic_weights ───────────────────────────


class _FakeElbv2Weights:
    """`existing_tg_order` deliberately lets a test control whether the
    rule's stored TargetGroups array happens to match or mismatch the real
    baseline/canary identity — the real bug this whole test class exists to
    catch only manifests when they mismatch."""

    def __init__(self, existing_rule, tg_arns: dict[str, str]):
        self.existing_rule = existing_rule
        self.tg_arns = tg_arns  # {target_group_name: arn}

    class exceptions:
        class TargetGroupNotFoundException(Exception):
            pass

    def describe_load_balancers(self, Names):
        return {"LoadBalancers": [{"LoadBalancerArn": "alb-arn"}]}

    def describe_listeners(self, LoadBalancerArn):
        return {"Listeners": [{"Port": 80, "ListenerArn": "listener-arn"}]}

    def describe_rules(self, ListenerArn):
        return {"Rules": [self.existing_rule] if self.existing_rule else []}

    def describe_target_groups(self, Names):
        name = Names[0]
        if name not in self.tg_arns:
            raise self.exceptions.TargetGroupNotFoundException(name)
        return {"TargetGroups": [{"TargetGroupArn": self.tg_arns[name]}]}

    def modify_rule(self, RuleArn, Actions):
        self.existing_rule["_modified_actions"] = Actions


def test_set_traffic_weights_finds_the_rule_by_path_prefix_and_updates_only_the_weights(monkeypatch):
    existing_rule = {
        "RuleArn": "rule-1",
        "Conditions": [{"Field": "path-pattern", "Values": ["/api/v1/payments*"]}],
        "Actions": [
            {
                "Type": "forward",
                "ForwardConfig": {
                    "TargetGroups": [
                        {"TargetGroupArn": "tg-baseline", "Weight": 100},
                        {"TargetGroupArn": "tg-canary", "Weight": 0},
                    ]
                },
            }
        ],
    }
    fake = _FakeElbv2Weights(
        existing_rule, {"payments-aws-baseline": "tg-baseline", "payments-aws-canary": "tg-canary"}
    )
    monkeypatch.setattr(ecs_onboarding.boto3, "client", lambda service, region_name: fake)

    result = ecs_onboarding.set_traffic_weights("us-east-1", "payments-aws", "/api/v1/payments", 50, 50)
    assert result == {"status": "weights_updated", "baseline_weight": 50, "canary_weight": 50}
    modified_tgs = existing_rule["_modified_actions"][0]["ForwardConfig"]["TargetGroups"]
    assert modified_tgs[0]["TargetGroupArn"] == "tg-baseline" and modified_tgs[0]["Weight"] == 50
    assert modified_tgs[1]["TargetGroupArn"] == "tg-canary" and modified_tgs[1]["Weight"] == 50


def test_set_traffic_weights_is_correct_even_when_the_rules_stored_order_is_reversed(monkeypatch):
    """
    Real bug found live (2026-09-15): this function used to trust the
    EXISTING rule's own TargetGroups array position (index 0 = baseline,
    index 1 = canary) — but the ELBv2 API does not guarantee it echoes back
    target groups in the order they were submitted. A real graduation
    (100% baseline / 0% canary requested) landed as 100% canary / 0%
    baseline instead — invisible until a 100/0 split made it catastrophic,
    since a canary scaled to zero replicas was now receiving all traffic.
    This asserts the fix: the rule's OWN stored order is irrelevant — only
    each target group's real ARN identity (looked up by name) decides
    which weight it gets.
    """
    existing_rule = {
        "RuleArn": "rule-1",
        "Conditions": [{"Field": "path-pattern", "Values": ["/api/v1/payments*"]}],
        "Actions": [
            {
                "Type": "forward",
                # Deliberately stored in canary-then-baseline order, the
                # opposite of what ensure_listener_rule originally submits.
                "ForwardConfig": {
                    "TargetGroups": [
                        {"TargetGroupArn": "tg-canary", "Weight": 0},
                        {"TargetGroupArn": "tg-baseline", "Weight": 100},
                    ]
                },
            }
        ],
    }
    fake = _FakeElbv2Weights(
        existing_rule, {"payments-aws-baseline": "tg-baseline", "payments-aws-canary": "tg-canary"}
    )
    monkeypatch.setattr(ecs_onboarding.boto3, "client", lambda service, region_name: fake)

    ecs_onboarding.set_traffic_weights("us-east-1", "payments-aws", "/api/v1/payments", baseline_weight=100, canary_weight=0)

    modified_tgs = existing_rule["_modified_actions"][0]["ForwardConfig"]["TargetGroups"]
    by_arn = {tg["TargetGroupArn"]: tg["Weight"] for tg in modified_tgs}
    assert by_arn["tg-baseline"] == 100
    assert by_arn["tg-canary"] == 0


def test_set_traffic_weights_raises_a_clear_error_when_the_project_was_never_onboarded(monkeypatch):
    class _FakeElbv2:
        def describe_load_balancers(self, Names):
            return {"LoadBalancers": [{"LoadBalancerArn": "alb-arn"}]}

        def describe_listeners(self, LoadBalancerArn):
            return {"Listeners": [{"Port": 80, "ListenerArn": "listener-arn"}]}

        def describe_rules(self, ListenerArn):
            return {"Rules": []}

    monkeypatch.setattr(ecs_onboarding.boto3, "client", lambda service, region_name: _FakeElbv2())

    with pytest.raises(RuntimeError, match="never been onboarded|No listener rule found"):
        ecs_onboarding.set_traffic_weights("us-east-1", "nonexistent", "/api/v1/nonexistent", 50, 50)


# ─────────────────────────── ensure_service create-vs-update ───────────────────────────


class _FakeEcsServices:
    def __init__(self, existing_active: bool):
        self.existing_active = existing_active
        self.created = []
        self.updated = []

    def describe_services(self, cluster, services):
        if self.existing_active:
            return {"services": [{"status": "ACTIVE"}]}
        return {"services": []}

    def update_service(self, **kwargs):
        self.updated.append(kwargs)

    def create_service(self, **kwargs):
        self.created.append(kwargs)


def test_ensure_service_creates_when_none_exists():
    ecs = _FakeEcsServices(existing_active=False)
    result = ecs_onboarding.ensure_service(
        ecs, "cluster", "svc", "task-def-arn", "tg-arn", "container", 8080, ["subnet-1"], "sg-1", 1
    )
    assert result["status"] == "created"
    assert len(ecs.created) == 1
    assert len(ecs.updated) == 0


def test_ensure_service_updates_in_place_when_it_already_exists():
    ecs = _FakeEcsServices(existing_active=True)
    result = ecs_onboarding.ensure_service(
        ecs, "cluster", "svc", "task-def-arn-v2", "tg-arn", "container", 8080, ["subnet-1"], "sg-1", 1
    )
    assert result["status"] == "updated"
    assert len(ecs.created) == 0
    assert len(ecs.updated) == 1
    assert ecs.updated[0]["taskDefinition"] == "task-def-arn-v2"


# ─────────────────────────── deprovision_ecs_service ───────────────────────────
#
# Real gap found live (2026-09-15): DELETE /api/v1/projects/{id} only ever
# called the Kubernetes-side /services/deprovision — a project onboarded
# with deploy_target "aws_ecs" had its real ECS services, target groups, and
# ALB listener rule left running (and billing) forever. Mirrors
# test_deprovisioning.py's coverage of the Kubernetes-side
# deprovision_service exactly: exact-object deletion, per-object
# not-found tolerance, nothing-to-deprovision is not an error. Never
# touches the shared ALB/listener/cluster itself.


class _FakeEcsDeprovision:
    def __init__(self, active_services: set[str] | None = None):
        self.active_services = active_services if active_services is not None else {"svc-baseline", "svc-canary"}
        self.deleted: list[str] = []

    class exceptions:
        class ClusterNotFoundException(Exception):
            pass

    def describe_services(self, cluster, services):
        name = services[0]
        if name in self.active_services:
            return {"services": [{"status": "ACTIVE"}]}
        return {"services": [{"status": "INACTIVE"}]} if name else {"services": []}

    def delete_service(self, cluster, service, force):
        self.deleted.append(service)


class _FakeElbv2Deprovision:
    def __init__(self, rule=None, missing_target_groups: set[str] | None = None):
        self.rule = rule
        self.missing_target_groups = missing_target_groups or set()
        self.deleted_rules: list[str] = []
        self.deleted_target_groups: list[str] = []

    class exceptions:
        class LoadBalancerNotFoundException(Exception):
            pass

        class TargetGroupNotFoundException(Exception):
            pass

    def describe_load_balancers(self, Names):
        return {"LoadBalancers": [{"LoadBalancerArn": "alb-arn"}]}

    def describe_listeners(self, LoadBalancerArn):
        return {"Listeners": [{"Port": 80, "ListenerArn": "listener-arn"}]}

    def describe_rules(self, ListenerArn):
        return {"Rules": [self.rule] if self.rule else []}

    def delete_rule(self, RuleArn):
        self.deleted_rules.append(RuleArn)

    def describe_target_groups(self, Names):
        name = Names[0]
        if name in self.missing_target_groups:
            raise self.exceptions.TargetGroupNotFoundException("not found")
        return {"TargetGroups": [{"TargetGroupArn": f"arn-{name}"}]}

    def delete_target_group(self, TargetGroupArn):
        self.deleted_target_groups.append(TargetGroupArn)


def _patch_deprovision_clients(monkeypatch, ecs, elbv2):
    def _client(service, region_name):
        return ecs if service == "ecs" else elbv2

    monkeypatch.setattr(ecs_onboarding.boto3, "client", _client)


def test_deprovision_deletes_exactly_the_objects_onboarding_would_have_created(monkeypatch):
    rule = {
        "RuleArn": "rule-1",
        "Conditions": [{"Field": "path-pattern", "Values": ["/api/v1/svc*"]}],
        "Actions": [],
    }
    ecs = _FakeEcsDeprovision()
    elbv2 = _FakeElbv2Deprovision(rule=rule)
    _patch_deprovision_clients(monkeypatch, ecs, elbv2)

    result = ecs_onboarding.deprovision_ecs_service("us-east-1", "svc", "/api/v1/svc")

    assert sorted(ecs.deleted) == ["svc-baseline", "svc-canary"]
    assert elbv2.deleted_rules == ["rule-1"]
    assert sorted(elbv2.deleted_target_groups) == ["arn-svc-baseline", "arn-svc-canary"]
    assert sorted(result["deleted"]["services"]) == ["svc-baseline", "svc-canary"]


def test_deprovision_defaults_path_prefix_when_not_given(monkeypatch):
    """Mirrors EcsOnboardingSpec.__post_init__'s own default derivation —
    a project onboarded without an explicit path_prefix must still be
    findable and deletable by the SAME default the onboarding call used."""
    rule = {
        "RuleArn": "rule-1",
        "Conditions": [{"Field": "path-pattern", "Values": ["/api/v1/svc*"]}],
        "Actions": [],
    }
    ecs = _FakeEcsDeprovision()
    elbv2 = _FakeElbv2Deprovision(rule=rule)
    _patch_deprovision_clients(monkeypatch, ecs, elbv2)

    result = ecs_onboarding.deprovision_ecs_service("us-east-1", "svc", path_prefix=None)

    assert elbv2.deleted_rules == ["rule-1"]
    assert result["deleted"]["listener_rules"] == ["/api/v1/svc"]


def test_deprovision_ignores_individually_missing_objects(monkeypatch):
    """Real case this guards: a project whose AWS onboarding never fully
    succeeded (e.g. only the baseline service landed) must still clean up
    everything that DOES exist, not abort partway."""
    ecs = _FakeEcsDeprovision(active_services={"svc-baseline"})
    elbv2 = _FakeElbv2Deprovision(rule=None, missing_target_groups={"svc-canary"})
    _patch_deprovision_clients(monkeypatch, ecs, elbv2)

    result = ecs_onboarding.deprovision_ecs_service("us-east-1", "svc", "/api/v1/svc")

    assert result["deleted"]["services"] == ["svc-baseline"]
    assert result["deleted"]["listener_rules"] == []
    assert result["deleted"]["target_groups"] == ["svc-baseline"]


def test_deprovision_nothing_to_deprovision_is_not_an_error(monkeypatch):
    ecs = _FakeEcsDeprovision(active_services=set())
    elbv2 = _FakeElbv2Deprovision(rule=None, missing_target_groups={"svc-baseline", "svc-canary"})
    _patch_deprovision_clients(monkeypatch, ecs, elbv2)

    result = ecs_onboarding.deprovision_ecs_service("us-east-1", "svc", "/api/v1/svc")

    assert result == {
        "status": "deprovisioned",
        "deleted": {"services": [], "target_groups": [], "listener_rules": []},
    }


def test_deprovision_handles_a_load_balancer_that_was_never_created(monkeypatch):
    """A project that only ever had its ECS services created (the ALB step
    failed or was never reached) must still clean those up, not error out
    trying to find a listener rule on a nonexistent ALB."""

    class _FakeElbv2NoAlb(_FakeElbv2Deprovision):
        def describe_load_balancers(self, Names):
            raise self.exceptions.LoadBalancerNotFoundException("not found")

    ecs = _FakeEcsDeprovision()
    elbv2 = _FakeElbv2NoAlb(missing_target_groups={"svc-baseline", "svc-canary"})
    _patch_deprovision_clients(monkeypatch, ecs, elbv2)

    result = ecs_onboarding.deprovision_ecs_service("us-east-1", "svc", "/api/v1/svc")

    assert sorted(result["deleted"]["services"]) == ["svc-baseline", "svc-canary"]
    assert result["deleted"]["listener_rules"] == []
