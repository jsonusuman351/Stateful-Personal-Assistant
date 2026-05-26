"""Tests for MessageRepository (T-010)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.persistence.models.conversation import Conversation
from src.persistence.models.message import Message
from src.persistence.repositories.message_repo import MessageRepository


@pytest.fixture
async def repo(db_session: AsyncSession) -> MessageRepository:
    """MessageRepository bound to the test session."""
    return MessageRepository(db_session)


async def _make_conversation(session: AsyncSession, user_id: UUID) -> Conversation:
    """Helper: insert a conversation and return it."""
    conv = Conversation(user_id=user_id, title="Test Conv")
    session.add(conv)
    await session.flush()
    return conv


class TestSaveMessage:
    """Tests for save_message()."""

    async def test_save_message_inserts(
        self, repo: MessageRepository, db_session: AsyncSession
    ) -> None:
        """save_message inserts a new message with correct fields."""
        user_id = uuid4()
        conv = await _make_conversation(db_session, user_id)

        msg = await repo.save_message(
            conversation_id=conv.id,
            user_id=user_id,
            role="user",
            content="Hello!",
            tool_name=None,
            turn_index=0,
        )

        assert msg.id is not None
        assert msg.conversation_id == conv.id
        assert msg.user_id == user_id
        assert msg.role == "user"
        assert msg.content == "Hello!"
        assert msg.tool_name is None
        assert msg.turn_index == 0

    async def test_save_message_increments_message_count(
        self, repo: MessageRepository, db_session: AsyncSession
    ) -> None:
        """save_message atomically increments conversations.message_count."""
        user_id = uuid4()
        conv = await _make_conversation(db_session, user_id)
        assert conv.message_count == 0

        await repo.save_message(conv.id, user_id, "user", "Hi", None, 0)
        await db_session.refresh(conv)
        assert conv.message_count == 1

        await repo.save_message(conv.id, user_id, "assistant", "Hello!", None, 0)
        await db_session.refresh(conv)
        assert conv.message_count == 2

    async def test_save_tool_message(
        self, repo: MessageRepository, db_session: AsyncSession
    ) -> None:
        """save_message accepts role='tool' with a tool_name."""
        user_id = uuid4()
        conv = await _make_conversation(db_session, user_id)

        msg = await repo.save_message(
            conversation_id=conv.id,
            user_id=user_id,
            role="tool",
            content='{"temperature": "22C"}',
            tool_name="weather",
            turn_index=1,
        )

        assert msg.role == "tool"
        assert msg.tool_name == "weather"


class TestListMessages:
    """Tests for list_messages()."""

    async def test_messages_ordered_ascending(
        self, repo: MessageRepository, db_session: AsyncSession
    ) -> None:
        """list_messages returns messages ordered by created_at ASC."""
        user_id = uuid4()
        conv = await _make_conversation(db_session, user_id)

        # Insert messages with explicit timestamps to guarantee ordering
        now = datetime.now(timezone.utc)
        for i, role in enumerate(["user", "assistant", "user"]):
            msg = Message(
                conversation_id=conv.id,
                user_id=user_id,
                role=role,
                content=f"Message {i}",
                tool_name=None,
                turn_index=i,
            )
            msg.created_at = now + timedelta(seconds=i)
            db_session.add(msg)
        await db_session.flush()

        messages = await repo.list_messages(user_id, conv.id)

        assert len(messages) == 3
        assert messages[0].content == "Message 0"
        assert messages[1].content == "Message 1"
        assert messages[2].content == "Message 2"

    async def test_list_messages_scoped_to_user(
        self, repo: MessageRepository, db_session: AsyncSession
    ) -> None:
        """list_messages returns only messages owned by the requesting user."""
        owner = uuid4()
        attacker = uuid4()
        conv = await _make_conversation(db_session, owner)

        # Insert a message owned by the owner
        db_session.add(
            Message(
                conversation_id=conv.id,
                user_id=owner,
                role="user",
                content="Private message",
                tool_name=None,
                turn_index=0,
            )
        )
        await db_session.flush()

        # Attacker queries with their own user_id
        messages = await repo.list_messages(attacker, conv.id)

        assert messages == []

    async def test_list_messages_empty_conversation(
        self, repo: MessageRepository, db_session: AsyncSession
    ) -> None:
        """list_messages returns empty list for a conversation with no messages."""
        user_id = uuid4()
        conv = await _make_conversation(db_session, user_id)

        messages = await repo.list_messages(user_id, conv.id)

        assert messages == []


# ── Mock-based unit tests (no real DB required) ───────────────────────────────


class TestMessageRepoMocked:
    """Covers MessageRepository method bodies via AsyncMock."""

    def _make_repo(self) -> tuple[MessageRepository, AsyncMock]:
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()
        mock_session.execute = AsyncMock()
        return MessageRepository(mock_session), mock_session

    async def test_save_message_adds_and_flushes(self) -> None:
        """save_message adds a Message row, issues the count UPDATE, and flushes."""
        repo, mock_session = self._make_repo()
        mock_session.execute.return_value = MagicMock()
        conv_id = uuid4()
        user_id = uuid4()

        msg = await repo.save_message(conv_id, user_id, "user", "Hello", None, 0)

        mock_session.add.assert_called_once()
        mock_session.execute.assert_awaited_once()
        mock_session.flush.assert_awaited_once()
        assert msg.conversation_id == conv_id
        assert msg.user_id == user_id
        assert msg.role == "user"
        assert msg.content == "Hello"
        assert msg.tool_name is None
        assert msg.turn_index == 0

    async def test_save_message_tool_role(self) -> None:
        """save_message stores tool_name when role='tool'."""
        repo, mock_session = self._make_repo()
        mock_session.execute.return_value = MagicMock()

        msg = await repo.save_message(uuid4(), uuid4(), "tool", '{"x":1}', "calculator", 1)

        assert msg.role == "tool"
        assert msg.tool_name == "calculator"

    async def test_list_messages_returns_ordered_list(self) -> None:
        """list_messages returns all messages from the session result."""
        repo, mock_session = self._make_repo()
        msg1 = MagicMock()
        msg2 = MagicMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value = iter([msg1, msg2])
        mock_session.execute.return_value = mock_result

        messages = await repo.list_messages(uuid4(), uuid4())

        assert messages == [msg1, msg2]

    async def test_list_messages_empty(self) -> None:
        """list_messages returns [] when no messages are found."""
        repo, mock_session = self._make_repo()
        mock_result = MagicMock()
        mock_result.scalars.return_value = iter([])
        mock_session.execute.return_value = mock_result

        messages = await repo.list_messages(uuid4(), uuid4())

        assert messages == []
