"""
Phase 7e - the independent cost estimate. The price lookup is injected, so this verifies OUR arithmetic,
selection and honesty rules (unpriced is never treated as $0). Real prices are checked live separately.
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest

from shared.provisioning.aws_pricing import PriceUnavailable, estimate_template_cost


class FakePrices:
    """The real us-east-1 on-demand prices observed from the Price List API."""

    def __init__(self, fail=()):
        self.fail = set(fail)

    def _get(self, name, value):
        if name in self.fail:
            raise PriceUnavailable(f"{name} unavailable")
        return Decimal(value)

    def rds_instance_hourly(self, klass, engine, multi_az):
        return self._get("rds_instance_hourly", "0.036" if multi_az else "0.018")

    def rds_storage_gb_month(self, volume, multi_az):
        return self._get("rds_storage_gb_month", "0.23" if multi_az else "0.115")

    def alb_hourly(self):
        return self._get("alb_hourly", "0.0225")

    def alb_lcu_hourly(self):
        return self._get("alb_lcu_hourly", "0.008")

    def fargate_vcpu_hourly(self):
        return self._get("fargate_vcpu_hourly", "0.04048")

    def fargate_gb_hourly(self):
        return self._get("fargate_gb_hourly", "0.004445")

    def elasticache_node_hourly(self, node, engine):
        return self._get("elasticache_node_hourly", "0.017")

    def nat_gateway_hourly(self):
        return self._get("nat_gateway_hourly", "0.045")


def tpl(**resources):
    return {"Resources": resources}


def by(res, name):
    return next(i for i in res["items"] if i["resource"] == name)


def test_rds_instance_and_storage_use_hours_and_gb_months():
    res = estimate_template_cost(tpl(Db={"Type": "AWS::RDS::DBInstance", "Properties": {
        "DBInstanceClass": "db.t3.micro", "Engine": "postgres", "AllocatedStorage": "20"}}), FakePrices())
    assert by(res, "Db")["monthly_usd"] == 13.14          # 0.018 * 730
    assert by(res, "Db storage")["monthly_usd"] == 2.30   # 20 GB * 0.115
    assert res["total_monthly_usd"] == 15.44
    assert res["errors"] == 0


def test_multi_az_selects_the_multi_az_prices():
    res = estimate_template_cost(tpl(Db={"Type": "AWS::RDS::DBInstance", "Properties": {
        "DBInstanceClass": "db.t3.micro", "Engine": "postgres", "MultiAZ": True, "AllocatedStorage": 20}}), FakePrices())
    assert by(res, "Db")["monthly_usd"] == 26.28          # 0.036 * 730
    assert by(res, "Db storage")["monthly_usd"] == 4.60   # 20 * 0.23


def test_string_true_and_string_numbers_from_cloudformation_are_understood():
    res = estimate_template_cost(tpl(Db={"Type": "AWS::RDS::DBInstance", "Properties": {
        "DBInstanceClass": "db.t3.micro", "Engine": "PostgreSQL", "MultiAZ": "true", "AllocatedStorage": "100"}}), FakePrices())
    assert by(res, "Db")["monthly_usd"] == 26.28 and by(res, "Db storage")["monthly_usd"] == 23.0


def test_alb_includes_the_assumed_lcu_and_says_so():
    res = estimate_template_cost(tpl(Alb={"Type": "AWS::ElasticLoadBalancingV2::LoadBalancer", "Properties": {}}), FakePrices())
    assert by(res, "Alb")["monthly_usd"] == pytest.approx(22.27, abs=0.01)  # (0.0225 + 0.008) * 730
    assert any("LCU" in a for a in res["assumptions"])


def test_network_load_balancers_are_reported_unpriced_not_priced_as_albs():
    res = estimate_template_cost(tpl(Nlb={"Type": "AWS::ElasticLoadBalancingV2::LoadBalancer", "Properties": {"Type": "network"}}), FakePrices())
    assert res["items"] == [] and res["unpriced"][0]["resource"] == "Nlb"


def test_fargate_service_cost_comes_from_the_referenced_task_definition():
    res = estimate_template_cost(tpl(
        Td={"Type": "AWS::ECS::TaskDefinition", "Properties": {"Cpu": "256", "Memory": "512"}},
        Svc={"Type": "AWS::ECS::Service", "Properties": {"LaunchType": "FARGATE", "DesiredCount": 2, "TaskDefinition": {"Ref": "Td"}}},
    ), FakePrices())
    # 2 tasks * (0.25 vCPU * 0.04048 + 0.5 GB * 0.004445) * 730
    assert by(res, "Svc")["monthly_usd"] == pytest.approx(18.02, abs=0.01)
    assert "AWS::ECS::TaskDefinition" in res["no_fixed_cost"]


def test_fargate_service_without_a_resolvable_task_definition_is_unpriced():
    res = estimate_template_cost(tpl(Svc={"Type": "AWS::ECS::Service", "Properties": {"TaskDefinition": "arn:aws:..."}}), FakePrices())
    assert res["items"] == [] and "Cpu/Memory" in res["unpriced"][0]["reason"]


def test_non_fargate_ecs_is_unpriced():
    res = estimate_template_cost(tpl(Svc={"Type": "AWS::ECS::Service", "Properties": {"LaunchType": "EC2"}}), FakePrices())
    assert res["unpriced"][0]["type"] == "AWS::ECS::Service"


def test_elasticache_and_nat_gateway():
    res = estimate_template_cost(tpl(
        Cache={"Type": "AWS::ElastiCache::CacheCluster", "Properties": {"CacheNodeType": "cache.t3.micro", "Engine": "redis", "NumCacheNodes": 2}},
        Nat={"Type": "AWS::EC2::NatGateway", "Properties": {}},
    ), FakePrices())
    assert by(res, "Cache")["monthly_usd"] == 24.82   # 2 * 0.017 * 730
    assert by(res, "Nat")["monthly_usd"] == 32.85     # 0.045 * 730
    assert any("data-processing" in a for a in res["assumptions"])


def test_free_and_usage_based_types_are_listed_not_priced_or_hidden():
    res = estimate_template_cost(tpl(
        Sg={"Type": "AWS::EC2::SecurityGroup"}, B={"Type": "AWS::S3::Bucket"}, Tg={"Type": "AWS::ElasticLoadBalancingV2::TargetGroup"}), FakePrices())
    assert res["total_monthly_usd"] == 0 and res["unpriced"] == []
    assert {"AWS::S3::Bucket", "AWS::EC2::SecurityGroup"} <= set(res["no_fixed_cost"])


def test_an_unknown_type_is_unpriced_never_silently_zero():
    res = estimate_template_cost(tpl(X={"Type": "AWS::SageMaker::Endpoint", "Properties": {}}), FakePrices())
    assert res["total_monthly_usd"] == 0
    assert res["unpriced"] == [{"resource": "X", "type": "AWS::SageMaker::Endpoint", "reason": "resource type is not priced by this estimator"}]
    assert res["errors"] == 0  # "not covered" is different from "lookup failed"


def test_a_failed_price_lookup_is_an_error_and_the_resource_is_unpriced():
    res = estimate_template_cost(tpl(
        Db={"Type": "AWS::RDS::DBInstance", "Properties": {"DBInstanceClass": "db.t3.micro", "Engine": "postgres"}},
        Alb={"Type": "AWS::ElasticLoadBalancingV2::LoadBalancer"}), FakePrices(fail={"rds_instance_hourly"}))
    assert [i["resource"] for i in res["items"]] == ["Alb"]
    assert res["errors"] == 1 and "price lookup failed" in res["unpriced"][0]["reason"]


def test_unresolvable_ref_properties_are_unpriced_not_guessed():
    res = estimate_template_cost(tpl(Db={"Type": "AWS::RDS::DBInstance", "Properties": {
        "DBInstanceClass": {"Ref": "InstanceClassParam"}, "Engine": "postgres"}}), FakePrices())
    assert res["items"] == [] and res["unpriced"][0]["resource"] == "Db"


def test_unsupported_storage_type_prices_the_instance_but_flags_the_storage():
    res = estimate_template_cost(tpl(Db={"Type": "AWS::RDS::DBInstance", "Properties": {
        "DBInstanceClass": "db.t3.micro", "Engine": "mysql", "AllocatedStorage": 50, "StorageType": "io1"}}), FakePrices())
    assert [i["resource"] for i in res["items"]] == ["Db"]
    assert res["unpriced"][0]["resource"] == "Db storage"


def test_empty_or_malformed_templates_do_not_crash():
    for t in ({}, {"Resources": None}, {"Resources": {}}):
        res = estimate_template_cost(t, FakePrices())
        assert res["total_monthly_usd"] == 0 and res["items"] == []


def test_a_full_web_service_with_database_total():
    res = estimate_template_cost(tpl(
        Alb={"Type": "AWS::ElasticLoadBalancingV2::LoadBalancer"},
        Td={"Type": "AWS::ECS::TaskDefinition", "Properties": {"Cpu": 256, "Memory": 512}},
        Svc={"Type": "AWS::ECS::Service", "Properties": {"TaskDefinition": {"Ref": "Td"}}},
        Db={"Type": "AWS::RDS::DBInstance", "Properties": {"DBInstanceClass": "db.t3.micro", "Engine": "postgres", "AllocatedStorage": 20}},
        Sg={"Type": "AWS::EC2::SecurityGroup"}), FakePrices())
    assert res["total_monthly_usd"] == pytest.approx(22.27 + 9.01 + 13.14 + 2.30, abs=0.03)
