"""HITL repository (T-011).

Atomic approval lifecycle management for Human-In-The-Loop gate.
All state transitions use single-statement UPDATE ... RETURNING (FR-10, NFR-16).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.persistence.models.hitl import HITLApproval, HITLAuditLog


class HITLRepository:
    """Data access layer for HITL approvals and append-only audit log.

    consume_approval uses a single UPDATE...WHERE used=false AND expired=false
    RETURNING to prevent double-consumption under concurrent requests (FR-10).
    insert_audit_log is append-only; no UPDATE/DELETE methods exist.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initialise with an async database session."""
        self._session = session

    async def create_approval(
        self,
        conversation_id: UUID,
        user_id: UUID | None,
        tool_name: str,
        thread_id: str,
        checkpoint_id: str,
    ) -> UUID:
        """Insert a new HITL approval row and return its approval_id.

        expires_at is set to NOW() + 10 minutes.

        Args:
            conversation_id: Owning conversation.
            user_id: Requesting user, or None for guest sessions.
            tool_name: Name of the tool requiring approval.
            thread_id: LangGraph thread identifier.
            checkpoint_id: LangGraph checkpoint at suspension point.

        Returns:
            The UUID primary key of the created approval row.
        """
        approval_id = uuid4()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        approval = HITLApproval(
            id=approval_id,
            conversation_id=conversation_id,
            user_id=user_id,
            tool_name=tool_name,
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
            expires_at=expires_at,
        )
        self._session.add(approval)
        await self._session.flush()
        return approval_id

    async def consume_approval(
        self,
        approval_id: UUID,
        session_id: str,  # noqa: ARG002 — accepted for API compat; HITLApproval has no session_id column
    ) -> HITLApproval | None:
        """Atomically mark an approval as used.

        Single UPDATE...WHERE used=false AND expired=false RETURNING prevents
        double-consumption under concurrent requests (FR-10).

        session_id is accepted for API compatibility but is not stored —
        HITLApproval has no session_id column (see T-011 deviations).

        Args:
            approval_id: The approval to consume.
            session_id: Caller session identifier (accepted, not stored).

        Returns:
            The HITLApproval on success, None if already used or expired.
        """
        stmt = (
            update(HITLApproval)
            .where(
                HITLApproval.id == approval_id,
                HITLApproval.used.is_(False),
                HITLApproval.expired.is_(False),
            )
            .values(used=True)
            .returning(HITLApproval)
        )
        result = await self._session.execute(stmt)
        return result.scalars().one_or_none()

    async def expire_stale_approvals(self) -> list[HITLApproval]:
        """Mark all un-consumed, past-deadline approvals as expired.

        Returns the full HITLApproval objects just expired so that the HITL
        timeout sweeper (T-045) has all fields needed to insert audit log
        entries (conversation_id, user_id, tool_name) without extra queries.

        Returns:
            List of HITLApproval objects transitioned to expired=True.
        """
        now = datetime.now(timezone.utc)
        stmt = (
            update(HITLApproval)
            .where(
                HITLApproval.expires_at < now,
                HITLApproval.used.is_(False),
                HITLApproval.expired.is_(False),
            )
            .values(expired=True)
            .returning(HITLApproval)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars())

    async def insert_audit_log(
        self,
        approval_id: UUID,
        user_id: UUID | None,
        conversation_id: UUID,
        tool_name: str,
        decision: str,
        request_ip: str | None,
        decision_reason: str | None,
    ) -> None:
        """Append one decision record to the append-only audit log.

        TASKS.md lists session_id here but HITLAuditLog has no such column;
        the model requires conversation_id instead (see T-011 deviations).

        Args:
            approval_id: Approval that was decided upon.
            user_id: User who made the decision, or None for timeout.
            conversation_id: Owning conversation (required by the model).
            tool_name: Tool name from the original approval request.
            decision: One of 'approve', 'deny', 'timeout'.
            request_ip: Client IP address, or None.
            decision_reason: Free-text reason, or None.
        """
        entry = HITLAuditLog(
            approval_id=approval_id,
            user_id=user_id,
            conversation_id=conversation_id,
            tool_name=tool_name,
            decision=decision,
            request_ip=request_ip,
            decision_reason=decision_reason,
        )
        self._session.add(entry)
        await self._session.flush()

    async def get_approval(self, approval_id: UUID) -> HITLApproval | None:
        """Fetch an approval row by ID regardless of its state.

        Used by the sessions router to distinguish 409 (concurrent race) from
        410 (already used/expired) before calling the atomic consume_approval.

        Args:
            approval_id: The approval UUID to look up.

        Returns:
            HITLApproval if found, None if the ID does not exist.
        """
        stmt = select(HITLApproval).where(HITLApproval.id == approval_id)
        result = await self._session.execute(stmt)
        return result.scalars().one_or_none()

    async def get_open_approval_ids_for_conversation(self, conversation_id: UUID) -> list[UUID]:
        """Return IDs of approvals that are neither used nor expired.

        Used by the retention job to find conversations with pending
        approvals before deletion (FR-22).

        Args:
            conversation_id: Conversation to query.

        Returns:
            List of open (unused and unexpired) approval UUIDs.
        """
        stmt = select(HITLApproval.id).where(
            HITLApproval.conversation_id == conversation_id,
            HITLApproval.used.is_(False),
            HITLApproval.expired.is_(False),
        )
        result = await self._session.execute(stmt)
        return list(result.scalars())
