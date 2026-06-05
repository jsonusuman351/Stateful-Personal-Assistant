"""add is_deleted to conversations (soft delete)

Revision ID: 002
Revises: 001
Create Date: 2026-06-05 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the is_deleted soft-delete flag to conversations.

    server_default=false() backfills existing rows so the NOT NULL constraint
    holds without a separate data-migration step. An index supports the
    ``WHERE is_deleted = false`` filter applied to every list/get query.
    """
    op.add_column(
        "conversations",
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        op.f("ix_conversations_is_deleted"),
        "conversations",
        ["is_deleted"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the is_deleted column and its index."""
    op.drop_index(
        op.f("ix_conversations_is_deleted"),
        table_name="conversations",
    )
    op.drop_column("conversations", "is_deleted")
