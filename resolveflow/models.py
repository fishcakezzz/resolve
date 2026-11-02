from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def new_id() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    request_key: Mapped[str] = mapped_column(String(120), unique=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    customer_id: Mapped[str] = mapped_column(String(120))
    order_id: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(40), default="CREATED")
    version: Mapped[int] = mapped_column(Integer, default=1)
    evidence_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    __mapper_args__ = {"version_id_col": version}


class Subcase(Base):
    __tablename__ = "subcases"
    __table_args__ = (UniqueConstraint("case_id", "key", name="uq_subcase_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    key: Mapped[str] = mapped_column(String(80))
    issue_type: Mapped[str] = mapped_column(String(40))
    requested_amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    sku: Mapped[str | None] = mapped_column(String(120), nullable=True)
    depends_on: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(40), default="CREATED")
    version: Mapped[int] = mapped_column(Integer, default=1)

    __mapper_args__ = {"version_id_col": version}


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (UniqueConstraint("case_id", "field", name="uq_case_evidence_field"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    field: Mapped[str] = mapped_column(String(120))
    value: Mapped[object] = mapped_column(JSON)
    source_system: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30), default="VALID")
    version: Mapped[int] = mapped_column(Integer, default=1)
    queried_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Proposal(Base):
    __tablename__ = "proposals"
    __table_args__ = (UniqueConstraint("subcase_id", "version", name="uq_proposal_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    subcase_id: Mapped[str] = mapped_column(ForeignKey("subcases.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    action_type: Mapped[str] = mapped_column(String(30))
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    sku: Mapped[str | None] = mapped_column(String(120), nullable=True)
    evidence_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), default="DRAFT")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PolicyDecision(Base):
    __tablename__ = "policy_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), unique=True)
    policy_version: Mapped[str] = mapped_column(String(40), default="v1")
    decision: Mapped[str] = mapped_column(String(20))
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    evidence_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), unique=True)
    proposal_version: Mapped[int] = mapped_column(Integer)
    approver_id: Mapped[str] = mapped_column(String(120))
    decision: Mapped[str] = mapped_column(String(20))
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Outbox(Base):
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    external_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Event(Base):
    __tablename__ = "case_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    subcase_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DemoAction(Base):
    """Local stand-in for a payment / OMS action with external idempotency."""

    __tablename__ = "demo_actions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    action_type: Mapped[str] = mapped_column(String(30))
    order_id: Mapped[str] = mapped_column(String(120))
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    sku: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
