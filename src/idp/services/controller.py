"""Controller admission and recovery helpers without stage model handlers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from idp.domain.models import JobClaim, ResourceRequest
from idp.persistence.repository import ResourceCapacityError
from idp.ports.batch_repository import BatchRepository


@dataclass(frozen=True)
class ControllerTick:
    """One visible result from a controller scheduling iteration."""

    recovered_jobs: int
    claimed_job: JobClaim | None
    paused_for_capacity: bool


class Controller:
    """Coordinates lease recovery and resource admission around a durable queue."""

    def __init__(
        self,
        repository: BatchRepository,
        *,
        worker_id: str,
        lease_duration: timedelta,
    ) -> None:
        self._repository = repository
        self._worker_id = worker_id
        self._lease_duration = lease_duration

    def recover_expired_leases(self) -> int:
        """Perform the always-safe reaper duty without touching model stages."""
        return self._repository.requeue_expired_jobs()

    def tick(self, requests: tuple[ResourceRequest, ...]) -> ControllerTick:
        """Recover, claim one job, and admit it to a bounded resource envelope."""
        recovered_jobs = self.recover_expired_leases()
        claim = self._repository.claim_next_job(
            worker_id=self._worker_id,
            lease_duration=self._lease_duration,
        )
        if claim is None:
            return ControllerTick(recovered_jobs, None, False)
        try:
            self._repository.reserve_resources(
                job_id=claim.job_id,
                owner=self._worker_id,
                requests=requests,
                lease_duration=self._lease_duration,
            )
        except ResourceCapacityError:
            self._repository.defer_job_for_capacity(
                job_id=claim.job_id,
                worker_id=self._worker_id,
                error_detail="resource pool capacity is unavailable",
            )
            return ControllerTick(recovered_jobs, None, True)
        return ControllerTick(recovered_jobs, claim, False)
