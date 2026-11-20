"""photo_1..photo_3 на черновиках визита.

Revision ID: 0070_visit_draft_photos
Revises: 0069_master_schedule_audit_batch
Create Date: 2026-11-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0070_visit_draft_photos"
down_revision = "0069_master_schedule_audit_batch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("visit_drafts", sa.Column("photo_1", sa.String(length=300), nullable=True))
    op.add_column("visit_drafts", sa.Column("photo_2", sa.String(length=300), nullable=True))
    op.add_column("visit_drafts", sa.Column("photo_3", sa.String(length=300), nullable=True))


def downgrade() -> None:
    op.drop_column("visit_drafts", "photo_3")
    op.drop_column("visit_drafts", "photo_2")
    op.drop_column("visit_drafts", "photo_1")
