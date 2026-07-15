import pytest

from idp.domain.states import BatchItemState, BatchState, JobState
from idp.services.state_machine import (
    InvalidStateTransition,
    require_batch_transition,
    require_item_transition,
    require_terminal_job_state,
)


def test_running_batch_can_pause_for_capacity() -> None:
    require_batch_transition(BatchState.RUNNING, BatchState.PAUSED_CAPACITY)


def test_completed_batch_cannot_be_reopened() -> None:
    with pytest.raises(InvalidStateTransition, match="completed -> running"):
        require_batch_transition(BatchState.COMPLETED, BatchState.RUNNING)


def test_running_item_can_be_quarantined() -> None:
    require_item_transition(BatchItemState.RUNNING, BatchItemState.QUARANTINED)


def test_job_completion_rejects_nonterminal_state() -> None:
    with pytest.raises(InvalidStateTransition, match="requires terminal state"):
        require_terminal_job_state(JobState.RUNNING)
