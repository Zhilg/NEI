"""Prometheus metrics for control-plane recovery and safety events."""

from __future__ import annotations

from prometheus_client import Counter, Gauge

LEASES_RECOVERED = Counter(
    "idp_leases_recovered_total",
    "Jobs returned to the queue after expired worker leases.",
)
CAPACITY_PAUSES = Gauge(
    "idp_batches_paused_capacity",
    "Batches currently paused because a bounded resource cannot be admitted.",
)
QUARANTINES = Counter(
    "idp_items_quarantined_total",
    "Document items quarantined after terminal technical failures.",
)


class ControllerMetrics:
    """Thin metric facade to keep controller code exporter-independent."""

    def observe_recovery(self, recovered_jobs: int) -> None:
        """Record a lease recovery sweep only when it did useful work."""
        if recovered_jobs:
            LEASES_RECOVERED.inc(recovered_jobs)

    def mark_capacity_paused(self) -> None:
        """Expose that controller admission stopped dispatching new heavy work."""
        CAPACITY_PAUSES.set(1)

    def clear_capacity_paused(self) -> None:
        """Clear capacity state after an explicit successful recovery check."""
        CAPACITY_PAUSES.set(0)
