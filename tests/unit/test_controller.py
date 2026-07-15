from datetime import UTC, datetime, timedelta
from uuid import uuid4

from idp.domain.models import JobClaim, ResourceRequest
from idp.domain.states import ReservationKind
from idp.persistence.repository import ResourceCapacityError
from idp.services.controller import Controller


class CapacityConstrainedRepository:
    def __init__(self) -> None:
        self.job_id = uuid4()
        self.deferred: list[dict[str, object]] = []

    def requeue_expired_jobs(self) -> int:
        return 2

    def claim_next_job(self, *, worker_id: str, lease_duration: timedelta) -> JobClaim:
        return JobClaim(
            job_id=self.job_id,
            batch_item_id=uuid4(),
            stage="render",
            attempt=1,
            payload={},
            created_at=datetime.now(UTC),
            lease_owner=worker_id,
            lease_expires_at=datetime.now(UTC) + lease_duration,
        )

    def reserve_resources(self, **_: object) -> None:
        raise ResourceCapacityError("storage capacity unavailable")

    def defer_job_for_capacity(self, **kwargs: object) -> None:
        self.deferred.append(kwargs)


def test_controller_defers_claim_without_holding_lease_when_capacity_is_full() -> None:
    repository = CapacityConstrainedRepository()
    controller = Controller(
        repository,  # type: ignore[arg-type]
        worker_id="controller-a",
        lease_duration=timedelta(minutes=1),
    )

    result = controller.tick((ResourceRequest(kind=ReservationKind.STORAGE, amount=1, unit="bytes"),))

    assert result.recovered_jobs == 2
    assert result.claimed_job is None
    assert result.paused_for_capacity
    assert repository.deferred[0]["job_id"] == repository.job_id
