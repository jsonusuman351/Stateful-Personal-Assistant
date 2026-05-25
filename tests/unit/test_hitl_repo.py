"""Tests for HITLRepository (T-011)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.persistence.models.hitl import HITLApproval, HITLAuditLog
from src.persistence.repositories.hitl_repo import HITLRepository


@pytest.fixture
async def repo(db_session: AsyncSession) -> HITLRepository:
    """HITLRepository bound to the test session."""
    return HITLRepository(db_session)


async def _make_approval(
    session: AsyncSession,
    *,
    conversation_id: UUID | None = None,
    user_id: UUID | None = None,
    tool_name: str = "dangerous_tool",
    used: bool = False,
    expired: bool = False,
    expires_at: datetime | None = None,
) -> HITLApproval:
    """Helper: insert a HITLApproval directly and return it."""
    approval = HITLApproval(
        id=uuid4(),
        conversation_id=conversation_id or uuid4(),
        user_id=user_id,
        tool_name=tool_name,
        thread_id="thread-1",
        checkpoint_id="checkpoint-1",
        used=used,
        expired=expired,
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    session.add(approval)
    await session.flush()
    return approval


class TestCreateApproval:
    """Tests for create_approval()."""

    async def test_create_approval_returns_uuid(
        self, repo: HITLRepository, db_session: AsyncSession
    ) -> None:
        """create_approval inserts a row and returns the approval_id UUID."""
        conv_id = uuid4()
        user_id = uuid4()

        approval_id = await repo.create_approval(
            conversation_id=conv_id,
            user_id=user_id,
            tool_name="send_email",
            thread_id="thread-abc",
            checkpoint_id="cp-123",
        )

        assert isinstance(approval_id, UUID)

        row = await db_session.get(HITLApproval, approval_id)
        assert row is not None
        assert row.conversation_id == conv_id
        assert row.user_id == user_id
        assert row.tool_name == "send_email"
        assert row.used is False
        assert row.expired is False

    async def test_create_approval_expires_in_10_minutes(
        self, repo: HITLRepository, db_session: AsyncSession
    ) -> None:
        """create_approval sets expires_at to approximately NOW() + 10 minutes."""
        before = datetime.now(timezone.utc)
        approval_id = await repo.create_approval(
            conversation_id=uuid4(),
            user_id=None,
            tool_name="tool",
            thread_id="t",
            checkpoint_id="c",
        )
        after = datetime.now(timezone.utc)

        row = await db_session.get(HITLApproval, approval_id)
        assert row is not None
        expected_min = before + timedelta(minutes=10)
        expected_max = after + timedelta(minutes=10)
        assert expected_min <= row.expires_at <= expected_max


class TestConsumeApproval:
    """Tests for consume_approval()."""

    async def test_consume_approval_atomic(
        self, repo: HITLRepository, db_session: AsyncSession
    ) -> None:
        """consume_approval marks the row as used and returns the approval."""
        approval = await _make_approval(db_session)

        result = await repo.consume_approval(approval.id, "session-1")

        assert result is not None
        assert result.id == approval.id

        await db_session.refresh(approval)
        assert approval.used is True

    async def test_double_consume_returns_none(
        self, repo: HITLRepository, db_session: AsyncSession
    ) -> None:
        """Second call to consume_approval returns None (already used)."""
        approval = await _make_approval(db_session)

        first = await repo.consume_approval(approval.id, "session-1")
        assert first is not None

        second = await repo.consume_approval(approval.id, "session-2")
        assert second is None

    async def test_expired_approval_returns_none(
        self, repo: HITLRepository, db_session: AsyncSession
    ) -> None:
        """consume_approval returns None for an approval flagged as expired."""
        approval = await _make_approval(db_session, expired=True)

        result = await repo.consume_approval(approval.id, "session-1")

        assert result is None


class TestExpireStaleApprovals:
    """Tests for expire_stale_approvals()."""

    async def test_expire_stale_returns_ids(
        self, repo: HITLRepository, db_session: AsyncSession
    ) -> None:
        """expire_stale_approvals returns UUIDs of approvals that were just expired."""
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        approval = await _make_approval(db_session, expires_at=past)

        expired_ids = await repo.expire_stale_approvals()

        assert approval.id in expired_ids

    async def test_expire_stale_skips_used(
        self, repo: HITLRepository, db_session: AsyncSession
    ) -> None:
        """expire_stale_approvals does not touch already-used approvals."""
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        approval = await _make_approval(db_session, used=True, expires_at=past)

        expired_ids = await repo.expire_stale_approvals()

        assert approval.id not in expired_ids

    async def test_expire_stale_skips_future(
        self, repo: HITLRepository, db_session: AsyncSession
    ) -> None:
        """expire_stale_approvals does not expire approvals that have not timed out."""
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        approval = await _make_approval(db_session, expires_at=future)

        expired_ids = await repo.expire_stale_approvals()

        assert approval.id not in expired_ids


class TestInsertAuditLog:
    """Tests for insert_audit_log()."""

    async def test_audit_log_inserted(self, repo: HITLRepository, db_session: AsyncSession) -> None:
        """insert_audit_log appends a row to hitl_audit_log."""
        approval = await _make_approval(db_session)
        user_id = uuid4()

        await repo.insert_audit_log(
            approval_id=approval.id,
            user_id=user_id,
            conversation_id=approval.conversation_id,
            tool_name="dangerous_tool",
            decision="approve",
            request_ip="127.0.0.1",
            decision_reason="Looks safe",
        )

        stmt = select(HITLAuditLog).where(HITLAuditLog.approval_id == approval.id)
        result = await db_session.execute(stmt)
        rows = list(result.scalars())

        assert len(rows) == 1
        assert rows[0].user_id == user_id
        assert rows[0].conversation_id == approval.conversation_id
        assert rows[0].tool_name == "dangerous_tool"
        assert rows[0].decision == "approve"
        assert rows[0].request_ip == "127.0.0.1"
        assert rows[0].decision_reason == "Looks safe"

    async def test_audit_log_multiple_entries(
        self, repo: HITLRepository, db_session: AsyncSession
    ) -> None:
        """insert_audit_log can be called multiple times for the same approval_id."""
        approval = await _make_approval(db_session)

        await repo.insert_audit_log(
            approval_id=approval.id,
            user_id=None,
            conversation_id=approval.conversation_id,
            tool_name="dangerous_tool",
            decision="timeout",
            request_ip=None,
            decision_reason=None,
        )
        await repo.insert_audit_log(
            approval_id=approval.id,
            user_id=uuid4(),
            conversation_id=approval.conversation_id,
            tool_name="dangerous_tool",
            decision="deny",
            request_ip="10.0.0.1",
            decision_reason="Not authorised",
        )

        stmt = select(HITLAuditLog).where(HITLAuditLog.approval_id == approval.id)
        result = await db_session.execute(stmt)
        rows = list(result.scalars())

        assert len(rows) == 2


class TestGetOpenApprovalIds:
    """Tests for get_open_approval_ids_for_conversation()."""

    async def test_returns_open_ids(self, repo: HITLRepository, db_session: AsyncSession) -> None:
        """get_open_approval_ids_for_conversation returns unused, unexpired IDs."""
        conv_id = uuid4()
        open_approval = await _make_approval(db_session, conversation_id=conv_id)
        await _make_approval(db_session, conversation_id=conv_id, used=True)
        await _make_approval(db_session, conversation_id=conv_id, expired=True)

        ids = await repo.get_open_approval_ids_for_conversation(conv_id)

        assert ids == [open_approval.id]

    async def test_scoped_to_conversation(
        self, repo: HITLRepository, db_session: AsyncSession
    ) -> None:
        """get_open_approval_ids_for_conversation ignores other conversations."""
        conv_a = uuid4()
        conv_b = uuid4()
        await _make_approval(db_session, conversation_id=conv_a)

        ids = await repo.get_open_approval_ids_for_conversation(conv_b)

        assert ids == []
