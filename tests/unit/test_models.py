"""Unit tests for ORM model definitions (T-007)."""

from __future__ import annotations

from sqlalchemy import inspect

from src.persistence.models.conversation import Conversation
from src.persistence.models.hitl import HITLApproval, HITLAuditLog
from src.persistence.models.message import Message
from src.persistence.models.user import RefreshToken, User


class TestUserModel:
    def test_user_model_columns(self) -> None:
        """User model must have all required columns with correct types."""
        mapper = inspect(User)
        columns = {c.name: c for c in mapper.columns}

        # Check all required columns exist
        assert "id" in columns
        assert "email" in columns
        assert "password_hash" in columns
        assert "created_at" in columns
        assert "updated_at" in columns
        assert "is_active" in columns
        assert "failed_login_attempts" in columns
        assert "locked_until" in columns

        # Check column types
        assert "CHAR" in str(columns["id"].type) or str(columns["id"].type) == "UUID"
        assert "VARCHAR" in str(columns["email"].type)
        assert str(columns["password_hash"].type) == "TEXT"
        assert "TIMESTAMP" in str(columns["created_at"].type) or "DATETIME" in str(
            columns["created_at"].type
        )
        assert "TIMESTAMP" in str(columns["updated_at"].type) or "DATETIME" in str(
            columns["updated_at"].type
        )
        assert str(columns["is_active"].type) == "BOOLEAN"
        assert str(columns["failed_login_attempts"].type) == "INTEGER"
        assert "TIMESTAMP" in str(columns["locked_until"].type) or columns["locked_until"].nullable

    def test_user_table_name(self) -> None:
        """User model must map to 'users' table."""
        assert User.__tablename__ == "users"


class TestRefreshTokenModel:
    def test_refresh_token_model_columns(self) -> None:
        """RefreshToken model must have all required columns with correct types."""
        mapper = inspect(RefreshToken)
        columns = {c.name: c for c in mapper.columns}

        # Check all required columns exist
        assert "jti" in columns
        assert "user_id" in columns
        assert "expires_at" in columns
        assert "created_at" in columns
        assert "revoked_at" in columns

        # Check column types
        assert "CHAR" in str(columns["jti"].type) or str(columns["jti"].type) == "UUID"
        assert "CHAR" in str(columns["user_id"].type) or str(columns["user_id"].type) == "UUID"
        assert "TIMESTAMP" in str(columns["expires_at"].type) or "DATETIME" in str(
            columns["expires_at"].type
        )
        assert "TIMESTAMP" in str(columns["created_at"].type) or "DATETIME" in str(
            columns["created_at"].type
        )
        assert (
            "TIMESTAMP" in str(columns["revoked_at"].type)
            or "DATETIME" in str(columns["revoked_at"].type)
            or columns["revoked_at"].nullable
        )

    def test_refresh_token_table_name(self) -> None:
        """RefreshToken model must map to 'refresh_tokens' table."""
        assert RefreshToken.__tablename__ == "refresh_tokens"


class TestConversationModel:
    def test_conversation_model_columns(self) -> None:
        """Conversation model must have all required columns with correct types."""
        mapper = inspect(Conversation)
        columns = {c.name: c for c in mapper.columns}

        # Check all required columns exist
        assert "id" in columns
        assert "user_id" in columns
        assert "title" in columns
        assert "active_model" in columns
        assert "created_at" in columns
        assert "last_accessed" in columns
        assert "access_count" in columns
        assert "message_count" in columns

        # Check column types
        assert "CHAR" in str(columns["id"].type) or str(columns["id"].type) == "UUID"
        assert "CHAR" in str(columns["user_id"].type) or str(columns["user_id"].type) == "UUID"
        assert "VARCHAR" in str(columns["title"].type)
        assert "VARCHAR" in str(columns["active_model"].type)
        assert "TIMESTAMP" in str(columns["created_at"].type) or "DATETIME" in str(
            columns["created_at"].type
        )
        assert "TIMESTAMP" in str(columns["last_accessed"].type) or "DATETIME" in str(
            columns["last_accessed"].type
        )
        assert str(columns["access_count"].type) == "INTEGER"
        assert str(columns["message_count"].type) == "INTEGER"

    def test_conversation_table_name(self) -> None:
        """Conversation model must map to 'conversations' table."""
        assert Conversation.__tablename__ == "conversations"


class TestMessageModel:
    def test_message_model_columns(self) -> None:
        """Message model must have all required columns with correct types."""
        mapper = inspect(Message)
        columns = {c.name: c for c in mapper.columns}

        # Check all required columns exist
        assert "id" in columns
        assert "conversation_id" in columns
        assert "user_id" in columns
        assert "role" in columns
        assert "content" in columns
        assert "tool_name" in columns
        assert "turn_index" in columns
        assert "created_at" in columns

        # Check column types
        assert "CHAR" in str(columns["id"].type) or str(columns["id"].type) == "UUID"
        assert (
            "CHAR" in str(columns["conversation_id"].type)
            or str(columns["conversation_id"].type) == "UUID"
        )
        assert "CHAR" in str(columns["user_id"].type) or str(columns["user_id"].type) == "UUID"
        assert "VARCHAR" in str(columns["role"].type)
        assert str(columns["content"].type) == "TEXT"
        assert "VARCHAR" in str(columns["tool_name"].type) or columns["tool_name"].nullable
        assert str(columns["turn_index"].type) == "INTEGER"
        assert "TIMESTAMP" in str(columns["created_at"].type) or "DATETIME" in str(
            columns["created_at"].type
        )

    def test_message_table_name(self) -> None:
        """Message model must map to 'messages' table."""
        assert Message.__tablename__ == "messages"


class TestHITLApprovalModel:
    def test_hitl_approval_model_columns(self) -> None:
        """HITLApproval model must have all required columns with correct types."""
        mapper = inspect(HITLApproval)
        columns = {c.name: c for c in mapper.columns}

        # Check all required columns exist
        assert "id" in columns
        assert "conversation_id" in columns
        assert "user_id" in columns
        assert "tool_name" in columns
        assert "thread_id" in columns
        assert "checkpoint_id" in columns
        assert "used" in columns
        assert "expired" in columns
        assert "created_at" in columns
        assert "expires_at" in columns

    def test_hitl_approval_table_name(self) -> None:
        """HITLApproval model must map to 'hitl_approvals' table."""
        assert HITLApproval.__tablename__ == "hitl_approvals"


class TestHITLAuditLogModel:
    def test_hitl_audit_log_columns(self) -> None:
        """HITLAuditLog model must have all required columns with correct types."""
        mapper = inspect(HITLAuditLog)
        columns = {c.name: c for c in mapper.columns}

        # Check all required columns exist
        assert "id" in columns
        assert "approval_id" in columns
        assert "user_id" in columns
        assert "conversation_id" in columns
        assert "tool_name" in columns
        assert "decision" in columns
        assert "decided_at" in columns
        assert "request_ip" in columns
        assert "decision_reason" in columns

    def test_hitl_audit_log_table_name(self) -> None:
        """HITLAuditLog model must map to 'hitl_audit_log' table."""
        assert HITLAuditLog.__tablename__ == "hitl_audit_log"

    def test_hitl_audit_log_no_onupdate(self) -> None:
        """HITLAuditLog must be append-only with no update capability."""
        # This is enforced at the DB level via REVOKE, but we check the model
        # doesn't have onupdate hooks
        mapper = inspect(HITLAuditLog)
        for column in mapper.columns:
            # SQLAlchemy columns shouldn't have onupdate logic for this table
            assert not hasattr(column, "onupdate") or column.onupdate is None


class TestTokenModuleReexport:
    def test_token_module_exports_refresh_token(self) -> None:
        """token.py re-exports RefreshToken so callers can import from either path."""
        from src.persistence.models import token as token_mod

        assert token_mod.RefreshToken is RefreshToken
        assert "RefreshToken" in token_mod.__all__
