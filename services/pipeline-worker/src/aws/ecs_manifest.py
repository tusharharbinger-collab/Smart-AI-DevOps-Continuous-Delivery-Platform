"""
services/pipeline-worker/src/aws/ecs_manifest.py

Module 8 — AWS ECS Fargate as a real second deployment target, alongside
the existing Kubernetes/Kind path. Deliberately mirrors
`k8s/manifest_generator.py`'s shape (one dataclass carrying every input, a
handful of pure naming helpers) rather than sharing code with it — ECS and
Kubernetes have genuinely different primitives (task definitions vs.
Deployments, target groups vs. Services, ALB listener rules vs. HTTPRoute),
and forcing a shared abstraction before a second real target existed would
have meant guessing at the interface. This is the first step of Module 8's
"the interface, refactor Kubernetes into it" — ECS proves itself as a real,
working target first; unifying both behind one `DeploymentTarget` interface
is a follow-up once there are two real implementations to generalize from,
not one imagined in advance.

Every generated name is derived from `service_name` alone, exactly matching
the Kubernetes side's own convention (`{service_name}-baseline`/`-canary`),
so the two targets are recognizable as the same platform.
"""
from dataclasses import dataclass


@dataclass
class EcsOnboardingSpec:
    service_name: str
    image: str  # ECR repo URI, no tag (e.g. "123456789012.dkr.ecr.us-east-1.amazonaws.com/widget")
    baseline_tag: str
    canary_tag: str
    tenant_id: str
    port: int = 8080
    health_check_path: str = "/healthz"
    path_prefix: str | None = None
    region: str = "us-east-1"
    # Smallest real Fargate size — this is a real, billable resource, and
    # nothing about this platform's onboarding flow needs more than the
    # minimum to prove a genuine deployment. A project can be resized later;
    # nothing here is a hard ceiling.
    cpu: str = "256"
    memory: str = "512"
    desired_count: int = 1

    def __post_init__(self):
        if not self.path_prefix:
            self.path_prefix = f"/api/v1/{self.service_name}"

    @property
    def cluster_name(self) -> str:
        # One shared cluster across every project, matching the Kubernetes
        # side's one-shared-Gateway pattern — an ECS cluster itself carries
        # no cost; only the tasks running inside it do.
        return "smartcd-platform"

    @property
    def baseline_target_group_name(self) -> str:
        # ALB target group names are capped at 32 characters and must be
        # unique per region/account — truncate defensively rather than let
        # a long service_name fail deep inside a real AWS API call.
        return f"{self.service_name}-baseline"[:32]

    @property
    def canary_target_group_name(self) -> str:
        return f"{self.service_name}-canary"[:32]

    @property
    def baseline_service_name(self) -> str:
        return f"{self.service_name}-baseline"

    @property
    def canary_service_name(self) -> str:
        return f"{self.service_name}-canary"

    @property
    def task_family_baseline(self) -> str:
        return f"{self.service_name}-baseline"

    @property
    def task_family_canary(self) -> str:
        return f"{self.service_name}-canary"

    @property
    def log_group_name(self) -> str:
        return f"/ecs/{self.service_name}"

    @property
    def security_group_name(self) -> str:
        return f"{self.service_name}-sg"


# One shared platform-wide ALB, matching the Kubernetes side's one shared
# Gateway — routing between projects happens via listener rules (path-based,
# same as HTTPRoute's PathPrefix match), not a new $16/month ALB per
# project. This is the single source of truth for that name; every module
# that needs to find it uses this constant instead of a second hardcoded
# string that could drift.
SHARED_ALB_NAME = "smartcd-platform-alb"
SHARED_ALB_SECURITY_GROUP_NAME = "smartcd-platform-alb-sg"
TASK_EXECUTION_ROLE_NAME = "smartcd-ecsTaskExecutionRole"
