"""
Regression tests for AwsPriceList's product SELECTION, built from the exact usagetype strings the real AWS
Price List API returned (observed live 2026-09-27). The first version of these selectors silently failed
on Multi-AZ RDS ("Multi-AZUsage:") and ElastiCache (no region prefix) - the unit tests with a fake price
lookup could not see that, only real data could.
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest

from shared.provisioning.aws_pricing import AwsPriceList, PriceUnavailable


def product(usagetype, price, unit="Hrs"):
    return {
        "product": {"attributes": {"usagetype": usagetype}},
        "terms": {"OnDemand": {"t": {"priceDimensions": {"d": {"unit": unit, "pricePerUnit": {"USD": price}}}}}},
    }


def prices_with(products):
    pl = AwsPriceList.__new__(AwsPriceList)  # skip creating a real boto3 client
    pl.region = "us-east-1"
    pl._products = lambda service, filters, max_results=100: products  # type: ignore[method-assign]
    return pl


def test_elasticache_skips_extended_support_rows_and_accepts_the_unprefixed_standard_row():
    pl = prices_with([
        product("USE1-ExtendedSupportYr1_Yr2-NodeUsage:cache.t3.micro", "0.0140000000"),
        product("USE1-ExtendedSupportYr3-NodeUsage:cache.t3.micro", "0.0270000000"),
        product("NodeUsage:cache.t3.micro", "0.0170000000"),
    ])
    assert pl.elasticache_node_hourly("cache.t3.micro", "Redis") == Decimal("0.017")


def test_elasticache_does_not_match_a_different_node_type_with_the_same_prefix():
    pl = prices_with([product("NodeUsage:cache.t3.micro.something", "9"), product("NodeUsage:cache.t3.small", "0.034")])
    with pytest.raises(PriceUnavailable):
        pl.elasticache_node_hourly("cache.t3.micro", "Redis")


def test_rds_multi_az_uses_the_multi_az_usagetype():
    pl = prices_with([product("Multi-AZUsage:db.t3.micro", "0.0360000000")])
    assert pl.rds_instance_hourly("db.t3.micro", "PostgreSQL", True) == Decimal("0.036")


def test_rds_single_az_uses_the_instance_usagetype_and_ignores_unrelated_rows():
    pl = prices_with([product("RDS:GP2-Storage", "0.115", "GB-Mo"), product("InstanceUsage:db.t3.micro", "0.0180000000")])
    assert pl.rds_instance_hourly("db.t3.micro", "PostgreSQL", False) == Decimal("0.018")


def test_fargate_picks_the_exact_on_demand_x86_row_not_spot_or_arm():
    pl = prices_with([
        product("USE1-Fargate-ARM-vCPU-Hours:perCPU", "0.03238", "hours"),
        product("USE1-SpotUsage-Fargate-vCPU-Hours:perCPU", "0.012", "hours"),
        product("USE1-Fargate-vCPU-Hours:perCPU", "0.04048", "hours"),
    ])
    assert pl.fargate_vcpu_hourly() == Decimal("0.04048")


def test_fargate_accepts_a_region_with_no_usagetype_prefix():
    pl = prices_with([product("Fargate-GB-Hours", "0.004445", "hours")])
    assert pl.fargate_gb_hourly() == Decimal("0.004445")


def test_alb_and_lcu_rows_are_told_apart():
    rows = [product("LCUUsage", "0.0080000000", "LCU-Hrs"), product("LoadBalancerUsage", "0.0225000000")]
    assert prices_with(rows).alb_hourly() == Decimal("0.0225")
    assert prices_with(rows).alb_lcu_hourly() == Decimal("0.008")


def test_nat_gateway_hours_not_bytes_or_regional_variant():
    rows = [product("NatGateway-Bytes", "0.045", "GB"), product("RegionalNatGateway-Hours", "0.045"),
            product("NatGateway-Hours", "0.0450000000")]
    assert prices_with(rows).nat_gateway_hourly() == Decimal("0.045")


def test_a_product_with_only_zero_prices_is_unavailable():
    with pytest.raises(PriceUnavailable):
        prices_with([product("LoadBalancerUsage", "0.0000000000")]).alb_hourly()


def test_no_matching_product_raises_price_unavailable_naming_the_region():
    with pytest.raises(PriceUnavailable, match="us-east-1"):
        prices_with([]).alb_hourly()


def test_rds_usagetypes_carry_a_region_prefix_outside_us_east_1():
    # Found live against eu-north-1: "EUN1-InstanceUsage:db.t3.micro" / "EUN1-Multi-AZUsage:db.t3.micro".
    single = prices_with([product("EUN1-InstanceUsage:db.t3.micro", "0.0170000000")])
    multi = prices_with([product("EUN1-Multi-AZUsage:db.t3.micro", "0.0340000000")])
    assert single.rds_instance_hourly("db.t3.micro", "PostgreSQL", False) == Decimal("0.017")
    assert multi.rds_instance_hourly("db.t3.micro", "PostgreSQL", True) == Decimal("0.034")


def test_rds_storage_with_a_region_prefix():
    pl = prices_with([product("EUN1-RDS:GP2-Storage", "0.121", "GB-Mo")])
    assert pl.rds_storage_gb_month("General Purpose", False) == Decimal("0.121")
