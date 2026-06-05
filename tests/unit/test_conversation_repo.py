"""Tests for ConversationRepository (T-010)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock
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
            await _make_conversation(db_session, user_id, f"Conv {i}", now - timedelta(hours=i))

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
            await _make_conversation(db_session, user_id, f"Conv {i}", now - timedelta(hours=i))

        _, cursor = await repo.list_conversations(user_id, limit=2)
        items2, cursor2 = await repo.list_conversations(user_id, cursor=cursor, limit=2)

        assert len(items2) == 1
        assert cursor2 is None

    async def test_list_empty_returns_empty(self, repo: ConversationRepository) -> None:
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

    async def test_get_nonexistent_returns_none(self, repo: ConversationRepository) -> None:
        """get_conversation returns None for a random conversation_id."""
        result = await repo.get_conversation(uuid4(), uuid4())
        assert result is None


class TestCreateConversation:
    """Tests for create_conversation()."""

    async def test_create_basic(self, repo: ConversationRepository) -> None:
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


class TestSoftDeleteConversation:
    """Tests for soft_delete_conversation() and is_deleted read filtering."""

    async def test_soft_delete_sets_flag_not_removes_row(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """soft_delete_conversation sets is_deleted=True but keeps the row."""
        user_id = uuid4()
        conv = await _make_conversation(db_session, user_id, "ToSoftDelete")

        await repo.soft_delete_conversation(user_id, conv.id)

        row = await db_session.get(Conversation, conv.id)
        assert row is not None  # row still exists
        assert row.is_deleted is True

    async def test_soft_deleted_hidden_from_list(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """list_conversations excludes soft-deleted conversations."""
        user_id = uuid4()
        keep = await _make_conversation(db_session, user_id, "Keep")
        gone = await _make_conversation(db_session, user_id, "Gone")

        await repo.soft_delete_conversation(user_id, gone.id)

        items, _ = await repo.list_conversations(user_id)
        ids = {c.id for c in items}
        assert keep.id in ids
        assert gone.id not in ids

    async def test_soft_deleted_hidden_from_get(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """get_conversation returns None for a soft-deleted conversation."""
        user_id = uuid4()
        conv = await _make_conversation(db_session, user_id, "Hidden")

        await repo.soft_delete_conversation(user_id, conv.id)

        assert await repo.get_conversation(user_id, conv.id) is None

    async def test_soft_delete_other_user_is_noop(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """soft_delete_conversation is a no-op when user_id does not match."""
        owner = uuid4()
        attacker = uuid4()
        conv = await _make_conversation(db_session, owner, "Protected")

        await repo.soft_delete_conversation(attacker, conv.id)

        row = await db_session.get(Conversation, conv.id)
        assert row is not None
        assert row.is_deleted is False  # untouched

    async def test_unique_title_ignores_soft_deleted(
        self, repo: ConversationRepository, db_session: AsyncSession
    ) -> None:
        """Re-using a soft-deleted conversation's title yields no '(2)' suffix."""
        user_id = uuid4()
        first = await repo.create_conversation(user_id, "Project Notes")
        await db_session.flush()

        await repo.soft_delete_conversation(user_id, first.id)

        # The visible namespace no longer contains "Project Notes", so the new
        # conversation should reclaim the bare title rather than become "(2)".
        second = await repo.create_conversation(user_id, "Project Notes")
        assert second.title == "Project Notes"


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


# ── Mock-based unit tests (no real DB required) ───────────────────────────────


class TestConversationRepoMocked:
    """Covers all method bodies via AsyncMock — runs without TEST_DATABASE_URL."""

    def _make_repo(self) -> tuple[ConversationRepository, AsyncMock]:
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()
        mock_session.execute = AsyncMock()
        return ConversationRepository(mock_session), mock_session

    def _scalar_result(self, value: object) -> MagicMock:
        r = MagicMock()
        r.scalar_one_or_none.return_value = value
        r.scalars.return_value = iter([value] if value is not None else [])
        return r

    def _scalars_result(self, values: list[Any]) -> MagicMock:
        r = MagicMock()
        r.scalars.return_value = iter(values)
        return r

    def _scalar_one_result(self, value: object) -> MagicMock:
        r = MagicMock()
        r.scalar_one.return_value = value
        return r

    async def test_list_conversations_empty(self) -> None:
        """list_conversations returns empty list when session returns no rows."""
        repo, mock_session = self._make_repo()
        mock_session.execute.return_value = self._scalars_result([])

        items, cursor = await repo.list_conversations(uuid4())

        assert items == []
        assert cursor is None

    async def test_list_conversations_within_limit(self) -> None:
        """list_conversations returns all rows and next_cursor=None when ≤ limit rows."""
        repo, mock_session = self._make_repo()
        conv = MagicMock()
        mock_session.execute.return_value = self._scalars_result([conv])

        items, cursor = await repo.list_conversations(uuid4(), limit=5)

        assert items == [conv]
        assert cursor is None

    async def test_list_conversations_exceeds_limit_returns_cursor(self) -> None:
        """list_conversations truncates to limit and returns a next_cursor when more rows exist."""
        repo, mock_session = self._make_repo()
        ts = datetime.now(timezone.utc)
        convs = []
        for _ in range(3):
            c = MagicMock()
            c.last_accessed = ts
            convs.append(c)
        mock_session.execute.return_value = self._scalars_result(convs)

        items, cursor = await repo.list_conversations(uuid4(), limit=2)

        assert len(items) == 2
        assert cursor is not None

    async def test_list_conversations_invalid_cursor_ignored(self) -> None:
        """An invalid (non-base64) cursor is silently ignored; first page returned."""
        repo, mock_session = self._make_repo()
        mock_session.execute.return_value = self._scalars_result([])

        items, cursor = await repo.list_conversations(uuid4(), cursor="!!!invalid!!!")

        assert items == []

    async def test_list_conversations_valid_cursor(self) -> None:
        """A valid cursor passes datetime filter to the query."""
        import base64

        repo, mock_session = self._make_repo()
        mock_session.execute.return_value = self._scalars_result([])
        ts = datetime.now(timezone.utc)
        cursor = base64.b64encode(ts.isoformat().encode()).decode()

        items, _ = await repo.list_conversations(uuid4(), cursor=cursor)

        assert items == []

    async def test_get_conversation_returns_none(self) -> None:
        """get_conversation returns None when execute finds nothing."""
        repo, mock_session = self._make_repo()
        mock_session.execute.return_value = self._scalar_result(None)

        result = await repo.get_conversation(uuid4(), uuid4())

        assert result is None

    async def test_get_conversation_returns_conv(self) -> None:
        """get_conversation returns the conversation when found."""
        repo, mock_session = self._make_repo()
        conv = MagicMock()
        mock_session.execute.return_value = self._scalar_result(conv)

        result = await repo.get_conversation(uuid4(), uuid4())

        assert result is conv

    async def test_create_conversation_no_duplicate(self) -> None:
        """create_conversation uses the title as-is when no duplicate exists."""
        repo, mock_session = self._make_repo()
        mock_session.execute.return_value = self._scalar_one_result(0)

        conv = await repo.create_conversation(uuid4(), "Fresh Title")

        assert conv.title == "Fresh Title"
        mock_session.add.assert_called_once()
        mock_session.flush.assert_awaited_once()

    async def test_create_conversation_title_collision(self) -> None:
        """create_conversation appends ' (2)' suffix on first duplicate."""
        repo, mock_session = self._make_repo()

        # First call (title exists=1), second call (suffix (2) free=0)
        r_exists = MagicMock()
        r_exists.scalar_one.return_value = 1
        r_free = MagicMock()
        r_free.scalar_one.return_value = 0
        mock_session.execute = AsyncMock(side_effect=[r_exists, r_free])

        conv = await repo.create_conversation(uuid4(), "Chat")

        assert conv.title == "Chat (2)"

    async def test_delete_conversation_executes(self) -> None:
        """delete_conversation calls session.execute with a DELETE statement."""
        repo, mock_session = self._make_repo()
        mock_session.execute.return_value = MagicMock()

        await repo.delete_conversation(uuid4(), uuid4())

        mock_session.execute.assert_awaited_once()

    async def test_update_last_accessed_executes(self) -> None:
        """update_last_accessed calls session.execute with an UPDATE statement."""
        repo, mock_session = self._make_repo()
        mock_session.execute.return_value = MagicMock()

        await repo.update_last_accessed(uuid4())

        mock_session.execute.assert_awaited_once()

    async def test_delete_stale_conversations_no_ids(self) -> None:
        """delete_stale_conversations returns 0 when no stale conversations exist."""
        repo, mock_session = self._make_repo()
        mock_session.execute.return_value = self._scalars_result([])

        count = await repo.delete_stale_conversations()

        assert count == 0
        assert mock_session.execute.await_count == 1  # only the SELECT, no DELETE

    async def test_delete_stale_conversations_with_ids(self) -> None:
        """delete_stale_conversations issues DELETE and returns rowcount."""
        repo, mock_session = self._make_repo()
        stale_id = uuid4()

        select_result = MagicMock()
        select_result.scalars.return_value = iter([stale_id])

        del_result = MagicMock()
        del_result.rowcount = 1

        mock_session.execute.side_effect = [select_result, del_result]

        count = await repo.delete_stale_conversations()

        assert count == 1
        assert mock_session.execute.await_count == 2
