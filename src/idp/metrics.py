"""Prometheus metrics for control-plane and pipeline observability."""

from __future__ import annotations

from threading import Lock
from typing import cast

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, REGISTRY


_COLLECTOR_CACHE_ATTRIBUTE = "_idp_metrics_collectors_v1"
_collector_lock = Lock()


class _MetricCollectors:
    """Collectors registered together for one Prometheus registry."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self.leases_recovered = Counter(
            "idp_leases_recovered_total",
            "Jobs returned to the queue after expired worker leases.",
            registry=registry,
        )
        self.capacity_pauses = Gauge(
            "idp_batches_paused_capacity",
            "Batches currently paused because a bounded resource cannot be admitted.",
            registry=registry,
        )
        self.queue_depth = Gauge(
            "idp_queue_depth",
            "Current number of jobs in a queue state by stage.",
            ("stage", "state"),
            registry=registry,
        )
        self.stage_duration = Histogram(
            "idp_stage_duration_seconds",
            "Wall-clock duration of completed stage attempts.",
            ("stage", "outcome"),
            registry=registry,
        )
        self.retries = Counter(
            "idp_stage_retries_total",
            "Retryable stage failures scheduled for another attempt.",
            ("stage", "reason"),
            registry=registry,
        )
        self.quarantines = Counter(
            "idp_items_quarantined_total",
            "Document items quarantined after terminal technical failures.",
            ("stage", "reason"),
            registry=registry,
        )
        self.cache_reuses = Counter(
            "idp_cache_reuses_total",
            "Completed work reused from the immutable-result cache.",
            ("stage",),
            registry=registry,
        )
        self.quality_items = Gauge(
            "idp_quality_items",
            "Current published item count by quality outcome.",
            ("quality",),
            registry=registry,
        )
        self.active_leases = Gauge(
            "idp_active_leases",
            "Current active worker leases by stage.",
            ("stage",),
            registry=registry,
        )
        self.capacity_reserved = Gauge(
            "idp_capacity_reserved",
            "Current capacity reserved by resource kind and unit.",
            ("resource", "unit"),
            registry=registry,
        )
        self.gpu_vram_bytes = Gauge(
            "idp_gpu_vram_bytes",
            "Current GPU VRAM used by device in bytes.",
            ("gpu",),
            registry=registry,
        )
        self.storage_free_bytes = Gauge(
            "idp_storage_free_bytes",
            "Free bytes available to the configured artifact storage volume.",
            registry=registry,
        )


def _collectors_for(registry: CollectorRegistry) -> _MetricCollectors:
    """Return registry-scoped collectors without duplicate registration on reload."""
    with _collector_lock:
        collectors = getattr(registry, _COLLECTOR_CACHE_ATTRIBUTE, None)
        if collectors is None:
            collectors = _MetricCollectors(registry)
            setattr(registry, _COLLECTOR_CACHE_ATTRIBUTE, collectors)
        return cast(_MetricCollectors, collectors)


_DEFAULT_COLLECTORS = _collectors_for(REGISTRY)

# Backwards-compatible access to the default registry collectors.
LEASES_RECOVERED = _DEFAULT_COLLECTORS.leases_recovered
CAPACITY_PAUSES = _DEFAULT_COLLECTORS.capacity_pauses
QUEUE_DEPTH = _DEFAULT_COLLECTORS.queue_depth
STAGE_DURATION = _DEFAULT_COLLECTORS.stage_duration
RETRIES = _DEFAULT_COLLECTORS.retries
QUARANTINES = _DEFAULT_COLLECTORS.quarantines
CACHE_REUSES = _DEFAULT_COLLECTORS.cache_reuses
QUALITY_ITEMS = _DEFAULT_COLLECTORS.quality_items
ACTIVE_LEASES = _DEFAULT_COLLECTORS.active_leases
CAPACITY_RESERVED = _DEFAULT_COLLECTORS.capacity_reserved
GPU_VRAM_BYTES = _DEFAULT_COLLECTORS.gpu_vram_bytes
STORAGE_FREE_BYTES = _DEFAULT_COLLECTORS.storage_free_bytes


class ControllerMetrics:
    """Thin exporter-independent facade for controller and worker observations."""

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        self._registry = registry
        self._collectors = _collectors_for(registry)
        self._queue_labels = getattr(registry, "_idp_queue_labels_v1", set())
        self._lease_labels = getattr(registry, "_idp_lease_labels_v1", set())
        self._reservation_labels = getattr(registry, "_idp_reservation_labels_v1", set())
        self._quality_labels = getattr(registry, "_idp_quality_labels_v1", set())

    def observe_recovery(self, recovered_jobs: int) -> None:
        """Record a lease recovery sweep only when it did useful work."""
        if recovered_jobs:
            self._collectors.leases_recovered.inc(recovered_jobs)

    def mark_capacity_paused(self) -> None:
        """Expose that controller admission stopped dispatching new heavy work."""
        self._collectors.capacity_pauses.set(1)

    def clear_capacity_paused(self) -> None:
        """Clear capacity state after an explicit successful recovery check."""
        self._collectors.capacity_pauses.set(0)

    def set_queue_depth(self, *, stage: str, state: str, depth: float) -> None:
        """Set the current queue depth for one stage and durable job state."""
        self._collectors.queue_depth.labels(stage=stage, state=state).set(depth)

    def observe_stage_duration(self, *, stage: str, outcome: str, seconds: float) -> None:
        """Record one completed stage attempt and its final outcome."""
        self._collectors.stage_duration.labels(stage=stage, outcome=outcome).observe(seconds)

    def record_retry(self, *, stage: str, reason: str) -> None:
        """Record a retry scheduled after a retryable stage failure."""
        self._collectors.retries.labels(stage=stage, reason=reason).inc()

    def record_quarantine(self, *, stage: str, reason: str) -> None:
        """Record terminal quarantine of an item after a failed stage."""
        self._collectors.quarantines.labels(stage=stage, reason=reason).inc()

    def record_cache_reuse(self, *, stage: str) -> None:
        """Record reuse of a completed result for a pipeline stage."""
        self._collectors.cache_reuses.labels(stage=stage).inc()

    def set_quality_count(self, *, quality: str, count: float) -> None:
        """Set the current published-item count for one quality outcome."""
        self._collectors.quality_items.labels(quality=quality).set(count)

    def set_active_leases(self, *, stage: str, count: float) -> None:
        """Set the current active worker lease count for one stage."""
        self._collectors.active_leases.labels(stage=stage).set(count)

    def set_capacity_reservation(self, *, resource: str, unit: str, amount: float) -> None:
        """Set the capacity currently reserved for one bounded resource pool."""
        self._collectors.capacity_reserved.labels(resource=resource, unit=unit).set(amount)

    def set_gpu_vram_bytes(self, *, gpu: str, used_bytes: float) -> None:
        """Set the GPU VRAM currently in use for one device."""
        self._collectors.gpu_vram_bytes.labels(gpu=gpu).set(used_bytes)

    def set_storage_free_bytes(self, free_bytes: float) -> None:
        """Set currently available artifact-storage capacity in bytes."""
        self._collectors.storage_free_bytes.set(free_bytes)

    def reconcile_durable_state(
        self,
        *,
        queue_depth: dict[tuple[str, str], int],
        active_leases: dict[tuple[str], int],
        reservations: dict[tuple[str, str], int],
        quality: dict[tuple[str], int],
    ) -> None:
        """Set current aggregates and clear label series absent from a newer snapshot."""
        queue_labels = set(queue_depth)
        for stage, state in self._queue_labels - queue_labels:
            self.set_queue_depth(stage=stage, state=state, depth=0)
        for (stage, state), count in queue_depth.items():
            self.set_queue_depth(stage=stage, state=state, depth=count)
        self._queue_labels = queue_labels
        setattr(self._registry, "_idp_queue_labels_v1", queue_labels)

        lease_labels = {stage for (stage,) in active_leases}
        for stage in self._lease_labels - lease_labels:
            self.set_active_leases(stage=stage, count=0)
        for (stage,), count in active_leases.items():
            self.set_active_leases(stage=stage, count=count)
        self._lease_labels = lease_labels
        setattr(self._registry, "_idp_lease_labels_v1", lease_labels)

        reservation_labels = set(reservations)
        for resource, unit in self._reservation_labels - reservation_labels:
            self.set_capacity_reservation(resource=resource, unit=unit, amount=0)
        for (resource, unit), amount in reservations.items():
            self.set_capacity_reservation(resource=resource, unit=unit, amount=amount)
        self._reservation_labels = reservation_labels
        setattr(self._registry, "_idp_reservation_labels_v1", reservation_labels)

        quality_labels = {quality_value for (quality_value,) in quality}
        for quality_value in self._quality_labels - quality_labels:
            self.set_quality_count(quality=quality_value, count=0)
        for (quality_value,), count in quality.items():
            self.set_quality_count(quality=quality_value, count=count)
        self._quality_labels = quality_labels
        setattr(self._registry, "_idp_quality_labels_v1", quality_labels)
