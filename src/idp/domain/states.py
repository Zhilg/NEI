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
