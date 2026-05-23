"""HITL approvals and audit log ORM models (T-007)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from src.persistence.models.base import Base


class HITLApproval(Base):
    """Human-in-the-loop approval with expiry and usage tracking."""

    __tablename__ = "hitl_approvals"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid, nullable=False, index=True
    )
    user_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    checkpoint_id: Mapped[str] = mapped_column(String(255), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HITLAuditLog(Base):
    """Append-only audit log of HITL approval decisions."""

    __tablename__ = "hitl_audit_log"

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=func.gen_random_uuid()
    )
    approval_id: Mapped[UUID] = mapped_column(
        Uuid, nullable=False, index=True
    )
    user_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    conversation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    decision: Mapped[str] = mapped_column(String(10), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now()
    )
    request_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "decision IN ('approve', 'deny', 'timeout')", name="check_decision"
        ),
    )
