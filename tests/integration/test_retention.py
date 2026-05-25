"""Integration tests for the conversation retention background job (T-046).

Requires a real PostgreSQL instance (TEST_DATABASE_URL env var). Tests that
use ``db_session`` roll back automatically via the fixture; ``test_in_progress``
manages its own transactions so it can hold a row lock across sessions.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.persistence.models.conversation import Conversation
from src.persistence.models.hitl import HITLApproval
from src.persistence.repositories.conversation_repo import ConversationRepository

_STALE = datetime.now(timezone.utc) - timedelta(days=91)
_RECENT = datetime.now(timezone.utc) - timedelta(days=1)


def _conv(
    *,
    user_id: uuid.UUID | None = None,
    last_accessed: datetime,
    access_count: int,
) -> Conversation:
    return Conversation(
        id=uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
        title="test",
        last_accessed=last_accessed,
        access_count=access_count,
    )


async def test_only_stale_low_access_deleted(db_session: AsyncSession) -> None:
    """Only rows that are both stale (>90d) AND low-access (<5) are deleted.

    Seeds four conversations covering all condition combinations:
    - stale + low_access   → deleted
    - stale + high_access  → kept
    - recent + low_access  → kept
    - recent + high_access → kept
    """
    stale_low = _conv(last_accessed=_STALE, access_count=2)
    stale_high = _conv(last_accessed=_STALE, access_count=10)
    recent_low = _conv(last_accessed=_RECENT, access_count=2)
    recent_high = _conv(last_accessed=_RECENT, access_count=10)

    db_session.add_all([stale_low, stale_high, recent_low, recent_high])
    await db_session.flush()

    repo = ConversationRepository(db_session)
    deleted = await repo.delete_stale_conversations()

    assert deleted == 1

    surviving = set((await db_session.execute(select(Conversation.id))).scalars())
    assert stale_low.id not in surviving
    assert stale_high.id in surviving
    assert recent_low.id in surviving
    assert recent_high.id in surviving


async def test_open_hitl_skipped(db_session: AsyncSession) -> None:
    """Conversations with an open HITL approval are never deleted (FR-22, REVIEW-31).

    The retention job must leave a stale+low-access conversation intact when
    it has an outstanding approval (used=FALSE AND expired=FALSE).
    """
    conv = _conv(last_accessed=_STALE, access_count=2)
    db_session.add(conv)
    await db_session.flush()

    approval = HITLApproval(
        id=uuid.uuid4(),
        conversation_id=conv.id,
        user_id=None,
        tool_name="web_search",
        thread_id="t1",
        checkpoint_id="c1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db_session.add(approval)
    await db_session.flush()

    repo = ConversationRepository(db_session)
    deleted = await repo.delete_stale_conversations()

    assert deleted == 0

    result = await db_session.execute(select(Conversation).where(Conversation.id == conv.id))
    assert result.scalar_one_or_none() is not None


async def test_in_progress_session_skipped(db_engine: Any) -> None:
    """FOR UPDATE SKIP LOCKED skips rows held by a live request (FR-22).

    Session A holds an exclusive row lock (simulating an active API request).
    The retention job (session B) must skip the locked row and return 0.
    After the lock is released the conversation remains untouched.
    """
    conv_id = uuid.uuid4()

    # Commit the stale conversation so both sessions can read it
    async with AsyncSession(db_engine, expire_on_commit=False) as setup:
        async with setup.begin():
            setup.add(
                Conversation(
                    id=conv_id,
                    user_id=uuid.uuid4(),
                    title="locked",
                    last_accessed=_STALE,
                    access_count=2,
                )
            )

    # Session A: hold a FOR UPDATE lock (simulates an in-progress request)
    async with AsyncSession(db_engine, expire_on_commit=False) as lock_session:
        async with lock_session.begin():
            await lock_session.execute(
                select(Conversation).where(Conversation.id == conv_id).with_for_update()
            )

            # Session B: retention job — must skip the locked row
            async with AsyncSession(db_engine, expire_on_commit=False) as job_session:
                async with job_session.begin():
                    repo = ConversationRepository(job_session)
                    deleted = await repo.delete_stale_conversations()
                    assert deleted == 0

        # lock_session.begin() exits → row lock released (implicit rollback)

    # Conversation was not deleted
    async with AsyncSession(db_engine, expire_on_commit=False) as verify:
        result = await verify.execute(select(Conversation).where(Conversation.id == conv_id))
        assert result.scalar_one_or_none() is not None


async def test_user_manual_delete_succeeds(db_session: AsyncSession) -> None:
    """User-initiated delete always succeeds, bypassing retention rules (FR-22).

    A recent+high-access conversation that the retention job would keep can
    still be removed via delete_conversation() (DELETE /sessions/{id}).
    """
    user_id = uuid.uuid4()
    conv = _conv(user_id=user_id, last_accessed=_RECENT, access_count=100)
    db_session.add(conv)
    await db_session.flush()

    repo = ConversationRepository(db_session)
    await repo.delete_conversation(user_id=user_id, conversation_id=conv.id)

    result = await db_session.execute(select(Conversation).where(Conversation.id == conv.id))
    assert result.scalar_one_or_none() is None
