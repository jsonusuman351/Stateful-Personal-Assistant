"""Tests for user and refresh-token repository (T-009)."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.persistence.models.user import User
from src.persistence.repositories.user_repo import UserRepository


@pytest.fixture
async def repo(db_session: AsyncSession) -> UserRepository:
    """Provide a UserRepository instance with a fresh DB session."""
    return UserRepository(db_session)


class TestGetUserByEmail:
    """Tests for get_user_by_email()."""

    async def test_get_user_by_email_found(
        self, repo: UserRepository, db_session: AsyncSession
    ) -> None:
        """get_user_by_email returns a User when email exists."""
        user = User(email="alice@example.com", password_hash="hashed_pwd")
        db_session.add(user)
        await db_session.flush()

        result = await repo.get_user_by_email("alice@example.com")

        assert result is not None
        assert result.email == "alice@example.com"
        assert result.password_hash == "hashed_pwd"  # pragma: allowlist secret

    async def test_get_user_by_email_not_found(self, repo: UserRepository) -> None:
        """get_user_by_email returns None when email does not exist."""
        result = await repo.get_user_by_email("nonexistent@example.com")
        assert result is None

    async def test_get_user_by_email_case_sensitive(
        self, repo: UserRepository, db_session: AsyncSession
    ) -> None:
        """get_user_by_email is case-sensitive."""
        user = User(email="alice@example.com", password_hash="hashed_pwd")
        db_session.add(user)
        await db_session.flush()

        result = await repo.get_user_by_email("ALICE@EXAMPLE.COM")
        assert result is None


class TestCreateUser:
    """Tests for create_user()."""

    async def test_create_user(self, repo: UserRepository) -> None:
        """create_user inserts a new user and returns it."""
        user = await repo.create_user("bob@example.com", "hashed_pwd_2")

        assert user.id is not None
        assert user.email == "bob@example.com"
        assert user.password_hash == "hashed_pwd_2"  # pragma: allowlist secret
        assert user.failed_login_attempts == 0
        assert user.locked_until is None
        assert user.is_active is True

    async def test_create_user_generates_uuid(self, repo: UserRepository) -> None:
        """create_user generates a UUID v4 for the user."""
        user = await repo.create_user("charlie@example.com", "hashed_pwd_3")
        assert user.id is not None
        assert isinstance(user.id, type(uuid4()))

    async def test_create_user_duplicate_email_raises(
        self, repo: UserRepository, db_session: AsyncSession
    ) -> None:
        """create_user raises IntegrityError when email is not unique."""
        await repo.create_user("dave@example.com", "hashed_pwd_4")

        with pytest.raises(IntegrityError):
            await repo.create_user("dave@example.com", "different_hash")


class TestFailedLoginCounters:
    """Tests for increment_failed_logins and reset_failed_logins."""

    async def test_increment_failed_logins(
        self, repo: UserRepository, db_session: AsyncSession
    ) -> None:
        """increment_failed_logins atomically increments the counter."""
        user = User(email="eve@example.com", password_hash="hashed")
        db_session.add(user)
        await db_session.flush()

        result = await repo.increment_failed_logins(user.id)
        assert result == 1

        result = await repo.increment_failed_logins(user.id)
        assert result == 2

    async def test_reset_failed_logins(
        self, repo: UserRepository, db_session: AsyncSession
    ) -> None:
        """reset_failed_logins sets the counter back to 0."""
        user = User(email="frank@example.com", password_hash="hashed")
        db_session.add(user)
        await db_session.flush()

        await repo.increment_failed_logins(user.id)
        await repo.increment_failed_logins(user.id)
        await repo.reset_failed_logins(user.id)

        stmt = select(User).where(User.id == user.id)
        result = await db_session.execute(stmt)
        refreshed_user = result.scalar_one()
        assert refreshed_user.failed_login_attempts == 0


class TestLockUser:
    """Tests for lock_user()."""

    async def test_lock_user(
        self, repo: UserRepository, db_session: AsyncSession
    ) -> None:
        """lock_user sets locked_until to the given timestamp."""
        user = User(email="grace@example.com", password_hash="hashed")
        db_session.add(user)
        await db_session.flush()

        now = datetime.now(timezone.utc)
        until = now + timedelta(minutes=10)

        await repo.lock_user(user.id, until)

        stmt = select(User).where(User.id == user.id)
        result = await db_session.execute(stmt)
        refreshed_user = result.scalar_one()
        assert refreshed_user.locked_until is not None
        assert abs((refreshed_user.locked_until - until).total_seconds()) < 1


class TestRefreshTokenLifecycle:
    """Tests for store_refresh_token, revoke_refresh_token, and get_valid_refresh_token."""

    async def test_store_refresh_token(
        self, repo: UserRepository, db_session: AsyncSession
    ) -> None:
        """store_refresh_token inserts a new token and returns it."""
        user = User(email="helen@example.com", password_hash="hashed")
        db_session.add(user)
        await db_session.flush()

        jti = uuid4()
        expires_at = datetime.now(timezone.utc) + timedelta(days=30)

        token = await repo.store_refresh_token(jti, user.id, expires_at)

        assert token.jti == jti
        assert token.user_id == user.id
        assert abs((token.expires_at - expires_at).total_seconds()) < 1
        assert token.revoked_at is None

    async def test_get_valid_refresh_token_when_valid(
        self, repo: UserRepository, db_session: AsyncSession
    ) -> None:
        """get_valid_refresh_token returns a token that is not revoked and not expired."""
        user = User(email="ivan@example.com", password_hash="hashed")
        db_session.add(user)
        await db_session.flush()

        jti = uuid4()
        expires_at = datetime.now(timezone.utc) + timedelta(days=30)

        await repo.store_refresh_token(jti, user.id, expires_at)

        result = await repo.get_valid_refresh_token(jti)

        assert result is not None
        assert result.jti == jti
        assert result.revoked_at is None

    async def test_get_valid_refresh_token_when_revoked(
        self, repo: UserRepository, db_session: AsyncSession
    ) -> None:
        """get_valid_refresh_token returns None when token is revoked."""
        user = User(email="jasmine@example.com", password_hash="hashed")
        db_session.add(user)
        await db_session.flush()

        jti = uuid4()
        expires_at = datetime.now(timezone.utc) + timedelta(days=30)

        await repo.store_refresh_token(jti, user.id, expires_at)
        await repo.revoke_refresh_token(jti)

        result = await repo.get_valid_refresh_token(jti)
        assert result is None

    async def test_get_valid_refresh_token_when_expired(
        self, repo: UserRepository, db_session: AsyncSession
    ) -> None:
        """get_valid_refresh_token returns None when token has expired."""
        user = User(email="kevin@example.com", password_hash="hashed")
        db_session.add(user)
        await db_session.flush()

        jti = uuid4()
        # Token expired in the past
        expires_at = datetime.now(timezone.utc) - timedelta(days=1)

        await repo.store_refresh_token(jti, user.id, expires_at)

        result = await repo.get_valid_refresh_token(jti)
        assert result is None

    async def test_refresh_token_revocation(
        self, repo: UserRepository, db_session: AsyncSession
    ) -> None:
        """refresh_token_revocation test covers the revocation flow end-to-end."""
        user = User(email="lucy@example.com", password_hash="hashed")
        db_session.add(user)
        await db_session.flush()

        jti = uuid4()
        expires_at = datetime.now(timezone.utc) + timedelta(days=30)

        # Store a token
        token = await repo.store_refresh_token(jti, user.id, expires_at)
        assert token.revoked_at is None

        # Token is valid before revocation
        valid_token = await repo.get_valid_refresh_token(jti)
        assert valid_token is not None

        # Revoke the token
        await repo.revoke_refresh_token(jti)

        # Token is no longer valid
        revoked_token = await repo.get_valid_refresh_token(jti)
        assert revoked_token is None


class TestNoRawSqlStringConcatenation:
    """Test that user_repo.py uses no raw SQL or f-string concatenation (NFR-13).

    This test performs a static AST analysis of the source code to ensure all
    SQL queries use parameterised statements (SQLAlchemy's select/update API)
    rather than f-strings or string concatenation.
    """

    def test_no_raw_sql_string_concatenation(self) -> None:
        """Verify that user_repo.py contains no f-string or + SQL concatenation."""
        # Import the source code
        import inspect

        from src.persistence.repositories import user_repo

        source = inspect.getsource(user_repo)

        # Parse AST
        tree = ast.parse(source)

        # Check for f-strings (JoinedStr nodes)
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                # It's an f-string — reject if it looks like SQL
                fstring_value = ast.unparse(node)
                if any(
                    keyword in fstring_value.lower()
                    for keyword in ["select", "update", "insert", "delete", "where"]
                ):
                    pytest.fail(
                        f"Found SQL-like f-string in user_repo.py: {fstring_value}"
                    )

        # Check for string concatenation with +
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                # It's a + operation — reject if operands are strings
                if (
                    isinstance(node.left, ast.Constant)
                    and isinstance(node.left.value, str)
                ) or (
                    isinstance(node.right, ast.Constant)
                    and isinstance(node.right.value, str)
                ):
                    concat_value = ast.unparse(node)
                    if any(
                        keyword in concat_value.lower()
                        for keyword in ["select", "update", "insert", "delete", "where"]
                    ):
                        pytest.fail(
                            f"Found SQL-like string concatenation in user_repo.py: {concat_value}"
                        )
