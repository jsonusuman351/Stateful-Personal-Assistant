"""Conversation repository (T-010).

Typed async methods for conversation CRUD. Every query is scoped by user_id
(FR-17 data isolation) and uses SQLAlchemy's parameterised API — no f-strings
or string concatenation (NFR-13).
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.persistence.models.conversation import Conversation
from src.persistence.models.hitl import HITLApproval


class ConversationRepository:
    """Data access layer for conversation sessions.

    All read queries are scoped to the requesting user_id. Write operations
    targeting another user's conversation are no-ops (caller enforces 403).
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initialise with an async database session."""
        self._session = session

    async def list_conversations(
        self,
        user_id: UUID,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[Conversation], str | None]:
        """Return a page of conversations ordered by last_accessed DESC.

        Uses opaque cursor-based pagination: the cursor encodes the
        last_accessed timestamp of the final item from the previous page.

        Args:
            user_id: Scope results to this user only.
            cursor: Opaque pagination token from a previous call, or None
                for the first page.
            limit: Maximum number of items to return per page.

        Returns:
            Tuple of (items, next_cursor). next_cursor is None when there
            are no further pages.
        """
        cursor_ts: datetime | None = None
        if cursor:
            try:
                raw = base64.b64decode(cursor.encode()).decode()
                cursor_ts = datetime.fromisoformat(raw)
            except Exception:
                cursor_ts = None

        stmt = select(Conversation).where(Conversation.user_id == user_id)
        if cursor_ts is not None:
            stmt = stmt.where(Conversation.last_accessed < cursor_ts)
        stmt = stmt.order_by(Conversation.last_accessed.desc()).limit(limit + 1)

        result = await self._session.execute(stmt)
        rows = list(result.scalars())

        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = base64.b64encode(
                rows[-1].last_accessed.isoformat().encode()
            ).decode()
        else:
            next_cursor = None

        return rows, next_cursor

    async def get_conversation(
        self, user_id: UUID, conversation_id: UUID
    ) -> Conversation | None:
        """Fetch a single conversation, scoped to the requesting user.

        Returns None — not raises — when the conversation belongs to a
        different user. The caller is responsible for converting None to
        HTTP 403 or 404 as appropriate.

        Args:
            user_id: Must match the conversation's owner.
            conversation_id: Conversation to fetch.

        Returns:
            Conversation if found and owned by user_id, None otherwise.
        """
        stmt = select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_conversation(
        self, user_id: UUID, title: str
    ) -> Conversation:
        """Create a new conversation, disambiguating duplicate titles.

        If a conversation with the same title already exists for this user,
        appends " (2)", " (3)", etc. until a unique title is found (FR-19).

        Args:
            user_id: Owner of the new conversation.
            title: Desired title (will be modified if a duplicate exists).

        Returns:
            The newly created Conversation.
        """
        unique_title = await self._unique_title(user_id, title)
        conversation = Conversation(user_id=user_id, title=unique_title)
        self._session.add(conversation)
        await self._session.flush()
        return conversation

    async def _unique_title(self, user_id: UUID, title: str) -> str:
        """Return a title that is unique for this user, disambiguating if needed."""
        count_stmt = (
            select(func.count())
            .select_from(Conversation)
            .where(
                Conversation.user_id == user_id,
                Conversation.title == title,
            )
        )
        result = await self._session.execute(count_stmt)
        if result.scalar_one() == 0:
            return title

        suffix = 2
        while True:
            candidate = f"{title} ({suffix})"
            check_stmt = (
                select(func.count())
                .select_from(Conversation)
                .where(
                    Conversation.user_id == user_id,
                    Conversation.title == candidate,
                )
            )
            result2 = await self._session.execute(check_stmt)
            if result2.scalar_one() == 0:
                return candidate
            suffix += 1

    async def delete_conversation(
        self, user_id: UUID, conversation_id: UUID
    ) -> None:
        """Delete a conversation owned by user_id.

        Silently no-ops when the conversation_id belongs to a different user
        (caller must return HTTP 403 based on a prior ownership check).

        Args:
            user_id: Must match the conversation's owner.
            conversation_id: Conversation to delete.
        """
        stmt = delete(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
        await self._session.execute(stmt)

    async def update_last_accessed(self, conversation_id: UUID) -> None:
        """Bump last_accessed to now and increment access_count.

        Args:
            conversation_id: Conversation to update.
        """
        stmt = (
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(
                last_accessed=func.now(),
                access_count=Conversation.access_count + 1,
            )
        )
        await self._session.execute(stmt)

    async def delete_stale_conversations(self) -> int:
        """Delete conversations idle for 90+ days with fewer than 5 accesses.

        Uses a two-step SELECT ... FOR UPDATE SKIP LOCKED + DELETE so that rows
        held by active requests are skipped rather than blocked (FR-22).
        Conversations with any open HITL approval (used=FALSE AND expired=FALSE)
        are excluded regardless of age (FR-22, REVIEW-31 fix).

        Returns:
            Number of conversations deleted.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)

        open_hitl = (
            select(HITLApproval.conversation_id).where(
                HITLApproval.used.is_(False),
                HITLApproval.expired.is_(False),
            )
        )

        select_stmt = (
            select(Conversation.id)
            .where(
                Conversation.last_accessed < cutoff,
                Conversation.access_count < 5,
                ~Conversation.id.in_(open_hitl),
            )
            .with_for_update(skip_locked=True)
        )

        result = await self._session.execute(select_stmt)
        ids = list(result.scalars())

        if not ids:
            return 0

        del_stmt = delete(Conversation).where(Conversation.id.in_(ids))
        del_result = await self._session.execute(del_stmt)
        return del_result.rowcount  # type: ignore[attr-defined, no-any-return]
