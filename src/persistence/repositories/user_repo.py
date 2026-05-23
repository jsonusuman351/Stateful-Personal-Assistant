"""User and refresh-token repository (T-009).

This module implements parameterised, async-safe data access for user accounts
and refresh tokens. All queries use SQLAlchemy's async API with explicit
parameter binding — never f-strings or string concatenation (NFR-13).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.persistence.models.user import RefreshToken, User


class UserRepository:
    """Data access layer for user accounts and refresh tokens.

    All queries are parameterised and use SQLAlchemy's async API.
    No raw SQL or f-string concatenation.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initialise with an async database session."""
        self._session = session

    async def get_user_by_email(self, email: str) -> User | None:
        """Fetch a user by email address.

        Args:
            email: Email address (case-sensitive).

        Returns:
            User object if found, None otherwise.
        """
        stmt = select(User).where(User.email == email)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_user(self, email: str, password_hash: str) -> User:
        """Create a new user account.

        Args:
            email: Email address (must be unique).
            password_hash: Argon2id hashed password.

        Returns:
            The newly created User object.

        Raises:
            sqlalchemy.exc.IntegrityError: If email is not unique.
        """
        user = User(email=email, password_hash=password_hash)
        self._session.add(user)
        await self._session.flush()
        return user

    async def increment_failed_logins(self, user_id: UUID) -> int:
        """Atomically increment the failed login counter.

        Args:
            user_id: User ID.

        Returns:
            The new value of failed_login_attempts.
        """
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(failed_login_attempts=User.failed_login_attempts + 1)
            .returning(User.failed_login_attempts)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def reset_failed_logins(self, user_id: UUID) -> None:
        """Reset the failed login counter to zero.

        Args:
            user_id: User ID.
        """
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(failed_login_attempts=0)
        )
        await self._session.execute(stmt)

    async def lock_user(self, user_id: UUID, until: datetime) -> None:
        """Lock a user account until a given timestamp.

        Args:
            user_id: User ID.
            until: UTC datetime after which the user is unlocked.
        """
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(locked_until=until)
        )
        await self._session.execute(stmt)

    async def store_refresh_token(
        self, jti: UUID, user_id: UUID, expires_at: datetime
    ) -> RefreshToken:
        """Store a new refresh token.

        Args:
            jti: JWT ID (unique identifier for this token).
            user_id: User ID.
            expires_at: UTC datetime when the token expires.

        Returns:
            The newly created RefreshToken object.
        """
        token = RefreshToken(jti=jti, user_id=user_id, expires_at=expires_at)
        self._session.add(token)
        await self._session.flush()
        return token

    async def revoke_refresh_token(self, jti: UUID) -> None:
        """Revoke a refresh token by marking it revoked.

        Args:
            jti: JWT ID to revoke.
        """
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.jti == jti)
            .values(revoked_at=datetime.now(timezone.utc))
        )
        await self._session.execute(stmt)

    async def get_valid_refresh_token(self, jti: UUID) -> RefreshToken | None:
        """Fetch a refresh token if it is valid (not revoked and not expired).

        Args:
            jti: JWT ID.

        Returns:
            RefreshToken object if found, not revoked, and not expired; None otherwise.
        """
        now = datetime.now(timezone.utc)
        stmt = select(RefreshToken).where(
            and_(
                RefreshToken.jti == jti,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > now,
            )
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
