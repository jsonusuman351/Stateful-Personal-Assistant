"""Message ORM model (T-007)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from src.persistence.models.base import Base


class Message(Base):
    """Message in a conversation with role, content, and tool tracking."""

    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=func.gen_random_uuid()
    )
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid, nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid, nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now()
    )

    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant', 'tool')", name="check_role"),
    )
