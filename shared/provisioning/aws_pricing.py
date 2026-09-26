"""
shared/provisioning/aws_pricing.py

AI_INFRA_PROVISIONING_EXECUTION_PLAN.md 7A / Phase 7e - an INDEPENDENT monthly cost estimate for an
AI-generated CloudFormation template, computed from AWS's own published on-demand prices (the AWS Price
List API, `pricing:GetProducts`) instead of trusting the number the model wrote.

Why not Infracost: it is built on the same price data but needs a third-party API key and a CLI; AWS's own
API needs neither and already works with the credentials the platform holds. The tradeoff is that this
covers only the resource types below - everything else is reported as `unpriced` (never silently priced
at $0), so the total is always an honest "priced resources only" figure.

Pure logic (`estimate_template_cost`) takes an injectable price lookup so the arithmetic is unit-testable
without AWS; `AwsPriceList` is the real lookup. Synchronous (boto3) - callers use `asyncio.to_thread`.
"""
import re
from decimal import Decimal
from functools import lru_cache
from typing import Callable

import boto3
import structlog

logger = structlog.get_logger(__name__)

HOURS_PER_MONTH = 730  # AWS's own convention for monthly estimates (365 * 24 / 12)
ASSUMED_ALB_LCUS = 1  # an idle/low-traffic ALB still consumes ~1 LCU-hour; usage above that is not knowable from a template

# Resource types with no fixed monthly cost: free constructs, or purely usage-based (S3 storage, log
# ingestion, ...). Listed explicitly so "no fixed cost" is a decision, not an omission.
NO_FIXED_COST_TYPES = {
    "AWS::EC2::SecurityGroup", "AWS::EC2::SecurityGroupIngress", "AWS::EC2::SecurityGroupEgress",
    "AWS::EC2::VPC", "AWS::EC2::Subnet", "AWS::EC2::RouteTable", "AWS::EC2::Route",
    "AWS::EC2::SubnetRouteTableAssociation", "AWS::EC2::InternetGateway", "AWS::EC2::VPCGatewayAttachment",
    "AWS::ElasticLoadBalancingV2::TargetGroup", "AWS::ElasticLoadBalancingV2::Listener",
    "AWS::ElasticLoadBalancingV2::ListenerRule",
    "AWS::ECS::Cluster", "AWS::ECS::TaskDefinition",
    "AWS::IAM::Role", "AWS::IAM::Policy", "AWS::IAM::ManagedPolicy", "AWS::IAM::InstanceProfile",
    "AWS::Logs::LogGroup", "AWS::S3::Bucket", "AWS::S3::BucketPolicy",
    "AWS::RDS::DBSubnetGroup", "AWS::ElastiCache::SubnetGroup",
}

_RDS_ENGINES = {"postgres": "PostgreSQL", "postgresql": "PostgreSQL", "mysql": "MySQL", "mariadb": "MariaDB"}
_CACHE_ENGINES = {"redis": "Redis", "memcached": "Memcached", "valkey": "Valkey"}
_RDS_STORAGE_VOLUME = {"gp2": "General Purpose", "gp3": "General Purpose-GP3"}


class PriceUnavailable(Exception):
    """A price could not be looked up (permissions, throttling, or no matching product)."""


# ---------------------------------------------------------------------------------- real price lookup


class AwsPriceList:
    """Thin, cached wrapper over the Price List API. Every method returns a USD Decimal."""

    def __init__(self, region: str):
        self.region = region
        # The Price List API is served only from a couple of regions, regardless of the region being priced.
        self._client = boto3.client("pricing", region_name="us-east-1")

    def _products(self, service: str, filters: list[dict], max_results: int = 100) -> list[dict]:
        import json

        f = [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": self.region}] + filters
        try:
            resp = self._client.get_products(ServiceCode=service, Filters=f, MaxResults=max_results)
        except Exception as e:  # botocore ClientError / EndpointConnectionError - all mean "can't price"
            raise PriceUnavailable(f"{service}: {type(e).__name__}: {str(e)[:160]}") from e
        return [json.loads(raw) for raw in resp["PriceList"]]

    @staticmethod
    def _first_price(product: dict) -> Decimal:
        terms = list(product["terms"]["OnDemand"].values())[0]["priceDimensions"].values()
        for dim in terms:
            price = Decimal(dim["pricePerUnit"]["USD"])
            if price > 0:
                return price
        raise PriceUnavailable("product has no non-zero on-demand price")

    def _pick(self, service: str, filters: list[dict], usagetype_regex: str) -> Decimal:
        pattern = re.compile(usagetype_regex)
        for product in self._products(service, filters):
            if pattern.search(product["product"]["attributes"].get("usagetype", "")):
                return self._first_price(product)
        raise PriceUnavailable(f"{service}: no product matching {usagetype_regex} in {self.region}")

    @lru_cache(maxsize=256)
    def rds_instance_hourly(self, instance_class: str, engine: str, multi_az: bool) -> Decimal:
        return self._pick("AmazonRDS", [
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_class},
            {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": engine},
            {"Type": "TERM_MATCH", "Field": "deploymentOption", "Value": "Multi-AZ" if multi_az else "Single-AZ"},
        ], r"^([A-Z0-9]+-)?(Instance|Multi-AZ)Usage:")

    @lru_cache(maxsize=64)
    def rds_storage_gb_month(self, volume_type: str, multi_az: bool) -> Decimal:
        return self._pick("AmazonRDS", [
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Database Storage"},
            {"Type": "TERM_MATCH", "Field": "volumeType", "Value": volume_type},
            {"Type": "TERM_MATCH", "Field": "deploymentOption", "Value": "Multi-AZ" if multi_az else "Single-AZ"},
        ], r"Storage$")

    @lru_cache(maxsize=8)
    def alb_hourly(self) -> Decimal:
        return self._pick("AmazonEC2", [
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Load Balancer-Application"},
        ], r"(^|-)LoadBalancerUsage$")

    @lru_cache(maxsize=8)
    def alb_lcu_hourly(self) -> Decimal:
        return self._pick("AmazonEC2", [
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Load Balancer-Application"},
        ], r"(^|-)LCUUsage$")

    @lru_cache(maxsize=8)
    def fargate_vcpu_hourly(self) -> Decimal:
        # Exact usagetype: excludes the Spot / ARM / Windows / ephemeral-storage variants.
        return self._pick("AmazonECS", [
            {"Type": "CONTAINS", "Field": "usagetype", "Value": "Fargate-vCPU-Hours"},
        ], r"^([A-Z0-9]+-)?Fargate-vCPU-Hours:perCPU$")

    @lru_cache(maxsize=8)
    def fargate_gb_hourly(self) -> Decimal:
        return self._pick("AmazonECS", [
            {"Type": "CONTAINS", "Field": "usagetype", "Value": "Fargate-GB-Hours"},
        ], r"^([A-Z0-9]+-)?Fargate-GB-Hours$")

    @lru_cache(maxsize=64)
    def elasticache_node_hourly(self, node_type: str, engine: str) -> Decimal:
        # Excludes the "ExtendedSupport" price rows that share the same node type.
        return self._pick("AmazonElastiCache", [
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": node_type},
            {"Type": "TERM_MATCH", "Field": "cacheEngine", "Value": engine},
        ], r"^([A-Z0-9]+-)?NodeUsage:" + re.escape(node_type) + r"$")

    @lru_cache(maxsize=8)
    def nat_gateway_hourly(self) -> Decimal:
        return self._pick("AmazonEC2", [
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "NAT Gateway"},
        ], r"(^|-)NatGateway-Hours$")


# ---------------------------------------------------------------------------------- pure estimation


def _truthy(v) -> bool:
    return v is True or v == "true"


def _num(v, default=None):
    """CloudFormation numbers are often strings ("20"); a Ref/Fn:: object is not resolvable here -> default."""
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return default
    return default


def _money(x: Decimal | float) -> float:
    return round(float(x), 2)


def estimate_template_cost(template: dict, prices, region: str = "us-east-1") -> dict:
    """
    `prices` provides the lookup methods of AwsPriceList. Returns:
      total_monthly_usd  - sum of PRICED items only (see `unpriced`)
      items              - [{resource, type, monthly_usd, basis}]
      no_fixed_cost      - resource types with no fixed monthly cost (free or usage-based)
      unpriced           - [{resource, type, reason}] - excluded from the total, never assumed to be $0
      assumptions        - explicit, so the number is never presented as more certain than it is
      errors             - number of price lookups that FAILED (as opposed to types this module doesn't price)
    """
    resources = (template or {}).get("Resources", {}) or {}
    items: list[dict] = []
    unpriced: list[dict] = []
    no_fixed: set[str] = set()
    assumptions: list[str] = [f"{HOURS_PER_MONTH} hours/month, AWS on-demand prices for {region}"]
    errors = 0

    def add(resource, rtype, monthly, basis):
        items.append({"resource": resource, "type": rtype, "monthly_usd": _money(monthly), "basis": basis})

    def skip(resource, rtype, reason, is_error=False):
        nonlocal errors
        errors += 1 if is_error else 0
        unpriced.append({"resource": resource, "type": rtype, "reason": reason})

    for rid, res in resources.items():
        rtype = res.get("Type", "")
        p = res.get("Properties", {}) or {}
        try:
            if rtype == "AWS::RDS::DBInstance":
                klass, engine = p.get("DBInstanceClass"), _RDS_ENGINES.get(str(p.get("Engine", "")).lower())
                if not isinstance(klass, str) or engine is None:
                    skip(rid, rtype, f"unsupported or unresolvable instance class/engine ({p.get('Engine')})")
                    continue
                multi_az = _truthy(p.get("MultiAZ", False))
                hourly = prices.rds_instance_hourly(klass, engine, multi_az)
                add(rid, rtype, hourly * HOURS_PER_MONTH,
                    f"{klass} {engine} {'Multi-AZ' if multi_az else 'Single-AZ'} @ ${hourly}/hr")
                storage_gb = _num(p.get("AllocatedStorage"))
                volume = _RDS_STORAGE_VOLUME.get(str(p.get("StorageType", "gp2")).lower())
                if storage_gb and volume:
                    per_gb = prices.rds_storage_gb_month(volume, multi_az)
                    add(f"{rid} storage", "AWS::RDS::DBInstance/Storage", Decimal(str(storage_gb)) * per_gb,
                        f"{storage_gb:g} GB {p.get('StorageType', 'gp2')} @ ${per_gb}/GB-month")
                elif storage_gb:
                    skip(f"{rid} storage", rtype, f"storage type {p.get('StorageType')} is not priced")

            elif rtype == "AWS::ElasticLoadBalancingV2::LoadBalancer":
                if str(p.get("Type", "application")).lower() != "application":
                    skip(rid, rtype, f"load balancer type {p.get('Type')} is not priced")
                    continue
                hourly, lcu = prices.alb_hourly(), prices.alb_lcu_hourly()
                add(rid, rtype, (hourly + lcu * ASSUMED_ALB_LCUS) * HOURS_PER_MONTH,
                    f"ALB @ ${hourly}/hr + {ASSUMED_ALB_LCUS} LCU @ ${lcu}/hr")
                assumptions.append(f"ALB assumes {ASSUMED_ALB_LCUS} LCU average - traffic above that costs more")

            elif rtype == "AWS::ECS::Service":
                if str(p.get("LaunchType", "FARGATE")).upper() != "FARGATE":
                    skip(rid, rtype, f"launch type {p.get('LaunchType')} is not priced")
                    continue
                td_ref = p.get("TaskDefinition")
                td_id = td_ref.get("Ref") if isinstance(td_ref, dict) else None
                td = (resources.get(td_id) or {}).get("Properties", {}) if td_id else {}
                cpu, mem = _num(td.get("Cpu")), _num(td.get("Memory"))
                if not cpu or not mem:
                    skip(rid, rtype, "task definition Cpu/Memory not resolvable from the template")
                    continue
                count = _num(p.get("DesiredCount"), 1.0)
                vcpu_h, gb_h = prices.fargate_vcpu_hourly(), prices.fargate_gb_hourly()
                per_task = (Decimal(str(cpu / 1024)) * vcpu_h + Decimal(str(mem / 1024)) * gb_h) * HOURS_PER_MONTH
                add(rid, rtype, per_task * Decimal(str(count)),
                    f"{count:g} x Fargate task ({cpu / 1024:g} vCPU, {mem / 1024:g} GB) running continuously")

            elif rtype in ("AWS::ElastiCache::CacheCluster", "AWS::ElastiCache::ReplicationGroup"):
                node = p.get("CacheNodeType")
                engine = _CACHE_ENGINES.get(str(p.get("Engine", "redis")).lower())
                if not isinstance(node, str) or engine is None:
                    skip(rid, rtype, "unsupported or unresolvable node type/engine")
                    continue
                nodes = _num(p.get("NumCacheNodes") if rtype.endswith("CacheCluster") else p.get("NumCacheClusters"), 1.0)
                hourly = prices.elasticache_node_hourly(node, engine)
                add(rid, rtype, hourly * Decimal(str(nodes)) * HOURS_PER_MONTH, f"{nodes:g} x {node} {engine} @ ${hourly}/hr")

            elif rtype == "AWS::EC2::NatGateway":
                hourly = prices.nat_gateway_hourly()
                add(rid, rtype, hourly * HOURS_PER_MONTH, f"NAT gateway @ ${hourly}/hr")
                assumptions.append("NAT gateway data-processing charges ($/GB) are usage-based and not included")

            elif rtype in NO_FIXED_COST_TYPES:
                no_fixed.add(rtype)
            else:
                skip(rid, rtype, "resource type is not priced by this estimator")
        except PriceUnavailable as e:
            skip(rid, rtype, f"price lookup failed: {e}", is_error=True)

    return {
        "source": "aws-price-list",
        "currency": "USD",
        "total_monthly_usd": _money(sum(Decimal(str(i["monthly_usd"])) for i in items)),
        "items": items,
        "no_fixed_cost": sorted(no_fixed),
        "unpriced": unpriced,
        "assumptions": list(dict.fromkeys(assumptions)),
        "errors": errors,
    }


def estimate_for_region(template: dict, region: str) -> dict:
    return estimate_template_cost(template, AwsPriceList(region), region)
