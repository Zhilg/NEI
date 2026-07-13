"""Terminal states shared by the controller, reports, and manifests."""

from __future__ import annotations

from enum import StrEnum


class BatchState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED_CAPACITY = "paused_capacity"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    CANCELLED = "cancelled"


class BatchItemState(StrEnum):
    DISCOVERED = "discovered"
    QUEUED = "queued"
    RUNNING = "running"
    REUSED = "reused"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    QUARANTINED = "quarantined"
    SKIPPED_UNSUPPORTED = "skipped_unsupported"
    SKIPPED_UNSTABLE = "skipped_unstable"
    SKIPPED_SYMLINK = "skipped_symlink"


class QualityState(StrEnum):
    PASS = "pass"
    WARNING = "warning"
    FAILED = "failed"


class JobState(StrEnum):
    """Lifecycle of one leased, idempotent stage invocation."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ArtifactRetention(StrEnum):
    """Artifact lifecycle classes enforced by the artifact-store boundary."""

    TEMPORARY = "temporary"
    FINAL = "final"


class ReservationKind(StrEnum):
    """Globally bounded controller resource pools."""

    CPU = "cpu"
    GPU0 = "gpu0"
    GPU1 = "gpu1"
    STORAGE = "storage"


TERMINAL_ITEM_STATES = frozenset(
    {
        BatchItemState.REUSED,
        BatchItemState.COMPLETED,
        BatchItemState.COMPLETED_WITH_WARNINGS,
        BatchItemState.QUARANTINED,
        BatchItemState.SKIPPED_UNSUPPORTED,
        BatchItemState.SKIPPED_UNSTABLE,
        BatchItemState.SKIPPED_SYMLINK,
    }
)

TERMINAL_JOB_STATES = frozenset(
    {
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.CANCELLED,
    }
)
