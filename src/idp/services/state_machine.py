"""Pure lifecycle checks that prevent terminal-state rewrites."""

from __future__ import annotations

from idp.domain.states import BatchItemState, BatchState, JobState, TERMINAL_JOB_STATES


class InvalidStateTransition(ValueError):
    """Raised for a lifecycle transition that bypasses controller rules."""


_BATCH_TRANSITIONS: dict[BatchState, frozenset[BatchState]] = {
    BatchState.QUEUED: frozenset(
        {
            BatchState.RUNNING,
            BatchState.COMPLETED,
            BatchState.COMPLETED_WITH_WARNINGS,
            BatchState.COMPLETED_WITH_ERRORS,
            BatchState.CANCELLED,
        }
    ),
    BatchState.RUNNING: frozenset(
        {
            BatchState.PAUSED_CAPACITY,
            BatchState.COMPLETED,
            BatchState.COMPLETED_WITH_WARNINGS,
            BatchState.COMPLETED_WITH_ERRORS,
            BatchState.CANCELLED,
        }
    ),
    BatchState.PAUSED_CAPACITY: frozenset({BatchState.RUNNING, BatchState.CANCELLED}),
    BatchState.COMPLETED: frozenset(),
    BatchState.COMPLETED_WITH_WARNINGS: frozenset(),
    BatchState.COMPLETED_WITH_ERRORS: frozenset({BatchState.RUNNING}),
    BatchState.CANCELLED: frozenset(),
}

_ITEM_TRANSITIONS: dict[BatchItemState, frozenset[BatchItemState]] = {
    BatchItemState.DISCOVERED: frozenset(
        {
            BatchItemState.QUEUED,
            BatchItemState.REUSED,
            BatchItemState.SKIPPED_UNSUPPORTED,
            BatchItemState.SKIPPED_UNSTABLE,
            BatchItemState.SKIPPED_SYMLINK,
            BatchItemState.CANCELLED,
        }
    ),
    BatchItemState.QUEUED: frozenset(
        {BatchItemState.RUNNING, BatchItemState.QUARANTINED, BatchItemState.CANCELLED}
    ),
    BatchItemState.RUNNING: frozenset(
        {
            BatchItemState.QUEUED,
            BatchItemState.COMPLETED,
            BatchItemState.COMPLETED_WITH_WARNINGS,
            BatchItemState.QUARANTINED,
            BatchItemState.CANCELLED,
        }
    ),
    BatchItemState.REUSED: frozenset(),
    BatchItemState.COMPLETED: frozenset(),
    BatchItemState.COMPLETED_WITH_WARNINGS: frozenset(),
    BatchItemState.QUARANTINED: frozenset({BatchItemState.QUEUED}),
    BatchItemState.SKIPPED_UNSUPPORTED: frozenset(),
    BatchItemState.SKIPPED_UNSTABLE: frozenset(),
    BatchItemState.SKIPPED_SYMLINK: frozenset(),
    BatchItemState.CANCELLED: frozenset(),
}


def require_batch_transition(current: BatchState, target: BatchState) -> None:
    """Validate batch state change before it is committed."""
    if target not in _BATCH_TRANSITIONS[current]:
        msg = f"invalid batch transition: {current} -> {target}"
        raise InvalidStateTransition(msg)


def require_item_transition(current: BatchItemState, target: BatchItemState) -> None:
    """Validate batch-item state change before it is committed."""
    if target not in _ITEM_TRANSITIONS[current]:
        msg = f"invalid batch item transition: {current} -> {target}"
        raise InvalidStateTransition(msg)


def require_terminal_job_state(state: JobState) -> None:
    """Prevent a worker from completing a job as a nonterminal state."""
    if state not in TERMINAL_JOB_STATES:
        msg = f"job completion requires terminal state, got: {state}"
        raise InvalidStateTransition(msg)
