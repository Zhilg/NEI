"""Create the durable controller control plane.

Revision ID: 20260713_0001
Revises:
Create Date: 2026-07-13 10:15:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260713_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    batch_state = _enum(
        "batch_state",
        "queued",
        "running",
        "paused_capacity",
        "completed",
        "completed_with_warnings",
        "completed_with_errors",
        "cancelled",
    )
    item_state = _enum(
        "batch_item_state",
        "discovered",
        "queued",
        "running",
        "reused",
        "completed",
        "completed_with_warnings",
        "quarantined",
        "skipped_unsupported",
        "skipped_unstable",
        "skipped_symlink",
    )
    job_state = _enum("job_state", "pending", "running", "succeeded", "failed", "cancelled")
    quality_state = _enum("quality_state", "pass", "warning", "failed")
    reservation_kind = _enum("reservation_kind", "cpu", "gpu0", "gpu1", "storage")
    pool_kind = _enum("resource_pool_kind", "cpu", "gpu0", "gpu1", "storage")
    retention = _enum("artifact_retention", "temporary", "final")

    op.create_table(
        "pipeline_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("profile_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.UniqueConstraint("name", "profile_hash", name="uq_pipeline_profiles_name_hash"),
    )
    op.create_table(
        "resource_pools",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", pool_kind, nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("capacity > 0", name="ck_resource_pools_resource_pool_capacity_positive"),
        sa.UniqueConstraint("kind", "unit", name="uq_resource_pools_kind_unit"),
    )
    op.create_table(
        "batches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("state", batch_state, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["pipeline_profiles.id"], ondelete="RESTRICT"),
    )
    op.create_table(
        "batch_roots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["batches.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("batch_id", "path", name="uq_batch_roots_batch_path"),
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_table(
        "batch_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("state", item_state, nullable=False),
        sa.Column("quality", quality_state, nullable=True),
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
        sa.Column("final_bundle_prefix", sa.Text(), nullable=True),
        sa.Column("final_manifest_key", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("batch_id", "source_path", name="uq_batch_items_batch_path"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("batch_item_id", sa.Uuid(), nullable=False),
        sa.Column("depends_on_id", sa.Uuid(), nullable=True),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False, unique=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", job_state, nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("max_attempts > 0", name="ck_jobs_max_attempts_positive"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_jobs_attempt_count_nonnegative"),
        sa.ForeignKeyConstraint(["batch_item_id"], ["batch_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["depends_on_id"], ["jobs.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_jobs_claim", "jobs", ["state", "priority", "created_at"])
    op.create_table(
        "stage_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("job_id", "attempt", name="uq_stage_runs_job_attempt"),
    )
    op.create_table(
        "resource_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("pool_id", sa.Uuid(), nullable=False),
        sa.Column("kind", reservation_kind, nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("owner", sa.String(255), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount > 0", name="ck_resource_reservations_reservation_amount_positive"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pool_id"], ["resource_pools.id"], ondelete="RESTRICT"),
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("producing_job_id", sa.Uuid(), nullable=True),
        sa.Column("object_key", sa.Text(), nullable=False, unique=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("media_type", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("retention", retention, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("size_bytes >= 0", name="ck_artifacts_artifact_size_nonnegative"),
        sa.ForeignKeyConstraint(["producing_job_id"], ["jobs.id"], ondelete="SET NULL"),
    )
    op.create_table(
        "entity_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("batch_item_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["batch_item_id"], ["batch_items.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "audit_samples",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("batch_item_id", sa.Uuid(), nullable=False),
        sa.Column("sample_seed", sa.String(128), nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("review_status", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["batch_item_id"], ["batch_items.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("batch_item_id", name="uq_audit_samples_item"),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("batch_id", sa.Uuid(), nullable=True),
        sa.Column("batch_item_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["batch_item_id"], ["batch_items.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("events")
    op.drop_table("audit_samples")
    op.drop_table("entity_results")
    op.drop_table("artifacts")
    op.drop_table("resource_reservations")
    op.drop_table("stage_runs")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("batch_items")
    op.drop_table("documents")
    op.drop_table("batch_roots")
    op.drop_table("batches")
    op.drop_table("resource_pools")
    op.drop_table("pipeline_profiles")
