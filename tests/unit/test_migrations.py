"""Unit tests for Alembic migrations (T-008)."""

from __future__ import annotations

import subprocess

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from src.config.settings import get_settings


def run_alembic_command(command: list[str]) -> tuple[int, str, str]:
    """Run an Alembic command and return exit code, stdout, stderr."""
    result = subprocess.run(
        command,
        cwd=".",
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


class TestMigrations:
    """Test suite for Alembic migrations."""

    def test_upgrade_head_succeeds(self) -> None:
        """Alembic upgrade head must complete without error."""
        # Downgrade to base first to ensure clean state
        downgrade_code, _, stderr = run_alembic_command(
            ["alembic", "downgrade", "base"]
        )
        assert downgrade_code == 0, f"downgrade base failed: {stderr}"

        # Now upgrade to head
        upgrade_code, _, stderr = run_alembic_command(
            ["alembic", "upgrade", "head"]
        )
        assert upgrade_code == 0, f"upgrade head failed: {stderr}"

    def test_downgrade_from_head_succeeds(self) -> None:
        """Alembic downgrade -1 after upgrade head must succeed."""
        # Ensure we're at head first
        upgrade_code, _, stderr = run_alembic_command(
            ["alembic", "upgrade", "head"]
        )
        assert upgrade_code == 0, f"initial upgrade failed: {stderr}"

        # Downgrade one revision
        downgrade_code, _, stderr = run_alembic_command(
            ["alembic", "downgrade", "-1"]
        )
        assert downgrade_code == 0, f"downgrade -1 failed: {stderr}"

    def test_upgrade_downgrade_idempotent(self) -> None:
        """Upgrade and downgrade should be idempotent."""
        # Downgrade to base to start clean
        run_alembic_command(["alembic", "downgrade", "base"])

        # Upgrade twice
        upgrade_code1, _, stderr1 = run_alembic_command(
            ["alembic", "upgrade", "head"]
        )
        assert upgrade_code1 == 0, f"first upgrade failed: {stderr1}"

        # Downgrade and upgrade again
        run_alembic_command(["alembic", "downgrade", "base"])
        upgrade_code2, _, stderr2 = run_alembic_command(
            ["alembic", "upgrade", "head"]
        )
        assert upgrade_code2 == 0, f"second upgrade failed: {stderr2}"

    def test_alembic_current_shows_head(self) -> None:
        """Alembic current should show head revision after upgrade."""
        # Upgrade to head
        run_alembic_command(["alembic", "upgrade", "head"])

        # Check current revision
        exit_code, stdout, stderr = run_alembic_command(
            ["alembic", "current"]
        )

        assert exit_code == 0, f"alembic current failed: {stderr}"
        assert "001" in stdout, "current revision should show migration 001"

    @pytest.mark.asyncio
    async def test_all_tables_created_after_upgrade(self) -> None:
        """After upgrade head, all required tables must exist."""
        # Upgrade to head
        run_alembic_command(["alembic", "upgrade", "head"])

        # Check that all required tables exist
        required_tables = {
            "users",
            "refresh_tokens",
            "conversations",
            "messages",
            "hitl_approvals",
            "hitl_audit_log",
        }

        settings = get_settings()
        engine = create_async_engine(settings.DATABASE_URL, echo=False)
        try:
            async with engine.begin() as connection:
                tables = await connection.run_sync(
                    lambda conn: conn.execute(
                        sa.text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                        )
                    ).fetchall()
                )

            existing_tables = {row[0] for row in tables}
            assert required_tables.issubset(
                existing_tables
            ), f"Missing tables: {required_tables - existing_tables}"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_users_table_has_required_columns(self) -> None:
        """Verify users table has correct columns."""
        run_alembic_command(["alembic", "upgrade", "head"])

        settings = get_settings()
        engine = create_async_engine(settings.DATABASE_URL, echo=False)
        try:
            async with engine.begin() as connection:
                columns = await connection.run_sync(
                    lambda conn: conn.execute(
                        sa.text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'users' "
                            "ORDER BY ordinal_position"
                        )
                    ).fetchall()
                )

            column_names = {row[0] for row in columns}

            # Verify required columns exist
            required_columns = {
                "id",
                "email",
                "password_hash",
                "created_at",
                "updated_at",
                "is_active",
                "failed_login_attempts",
                "locked_until",
            }
            assert required_columns.issubset(column_names), (
                f"Missing columns: {required_columns - column_names}"
            )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_messages_table_has_check_constraint(self) -> None:
        """Verify messages table has role check constraint."""
        run_alembic_command(["alembic", "upgrade", "head"])

        settings = get_settings()
        engine = create_async_engine(settings.DATABASE_URL, echo=False)
        try:
            async with engine.begin() as connection:
                constraints = await connection.run_sync(
                    lambda conn: conn.execute(
                        sa.text(
                            "SELECT constraint_name "
                            "FROM information_schema.table_constraints "
                            "WHERE table_name = 'messages' "
                            "AND constraint_type = 'CHECK'"
                        )
                    ).fetchall()
                )

            constraint_names = {row[0] for row in constraints}
            # At least one check constraint should exist for role
            assert len(constraint_names) > 0, (
                "messages should have at least one check constraint"
            )
        finally:
            await engine.dispose()
