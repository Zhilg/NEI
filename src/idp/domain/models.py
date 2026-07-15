"""Typed records used before persistence adapters are implemented."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from idp.domain.states import (
    ArtifactRetention,
    BatchItemState,
    BatchState,
    JobState,
    QualityState,
    ReservationKind,
)


class ArtifactReference(BaseModel):
    """Immutable object-store artifact identity."""

    model_config = ConfigDict(frozen=True)

    object_key: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    media_type: str = Field(min_length=1)


class Entity(BaseModel):
    """Schema-v1 entity contract emitted after Fenic extraction."""

    model_config = ConfigDict(frozen=True)

    type: str = Field(min_length=1)
    value: str = Field(min_length=1)
    normalized_value: str | None = None
    page: int = Field(ge=0)
    block_id: str = Field(min_length=1)
    bbox: tuple[float, float, float, float]
    evidence: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class FinalManifest(BaseModel):
    """Versioned final bundle contract, independent of the object-store adapter."""

    model_config = ConfigDict(frozen=True)

    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    pipeline_profile_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    quality: QualityState
    final_markdown: ArtifactReference
    entities: ArtifactReference
    schema_version: str = Field(default="entity-v1", min_length=1)
    reconstruction: ArtifactReference | None = None
    model_versions: dict[str, str] = Field(default_factory=dict)
    findings: tuple[dict[str, object], ...] = ()
    evidence_coverage: float = Field(default=1.0, ge=0, le=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BatchItemSnapshot(BaseModel):
    """One file occurrence in an immutable batch scan snapshot."""

    model_config = ConfigDict(frozen=True)

    item_id: UUID = Field(default_factory=uuid4)
    root: Path
    path: Path
    source_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    state: BatchItemState
    reason: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    mtime_ns: int | None = Field(default=None, ge=0)
    device: int | None = Field(default=None, ge=0)
    inode: int | None = Field(default=None, ge=0)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BatchSnapshot(BaseModel):
    """In-memory representation of a submitted batch before persistence."""

    model_config = ConfigDict(frozen=True)

    batch_id: UUID = Field(default_factory=uuid4)
    state: BatchState = BatchState.QUEUED
    profile_name: str = Field(min_length=1)
    roots: tuple[Path, ...]
    items: tuple[BatchItemSnapshot, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StoredArtifact(BaseModel):
    """Verified immutable artifact metadata returned by an object store."""

    model_config = ConfigDict(frozen=True)

    reference: ArtifactReference
    size_bytes: int = Field(ge=0)
    retention: ArtifactRetention


class ResourceRequest(BaseModel):
    """One bounded controller resource needed before a stage starts."""

    model_config = ConfigDict(frozen=True)

    kind: ReservationKind
    amount: int = Field(gt=0)
    unit: str = Field(min_length=1, max_length=32)


class JobClaim(BaseModel):
    """An owned lease returned by the durable PostgreSQL job queue."""

    model_config = ConfigDict(frozen=True)

    job_id: UUID
    batch_item_id: UUID
    stage: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    payload: dict[str, object]
    created_at: datetime
    state: JobState = JobState.RUNNING
    lease_owner: str = Field(min_length=1)
    lease_expires_at: datetime
