"""Message repository (T-010).

Typed async methods for inserting and listing messages. Every query is scoped
by user_id (FR-17). save_message also increments conversations.message_count
atomically within the same session (no separate transaction needed).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.persistence.models.conversation import Conversation
from src.persistence.models.message import Message


class MessageRepository:
    """Data access layer for conversation messages."""

    def __init__(self, session: AsyncSession) -> None:
        """Initialise with an async database session."""
        self._session = session

    async def save_message(
        self,
        conversation_id: UUID,
        user_id: UUID,
        role: str,
        content: str,
        tool_name: str | None,
        turn_index: int,
    ) -> Message:
        """Insert a message and atomically increment conversation.message_count.

        Both the INSERT and the UPDATE run in the same session/transaction so
        they are committed or rolled back together.

        Args:
            conversation_id: Owning conversation.
            user_id: Message author (same as conversation owner).
            role: One of 'user', 'assistant', 'tool'.
            content: Message text.
            tool_name: Tool identifier, non-null only when role='tool'.
            turn_index: Zero-based index of the conversational turn.

        Returns:
            The newly created Message.
        """
        message = Message(
            conversation_id=conversation_id,
            user_id=user_id,
            role=role,
            content=content,
            tool_name=tool_name,
            turn_index=turn_index,
        )
        self._session.add(message)

        # Atomically keep message_count in sync
        stmt = (
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(message_count=Conversation.message_count + 1)
        )
        await self._session.execute(stmt)
        await self._session.flush()
        return message

    async def list_messages(
        self, user_id: UUID, conversation_id: UUID
    ) -> list[Message]:
        """Return all messages for a conversation, ordered by created_at ASC.

        Scoped to user_id so cross-user access returns an empty list.
        The caller is responsible for converting an empty list (from another
        user's conversation) to HTTP 403.

        Args:
            user_id: Must match each message's user_id.
            conversation_id: Conversation to list messages for.

        Returns:
            Messages ordered oldest-first.
        """
        stmt = (
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.user_id == user_id,
            )
            .order_by(Message.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars())
