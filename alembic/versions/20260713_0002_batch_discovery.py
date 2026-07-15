"""Add durable scan snapshot, reuse, and cancellation metadata.

Revision ID: 20260713_0002
Revises: 20260713_0001
Create Date: 2026-07-13 11:15:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260713_0002"
down_revision: str | None = "20260713_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE batch_items DROP CONSTRAINT IF EXISTS batch_item_state")
    op.execute(
        "ALTER TABLE batch_items ADD CONSTRAINT batch_item_state CHECK "
        "(state IN ('discovered','queued','running','reused','completed',"
        "'completed_with_warnings','quarantined','skipped_unsupported',"
        "'skipped_unstable','skipped_symlink','cancelled'))"
    )
    op.add_column("batch_items", sa.Column("scan_reason", sa.Text(), nullable=True))
    op.add_column("batch_items", sa.Column("source_size_bytes", sa.BigInteger(), nullable=True))
    op.add_column("batch_items", sa.Column("source_mtime_ns", sa.BigInteger(), nullable=True))
    op.add_column("batch_items", sa.Column("source_device", sa.BigInteger(), nullable=True))
    op.add_column("batch_items", sa.Column("source_inode", sa.BigInteger(), nullable=True))
    op.add_column("batch_items", sa.Column("source_object_key", sa.Text(), nullable=True))
    op.add_column("batch_items", sa.Column("source_object_key", sa.Text(), nullable=True))
    op.add_column("batch_items", sa.Column("reused_from_item_id", sa.Uuid(), nullable=True))
    op.add_column(
        "batch_items", sa.Column("cancellation_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_batch_items_reused_from_item_id_batch_items",
        "batch_items",
        "batch_items",
        ["reused_from_item_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.execute("ALTER TABLE batch_items DROP CONSTRAINT IF EXISTS batch_item_state")
    op.execute(
        "ALTER TABLE batch_items ADD CONSTRAINT batch_item_state CHECK "
        "(state IN ('discovered','queued','running','reused','completed',"
        "'completed_with_warnings','quarantined','skipped_unsupported',"
        "'skipped_unstable','skipped_symlink'))"
    )
    op.drop_constraint("fk_batch_items_reused_from_item_id_batch_items", "batch_items", type_="foreignkey")
    op.drop_column("batch_items", "cancellation_requested_at")
    op.drop_column("batch_items", "reused_from_item_id")
    op.drop_column("batch_items", "source_inode")
    op.drop_column("batch_items", "source_object_key")
    op.drop_column("batch_items", "source_object_key")
    op.drop_column("batch_items", "source_device")
    op.drop_column("batch_items", "source_mtime_ns")
    op.drop_column("batch_items", "source_size_bytes")
    op.drop_column("batch_items", "scan_reason")
