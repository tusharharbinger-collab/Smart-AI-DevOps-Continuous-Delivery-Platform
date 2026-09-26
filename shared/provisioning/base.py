"""
shared/provisioning/base.py

Phase 7 (AI_INFRA_PROVISIONING_EXECUTION_PLAN.md) — the narrow interface
every cloud-provisioning backend implements. AWS (via CloudFormation Change
Sets, see aws_cloudformation.py) is the only implementation built now; a
second cloud later means writing a second module against this same shape —
the approval state machine, api-gateway endpoints, and frontend never need
to know which provisioner is behind it.

Deliberately four methods, mirroring every real vendor's actual pattern
(Pulumi Neo, Harness IaCM — see that plan's §1 research): generate, preview
(a real dry-run diff with zero side effects), execute (only after a human
has seen the real preview), check status (polling, since provisioning is
asynchronous and can take minutes).
"""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ResourceChange:
    action: str  # "Add" | "Modify" | "Remove"
    logical_id: str
    resource_type: str


@dataclass
class ChangePreview:
    """The real, zero-risk dry-run result — AWS's own equivalent of `terraform plan`."""
    change_set_id: str
    stack_name: str
    stack_id: str | None
    status: str  # "READY" | "FAILED" | "NO_CHANGES"
    changes: list[ResourceChange] = field(default_factory=list)
    status_reason: str | None = None


@dataclass
class ProvisioningResult:
    stack_id: str
    status: str  # e.g. "UPDATE_IN_PROGRESS" | "CREATE_IN_PROGRESS"


@dataclass
class ProvisioningStatus:
    status: str  # e.g. "CREATE_COMPLETE" | "UPDATE_COMPLETE" | "ROLLBACK_COMPLETE" | "*_FAILED"
    is_terminal: bool
    succeeded: bool
    outputs: dict = field(default_factory=dict)
    status_reason: str | None = None


class InfraProvisioner(Protocol):
    def preview_changes(self, template_body: str, draft_id: str, region: str) -> ChangePreview: ...

    def execute_changes(self, change_set_id: str, stack_name: str, region: str) -> ProvisioningResult: ...

    def check_status(self, stack_name: str, region: str) -> ProvisioningStatus: ...
