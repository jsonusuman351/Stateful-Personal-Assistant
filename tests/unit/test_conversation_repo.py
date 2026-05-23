"""Tests for ConversationRepository (T-010)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.persistence.models.conversation import Conversation
from src.persistence.repositories.conversation_repo import ConversationRepository


@pytest.fixture
async def repo(db_session: AsyncSession) -> ConversationRepository:
    """ConversationRepository bound to the test session."""
    return ConversationRepository(db_session)


async def _make_conversation(
    session: AsyncSession,
    user_id: UUID,
    title: str = "Test",
    last_accessed: datetime | None = None,
) -> Conversation:
    """Helper: insert a conversation directly and return it."""
    conv = Conversation(user_id=user_id, title=title)
    if last_accessed is not None:
        conv.last_accessed = last_accessed
    session.add(conv)
    await session.flush()
    return conv


class TestListConversations:
    """Tests for list_conversations()."""

    async def test_list_scoped_to_user(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """list_conversations returns only the requesting user's conversations."""
        owner = uuid4()
        other = uuid4()

        await _make_conversation(db_session, owner, "Mine")
        await _make_conversation(db_session, other, "Theirs")

        items, _ = await repo.list_conversations(owner)

        assert len(items) == 1
        assert items[0].title == "Mine"

    async def test_list_ordered_by_last_accessed_desc(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """list_conversations returns conversations newest-first."""
        user_id = uuid4()
        now = datetime.now(timezone.utc)

        await _make_conversation(db_session, user_id, "Old", now - timedelta(hours=2))
        await _make_conversation(db_session, user_id, "New", now - timedelta(hours=1))

        items, _ = await repo.list_conversations(user_id)

        assert items[0].title == "New"
        assert items[1].title == "Old"

    async def test_list_pagination_first_page(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """Requesting limit=2 from 3 items returns 2 items and a next_cursor."""
        user_id = uuid4()
        now = datetime.now(timezone.utc)

        for i in range(3):
            await _make_conversation(
                db_session, user_id, f"Conv {i}", now - timedelta(hours=i)
            )

        items, cursor = await repo.list_conversations(user_id, limit=2)

        assert len(items) == 2
        assert cursor is not None

    async def test_list_pagination_last_page(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """Second page exhausts items and returns next_cursor=None."""
        user_id = uuid4()
        now = datetime.now(timezone.utc)

        for i in range(3):
            await _make_conversation(
                db_session, user_id, f"Conv {i}", now - timedelta(hours=i)
            )

        _, cursor = await repo.list_conversations(user_id, limit=2)
        items2, cursor2 = await repo.list_conversations(user_id, cursor=cursor, limit=2)

        assert len(items2) == 1
        assert cursor2 is None

    async def test_list_empty_returns_empty(
        self, repo: ConversationRepository
    ) -> None:
        """list_conversations returns empty list when user has no conversations."""
        items, cursor = await repo.list_conversations(uuid4())
        assert items == []
        assert cursor is None


class TestGetConversation:
    """Tests for get_conversation()."""

    async def test_get_own_conversation(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """get_conversation returns the conversation when user_id matches."""
        user_id = uuid4()
        conv = await _make_conversation(db_session, user_id, "MyConv")

        result = await repo.get_conversation(user_id, conv.id)

        assert result is not None
        assert result.id == conv.id

    async def test_get_other_user_returns_none(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """get_conversation returns None when conversation belongs to another user."""
        owner = uuid4()
        attacker = uuid4()
        conv = await _make_conversation(db_session, owner, "Private")

        result = await repo.get_conversation(attacker, conv.id)

        assert result is None

    async def test_get_nonexistent_returns_none(
        self, repo: ConversationRepository
    ) -> None:
        """get_conversation returns None for a random conversation_id."""
        result = await repo.get_conversation(uuid4(), uuid4())
        assert result is None


class TestCreateConversation:
    """Tests for create_conversation()."""

    async def test_create_basic(
        self, repo: ConversationRepository
    ) -> None:
        """create_conversation inserts a new conversation and returns it."""
        user_id = uuid4()
        conv = await repo.create_conversation(user_id, "My First Chat")

        assert conv.id is not None
        assert conv.title == "My First Chat"
        assert conv.user_id == user_id
        assert conv.message_count == 0
        assert conv.access_count == 0

    async def test_title_disambiguation_first_duplicate(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """Second conversation with same title gets ' (2)' suffix."""
        user_id = uuid4()
        await _make_conversation(db_session, user_id, "Chat")

        conv = await repo.create_conversation(user_id, "Chat")

        assert conv.title == "Chat (2)"

    async def test_title_disambiguation_second_duplicate(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """Third conversation with same title gets ' (3)' suffix."""
        user_id = uuid4()
        await _make_conversation(db_session, user_id, "Chat")
        await _make_conversation(db_session, user_id, "Chat (2)")

        conv = await repo.create_conversation(user_id, "Chat")

        assert conv.title == "Chat (3)"

    async def test_title_disambiguation(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """Disambiguation skips occupied suffixes until a free slot is found."""
        user_id = uuid4()
        await _make_conversation(db_session, user_id, "Report")
        await _make_conversation(db_session, user_id, "Report (2)")
        await _make_conversation(db_session, user_id, "Report (3)")

        conv = await repo.create_conversation(user_id, "Report")

        assert conv.title == "Report (4)"

    async def test_different_users_can_share_title(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """Two different users can both have a conversation titled 'Chat'."""
        user_a = uuid4()
        user_b = uuid4()
        await _make_conversation(db_session, user_a, "Chat")

        conv = await repo.create_conversation(user_b, "Chat")

        assert conv.title == "Chat"


class TestDeleteConversation:
    """Tests for delete_conversation()."""

    async def test_delete_own_conversation(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """delete_conversation removes the conversation from the database."""
        user_id = uuid4()
        conv = await _make_conversation(db_session, user_id, "ToDelete")

        await repo.delete_conversation(user_id, conv.id)

        result = await db_session.get(Conversation, conv.id)
        assert result is None

    async def test_delete_other_user_is_noop(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """delete_conversation is a no-op when user_id does not match."""
        owner = uuid4()
        attacker = uuid4()
        conv = await _make_conversation(db_session, owner, "Protected")

        await repo.delete_conversation(attacker, conv.id)

        result = await db_session.get(Conversation, conv.id)
        assert result is not None  # still exists


class TestUpdateLastAccessed:
    """Tests for update_last_accessed()."""

    async def test_update_last_accessed_bumps_timestamp(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """update_last_accessed sets last_accessed to a newer timestamp."""
        user_id = uuid4()
        old_ts = datetime.now(timezone.utc) - timedelta(hours=1)
        conv = await _make_conversation(db_session, user_id, "Old", old_ts)

        await repo.update_last_accessed(conv.id)
        await db_session.refresh(conv)

        assert conv.last_accessed > old_ts

    async def test_update_last_accessed_increments_count(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """update_last_accessed increments access_count by 1."""
        user_id = uuid4()
        conv = await _make_conversation(db_session, user_id, "Counting")
        assert conv.access_count == 0

        await repo.update_last_accessed(conv.id)
        await db_session.refresh(conv)

        assert conv.access_count == 1
