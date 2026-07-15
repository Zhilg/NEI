"""SQLAlchemy mappings for the PostgreSQL control plane."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    BigInteger,
    Enum as SqlEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from idp.domain.states import (
    ArtifactRetention,
    BatchItemState,
    BatchState,
    JobState,
    QualityState,
    ReservationKind,
)
from idp.persistence.base import Base


def _enum_type(enum_type: type[Any], name: str) -> SqlEnum:
    """Persist StrEnum values, rather than Python member names."""
    return SqlEnum(
        enum_type,
        name=name,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda enum: [member.value for member in enum],
    )


class PipelineProfileModel(Base):
    __tablename__ = "pipeline_profiles"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    configuration: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    batches: Mapped[list[BatchModel]] = relationship(back_populates="profile")

    __table_args__ = (UniqueConstraint("name", "profile_hash", name="uq_pipeline_profiles_name_hash"),)


class ResourcePoolModel(Base):
    __tablename__ = "resource_pools"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[ReservationKind] = mapped_column(
        _enum_type(ReservationKind, "resource_pool_kind"), nullable=False
    )
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    reservations: Mapped[list[ResourceReservationModel]] = relationship(back_populates="pool")

    __table_args__ = (
        UniqueConstraint("kind", "unit", name="uq_resource_pools_kind_unit"),
        CheckConstraint("capacity > 0", name="resource_pool_capacity_positive"),
    )


class BatchModel(Base):
    __tablename__ = "batches"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    profile_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipeline_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[BatchState] = mapped_column(
        _enum_type(BatchState, "batch_state"), nullable=False, default=BatchState.QUEUED
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    profile: Mapped[PipelineProfileModel] = relationship(back_populates="batches")
    roots: Mapped[list[BatchRootModel]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )
    items: Mapped[list[BatchItemModel]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class BatchRootModel(Base):
    __tablename__ = "batch_roots"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    batch_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("batches.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)

    batch: Mapped[BatchModel] = relationship(back_populates="roots")

    __table_args__ = (UniqueConstraint("batch_id", "path", name="uq_batch_roots_batch_path"),)


class DocumentModel(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    batch_items: Mapped[list[BatchItemModel]] = relationship(back_populates="document")


class BatchItemModel(Base):
    __tablename__ = "batch_items"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    batch_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("batches.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True
    )
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    scan_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_mtime_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_device: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_inode: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    reused_from_item_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("batch_items.id", ondelete="RESTRICT"), nullable=True
    )
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[BatchItemState] = mapped_column(
        _enum_type(BatchItemState, "batch_item_state"), nullable=False
    )
    quality: Mapped[QualityState | None] = mapped_column(
        _enum_type(QualityState, "quality_state"), nullable=True
    )
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_bundle_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_manifest_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    batch: Mapped[BatchModel] = relationship(back_populates="items")
    document: Mapped[DocumentModel | None] = relationship(back_populates="batch_items")
    jobs: Mapped[list[JobModel]] = relationship(
        back_populates="batch_item", cascade="all, delete-orphan"
    )
    entity_results: Mapped[list[EntityResultModel]] = relationship(
        back_populates="batch_item", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("batch_id", "source_path", name="uq_batch_items_batch_path"),)


class JobModel(Base):
    __tablename__ = "jobs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    batch_item_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("batch_items.id", ondelete="CASCADE"), nullable=False
    )
    depends_on_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=True
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    state: Mapped[JobState] = mapped_column(
        _enum_type(JobState, "job_state"), nullable=False, default=JobState.PENDING
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    batch_item: Mapped[BatchItemModel] = relationship(back_populates="jobs")
    dependency: Mapped[JobModel | None] = relationship(remote_side="JobModel.id")
    stage_runs: Mapped[list[StageRunModel]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    reservations: Mapped[list[ResourceReservationModel]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    artifacts: Mapped[list[ArtifactModel]] = relationship(back_populates="producing_job")

    __table_args__ = (
        CheckConstraint("max_attempts > 0", name="max_attempts_positive"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        Index("ix_jobs_claim", "state", "priority", "created_at"),
    )


class StageRunModel(Base):
    __tablename__ = "stage_runs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(255), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    job: Mapped[JobModel] = relationship(back_populates="stage_runs")

    __table_args__ = (UniqueConstraint("job_id", "attempt", name="uq_stage_runs_job_attempt"),)


class ResourceReservationModel(Base):
    __tablename__ = "resource_reservations"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    pool_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resource_pools.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[ReservationKind] = mapped_column(
        _enum_type(ReservationKind, "reservation_kind"), nullable=False
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[JobModel] = relationship(back_populates="reservations")
    pool: Mapped[ResourcePoolModel] = relationship(back_populates="reservations")

    __table_args__ = (CheckConstraint("amount > 0", name="reservation_amount_positive"),)


class ArtifactModel(Base):
    __tablename__ = "artifacts"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    producing_job_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    object_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    retention: Mapped[ArtifactRetention] = mapped_column(
        _enum_type(ArtifactRetention, "artifact_retention"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    producing_job: Mapped[JobModel | None] = relationship(back_populates="artifacts")

    __table_args__ = (CheckConstraint("size_bytes >= 0", name="artifact_size_nonnegative"),)


class EntityResultModel(Base):
    __tablename__ = "entity_results"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    batch_item_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("batch_items.id", ondelete="CASCADE"), nullable=False
    )
    schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    batch_item: Mapped[BatchItemModel] = relationship(back_populates="entity_results")


class AuditSampleModel(Base):
    __tablename__ = "audit_samples"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    batch_item_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("batch_items.id", ondelete="CASCADE"), nullable=False
    )
    sample_seed: Mapped[str] = mapped_column(String(128), nullable=False)
    selected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    review_status: Mapped[str] = mapped_column(String(64), nullable=False, default="pending")

    __table_args__ = (UniqueConstraint("batch_item_id", name="uq_audit_samples_item"),)


class EventModel(Base):
    __tablename__ = "events"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    batch_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("batches.id", ondelete="CASCADE"), nullable=True
    )
    batch_item_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("batch_items.id", ondelete="CASCADE"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
