# services/api-gateway/src/db/models.py
"""
SQLAlchemy ORM models that mirror the db/schema.sql DDL.
These are used for ORM queries; raw SQL is used only for DDL and complex reports.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey,
    Integer, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(Base):
    __tablename__ = "tenants"

    tenant_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(Text, nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    pipelines = relationship("Pipeline", back_populates="tenant", cascade="all, delete-orphan")


class Pipeline(Base):
    __tablename__ = "pipelines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name"),
    )

    pipeline_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False)
    name = Column(Text, nullable=False)
    policy_yaml = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    tenant = relationship("Tenant", back_populates="pipelines")
    executions = relationship("PipelineExecution", back_populates="pipeline", cascade="all, delete-orphan")


class Project(Base):
    """
    Phase 8 — a Render-style project: the repo/build/image metadata a user
    configures in the creation wizard. Owns a `Pipeline` (its generated
    declarative YAML) rather than replacing it, so a project's rollouts are
    ordinary pipeline executions and the existing verification/audit/
    actuation chain keeps working unchanged.
    """

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name"),
        CheckConstraint(
            "status IN ('IDLE','BUILDING','TESTING','VERIFYING','HEALTHY','ROLLED_BACK','FAILED')",
            name="ck_project_status",
        ),
    )

    project_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False)
    pipeline_id = Column(UUID(as_uuid=True), ForeignKey("pipelines.pipeline_id", ondelete="SET NULL"))
    name = Column(Text, nullable=False)
    # Nullable for pipelines adopted from the pre-project era — see
    # migrations/versions/0007_adopt_legacy_pipelines.py.
    repo_url = Column(Text)
    branch = Column(Text, nullable=False, default="main")
    root_directory = Column(Text, nullable=False, default="./")
    dockerfile_path = Column(Text, nullable=False, default="Dockerfile")
    test_command = Column(Text)
    container_image = Column(Text)
    active_production_tag = Column(Text, nullable=False, default="v1.0.0")
    canary_tag = Column(Text)
    status = Column(Text, nullable=False, default="IDLE")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    executions = relationship("PipelineExecution", back_populates="project")


class PipelineExecution(Base):
    __tablename__ = "pipeline_executions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','FAILED','PAUSED','AWAITING_APPROVAL','ROLLED_BACK')",
            name="ck_execution_status",
        ),
        CheckConstraint("current_traffic_weight BETWEEN 0 AND 100", name="ck_traffic_weight"),
    )

    pipeline_run_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False)
    pipeline_id = Column(UUID(as_uuid=True), ForeignKey("pipelines.pipeline_id"), nullable=False)
    # Phase 8 — nullable: a run triggered against a hand-registered pipeline
    # has no project and no git provenance.
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.project_id", ondelete="CASCADE"))
    trigger_type = Column(Text)
    commit_sha = Column(Text)
    commit_message = Column(Text)
    target_version = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="PENDING")
    current_stage = Column(Text)
    current_traffic_weight = Column(Integer, default=0)
    started_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    completed_at = Column(DateTime(timezone=True))

    pipeline = relationship("Pipeline", back_populates="executions")
    project = relationship("Project", back_populates="executions")
    execution_state = relationship("ExecutionState", back_populates="execution", uselist=False)
    verification_records = relationship("VerificationRecord", back_populates="execution")
    audit_entries = relationship("AuditLedger", back_populates="execution")
    stage_logs = relationship("StageLog", back_populates="execution", cascade="all, delete-orphan")


class StageLog(Base):
    """Phase 8 — durable per-stage output; Redis `logs:{run_id}` stays the live path."""

    __tablename__ = "stage_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("pipeline_executions.pipeline_run_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False)
    stage_name = Column(Text, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    execution = relationship("PipelineExecution", back_populates="stage_logs")


class ExecutionState(Base):
    __tablename__ = "execution_state"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','FAILED','PAUSED','AWAITING_APPROVAL','ROLLED_BACK')",
            name="ck_execstate_status",
        ),
        CheckConstraint("current_traffic_weight BETWEEN 0 AND 100", name="ck_execstate_weight"),
    )

    pipeline_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("pipeline_executions.pipeline_run_id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False)
    service_name = Column(Text, nullable=False)
    current_stage = Column(Text, nullable=False)
    current_traffic_weight = Column(Integer, nullable=False, default=0)
    status = Column(Text, nullable=False)
    last_updated = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    execution = relationship("PipelineExecution", back_populates="execution_state")


class VerificationRecord(Base):
    __tablename__ = "verification_records"
    __table_args__ = (
        CheckConstraint(
            "status IN ('HEALTHY','DEGRADED','FAILED','UNVERIFIABLE')",
            name="ck_verdict_status",
        ),
        CheckConstraint("composite_score BETWEEN 0 AND 100", name="ck_composite_score"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_confidence"),
    )

    verdict_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False)
    pipeline_run_id = Column(UUID(as_uuid=True), ForeignKey("pipeline_executions.pipeline_run_id"), nullable=False)
    status = Column(Text, nullable=False)
    composite_score = Column(Numeric(5, 2), nullable=False)
    confidence = Column(Numeric(4, 3), nullable=False)
    evidence = Column(JSONB, nullable=False, default=dict)
    tier1_breaches = Column(ARRAY(Text), nullable=False, default=list)
    rca_summary = Column(Text)
    hmac_signature = Column(Text, nullable=False)
    timestamp_utc = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    execution = relationship("PipelineExecution", back_populates="verification_records")


class PolicyRule(Base):
    __tablename__ = "policy_rules"

    policy_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False)
    pipeline_id = Column(UUID(as_uuid=True), ForeignKey("pipelines.pipeline_id"), nullable=False)
    rego_snapshot = Column(Text, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class AuditLedger(Base):
    __tablename__ = "audit_ledger"
    __table_args__ = (
        CheckConstraint(
            "action IN ('WEIGHT_UPDATE','ROLLBACK','PROMOTE','SCALE_ZERO','APPROVE','BLOCK','RIGHTSIZING')",
            name="ck_action_type",
        ),
    )

    actuation_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False)
    pipeline_run_id = Column(UUID(as_uuid=True), ForeignKey("pipeline_executions.pipeline_run_id"), nullable=False)
    action = Column(Text, nullable=False)
    verdict = Column(Text)
    confidence = Column(Numeric(4, 3))
    authorized_by = Column(Text, nullable=False)
    policy_rule = Column(Text)
    hmac_signature = Column(Text, nullable=False)
    canary_weight = Column(Integer)
    baseline_weight = Column(Integer)
    timestamp = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    execution = relationship("PipelineExecution", back_populates="audit_entries")


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "approver_role IN ('lead-sre','platform-admin','developer')",
            name="ck_approver_role",
        ),
    )

    approval_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False)
    pipeline_run_id = Column(UUID(as_uuid=True), ForeignKey("pipeline_executions.pipeline_run_id"), nullable=False)
    stage = Column(Text, nullable=False)
    approver_user_id = Column(UUID(as_uuid=True), nullable=False)
    approver_role = Column(Text, nullable=False)
    approved_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class CostAnalysis(Base):
    __tablename__ = "cost_analysis"

    cost_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False)
    pipeline_run_id = Column(UUID(as_uuid=True), ForeignKey("pipeline_executions.pipeline_run_id"), nullable=False)
    baseline_cost = Column(Numeric(10, 4), nullable=False)
    canary_cost = Column(Numeric(10, 4), nullable=False)
    delta_percent = Column(Numeric(6, 2), nullable=False)
    rightsizing_rec = Column(JSONB)
    computed_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
