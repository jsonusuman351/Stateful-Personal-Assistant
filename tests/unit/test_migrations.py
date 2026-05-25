"""Unit tests for Alembic migrations (T-008)."""

from __future__ import annotations

from pathlib import Path


class TestMigrationStructure:
    """Test suite for Alembic migration structure and syntax."""

    def test_alembic_ini_exists(self) -> None:
        """Alembic configuration file must exist."""
        assert Path("alembic.ini").exists(), "alembic.ini not found"

    def test_alembic_env_py_exists(self) -> None:
        """Alembic environment configuration must exist."""
        assert Path("alembic/env.py").exists(), "alembic/env.py not found"

    def test_alembic_script_template_exists(self) -> None:
        """Alembic migration template must exist."""
        assert Path("alembic/script.py.mako").exists(), "alembic/script.py.mako not found"

    def test_initial_migration_exists(self) -> None:
        """Initial migration file must exist."""
        assert Path("alembic/versions/001_initial_schema.py").exists(), (
            "001_initial_schema.py not found"
        )

    def test_migration_file_is_syntactically_valid(self) -> None:
        """Migration file must be valid Python."""
        import py_compile

        try:
            py_compile.compile("alembic/versions/001_initial_schema.py", doraise=True)
        except py_compile.PyCompileError as e:
            raise AssertionError(f"Migration file has syntax errors: {e}") from e

    def test_env_py_is_syntactically_valid(self) -> None:
        """Env.py must be valid Python."""
        import py_compile

        try:
            py_compile.compile("alembic/env.py", doraise=True)
        except py_compile.PyCompileError as e:
            raise AssertionError(f"env.py has syntax errors: {e}") from e

    def test_migration_has_upgrade_function(self) -> None:
        """Migration must define an upgrade() function."""
        with open("alembic/versions/001_initial_schema.py") as f:
            content = f.read()
        assert "def upgrade()" in content, "upgrade() function not found"

    def test_migration_has_downgrade_function(self) -> None:
        """Migration must define a downgrade() function (NFR-10)."""
        with open("alembic/versions/001_initial_schema.py") as f:
            content = f.read()
        assert "def downgrade()" in content, "downgrade() function not found"

    def test_migration_creates_users_table(self) -> None:
        """Migration must create users table."""
        with open("alembic/versions/001_initial_schema.py") as f:
            content = f.read()
        assert 'op.create_table(\n        "users"' in content, "users table creation not found"

    def test_migration_creates_refresh_tokens_table(self) -> None:
        """Migration must create refresh_tokens table."""
        with open("alembic/versions/001_initial_schema.py") as f:
            content = f.read()
        assert 'op.create_table(\n        "refresh_tokens"' in content, (
            "refresh_tokens table creation not found"
        )

    def test_migration_creates_conversations_table(self) -> None:
        """Migration must create conversations table."""
        with open("alembic/versions/001_initial_schema.py") as f:
            content = f.read()
        assert 'op.create_table(\n        "conversations"' in content, (
            "conversations table creation not found"
        )

    def test_migration_creates_messages_table(self) -> None:
        """Migration must create messages table."""
        with open("alembic/versions/001_initial_schema.py") as f:
            content = f.read()
        assert 'op.create_table(\n        "messages"' in content, (
            "messages table creation not found"
        )

    def test_migration_creates_hitl_approvals_table(self) -> None:
        """Migration must create hitl_approvals table."""
        with open("alembic/versions/001_initial_schema.py") as f:
            content = f.read()
        assert 'op.create_table(\n        "hitl_approvals"' in content, (
            "hitl_approvals table creation not found"
        )

    def test_migration_creates_hitl_audit_log_table(self) -> None:
        """Migration must create hitl_audit_log table."""
        with open("alembic/versions/001_initial_schema.py") as f:
            content = f.read()
        assert 'op.create_table(\n        "hitl_audit_log"' in content, (
            "hitl_audit_log table creation not found"
        )

    def test_migration_downgrade_drops_tables(self) -> None:
        """Downgrade must drop all created tables."""
        with open("alembic/versions/001_initial_schema.py") as f:
            content = f.read()

        required_drops = [
            'op.drop_table("hitl_audit_log")',
            'op.drop_table("hitl_approvals")',
            'op.drop_table("messages")',
            'op.drop_table("conversations")',
            'op.drop_table("refresh_tokens")',
            'op.drop_table("users")',
        ]

        for drop_statement in required_drops:
            assert drop_statement in content, f"Missing downgrade statement: {drop_statement}"

    def test_migration_has_revision_metadata(self) -> None:
        """Migration must have revision metadata."""
        with open("alembic/versions/001_initial_schema.py") as f:
            content = f.read()

        required_metadata = ["revision =", "down_revision =", "branch_labels ="]
        for metadata in required_metadata:
            assert metadata in content, f"Missing migration metadata: {metadata}"

    def test_env_py_imports_async_engine(self) -> None:
        """env.py must import async SQLAlchemy components."""
        with open("alembic/env.py") as f:
            content = f.read()
        assert "create_async_engine" in content, "create_async_engine not imported"
        assert "asyncio" in content, "asyncio not imported"

    def test_alembic_ini_has_logging_config(self) -> None:
        """alembic.ini must have logging configuration."""
        with open("alembic.ini") as f:
            content = f.read()
        assert "[loggers]" in content, "Logger configuration missing from alembic.ini"

    def test_users_table_has_id_column(self) -> None:
        """Users table must have id column."""
        with open("alembic/versions/001_initial_schema.py") as f:
            content = f.read()
        assert 'sa.Column("id", sa.Uuid()' in content, "users.id column not found"

    def test_users_table_has_email_column(self) -> None:
        """Users table must have email column with unique constraint."""
        with open("alembic/versions/001_initial_schema.py") as f:
            content = f.read()
        assert 'sa.Column("email", sa.String(length=255)' in content, "users.email column not found"
        assert 'sa.UniqueConstraint("email"' in content, "users.email unique constraint not found"

    def test_messages_table_has_role_check_constraint(self) -> None:
        """Messages table must have role CHECK constraint."""
        with open("alembic/versions/001_initial_schema.py") as f:
            content = f.read()
        assert "role IN ('user', 'assistant', 'tool')" in content, (
            "messages.role CHECK constraint not found"
        )

    def test_hitl_audit_log_has_decision_check_constraint(self) -> None:
        """HITLAuditLog must have decision CHECK constraint."""
        with open("alembic/versions/001_initial_schema.py") as f:
            content = f.read()
        assert "decision IN ('approve', 'deny', 'timeout')" in content, (
            "hitl_audit_log.decision CHECK constraint not found"
        )

    def test_migration_creates_indexes(self) -> None:
        """Migration must create indexes for performance."""
        with open("alembic/versions/001_initial_schema.py") as f:
            content = f.read()

        # Verify at least one index is created
        assert content.count("op.create_index") >= 1, "No indexes created"
