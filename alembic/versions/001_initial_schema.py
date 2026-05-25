"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-05-23 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create all tables from T-007 models."""
    # Create users table
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )

    # Create refresh_tokens table
    op.create_table(
        "refresh_tokens",
        sa.Column("jti", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_refresh_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("jti", name=op.f("pk_refresh_tokens")),
    )
    op.create_index(
        op.f("ix_refresh_tokens_user_id"),
        "refresh_tokens",
        ["user_id"],
        unique=False,
    )

    # Create conversations table
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("active_model", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_accessed", sa.DateTime(timezone=True), nullable=False),
        sa.Column("access_count", sa.Integer(), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_conversations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(
        op.f("ix_conversations_user_id_last_accessed"),
        "conversations",
        ["user_id", "last_accessed"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversations_retention"),
        "conversations",
        ["last_accessed", "access_count"],
        unique=False,
        postgresql_where=sa.text("last_accessed < NOW() - INTERVAL '60 days'"),
    )

    # Create messages table
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=True),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'tool')",
            name=op.f("ck_messages_role"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_messages_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
    )
    op.create_index(
        op.f("ix_messages_conversation_id_created_at"),
        "messages",
        ["conversation_id", "created_at"],
        unique=False,
    )

    # Create hitl_approvals table
    op.create_table(
        "hitl_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=255), nullable=False),
        sa.Column("used", sa.Boolean(), nullable=False),
        sa.Column("expired", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_hitl_approvals_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hitl_approvals")),
    )
    op.create_index(
        op.f("ix_hitl_approvals_conversation_id"),
        "hitl_approvals",
        ["conversation_id"],
        unique=False,
    )

    # Create hitl_audit_log table
    op.create_table(
        "hitl_audit_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("decision", sa.String(length=10), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_ip", sa.String(length=45), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "decision IN ('approve', 'deny', 'timeout')",
            name=op.f("ck_hitl_audit_log_decision"),
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["hitl_approvals.id"],
            name=op.f("fk_hitl_audit_log_approval_id_hitl_approvals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hitl_audit_log")),
    )
    op.create_index(
        op.f("ix_hitl_audit_log_approval_id"),
        "hitl_audit_log",
        ["approval_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_hitl_audit_log_user_id_decided_at"),
        "hitl_audit_log",
        ["user_id", "decided_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop all tables."""
    op.drop_index(
        op.f("ix_hitl_audit_log_user_id_decided_at"),
        table_name="hitl_audit_log",
    )
    op.drop_index(
        op.f("ix_hitl_audit_log_approval_id"),
        table_name="hitl_audit_log",
    )
    op.drop_table("hitl_audit_log")

    op.drop_index(
        op.f("ix_hitl_approvals_conversation_id"),
        table_name="hitl_approvals",
    )
    op.drop_table("hitl_approvals")

    op.drop_index(
        op.f("ix_messages_conversation_id_created_at"),
        table_name="messages",
    )
    op.drop_table("messages")

    op.drop_index(
        op.f("ix_conversations_retention"),
        table_name="conversations",
    )
    op.drop_index(
        op.f("ix_conversations_user_id_last_accessed"),
        table_name="conversations",
    )
    op.drop_table("conversations")

    op.drop_index(
        op.f("ix_refresh_tokens_user_id"),
        table_name="refresh_tokens",
    )
    op.drop_table("refresh_tokens")

    op.drop_table("users")
