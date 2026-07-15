import importlib

import pytest
from prometheus_client import CollectorRegistry

import idp.metrics as metrics


def _sample_value(
    registry: CollectorRegistry, name: str, labels: dict[str, str] | None = None
) -> float:
    value = registry.get_sample_value(name, labels)
    assert value is not None
    return value


def test_metrics_facade_records_labeled_events_and_stage_duration() -> None:
    registry = CollectorRegistry()
    facade = metrics.ControllerMetrics(registry)

    facade.observe_recovery(2)
    facade.record_retry(stage="render", reason="timeout")
    facade.record_quarantine(stage="render", reason="max_attempts")
    facade.record_cache_reuse(stage="publication")
    facade.observe_stage_duration(stage="render", outcome="succeeded", seconds=2.5)

    assert _sample_value(registry, "idp_leases_recovered_total") == 2
    assert _sample_value(
        registry, "idp_stage_retries_total", {"stage": "render", "reason": "timeout"}
    ) == 1
    assert _sample_value(
        registry,
        "idp_items_quarantined_total",
        {"stage": "render", "reason": "max_attempts"},
    ) == 1
    assert _sample_value(registry, "idp_cache_reuses_total", {"stage": "publication"}) == 1
    assert _sample_value(
        registry, "idp_stage_duration_seconds_count", {"stage": "render", "outcome": "succeeded"}
    ) == 1
    assert _sample_value(
        registry, "idp_stage_duration_seconds_sum", {"stage": "render", "outcome": "succeeded"}
    ) == pytest.approx(2.5)


def test_metrics_facade_sets_labeled_current_state() -> None:
    registry = CollectorRegistry()
    facade = metrics.ControllerMetrics(registry)

    facade.set_queue_depth(stage="layout", state="pending", depth=7)
    facade.set_quality_count(quality="warning", count=3)
    facade.set_active_leases(stage="ocr", count=2)
    facade.set_capacity_reservation(resource="gpu1", unit="slots", amount=1)
    facade.set_gpu_vram_bytes(gpu="gpu1", used_bytes=6 * 1024**3)
    facade.set_storage_free_bytes(12 * 1024**3)
    facade.mark_capacity_paused()
    facade.clear_capacity_paused()

    assert _sample_value(
        registry, "idp_queue_depth", {"stage": "layout", "state": "pending"}
    ) == 7
    assert _sample_value(registry, "idp_quality_items", {"quality": "warning"}) == 3
    assert _sample_value(registry, "idp_active_leases", {"stage": "ocr"}) == 2
    assert _sample_value(
        registry, "idp_capacity_reserved", {"resource": "gpu1", "unit": "slots"}
    ) == 1
    assert _sample_value(registry, "idp_gpu_vram_bytes", {"gpu": "gpu1"}) == 6 * 1024**3
    assert _sample_value(registry, "idp_batches_paused_capacity") == 0
    assert _sample_value(registry, "idp_storage_free_bytes") == 12 * 1024**3


def test_reloading_metrics_module_reuses_default_registry_collectors() -> None:
    reloaded = importlib.reload(metrics)

    reloaded.ControllerMetrics()


def test_metrics_reconciliation_clears_absent_durable_labels() -> None:
    registry = CollectorRegistry()
    facade = metrics.ControllerMetrics(registry)
    facade.reconcile_durable_state(
        queue_depth={("ocr", "pending"): 2},
        active_leases={("ocr",): 1},
        reservations={("gpu1", "role"): 1},
        quality={("pass",): 3},
    )
    facade.reconcile_durable_state(
        queue_depth={}, active_leases={}, reservations={}, quality={}
    )

    assert _sample_value(registry, "idp_queue_depth", {"stage": "ocr", "state": "pending"}) == 0
    assert _sample_value(registry, "idp_active_leases", {"stage": "ocr"}) == 0
    assert _sample_value(registry, "idp_capacity_reserved", {"resource": "gpu1", "unit": "role"}) == 0
    assert _sample_value(registry, "idp_quality_items", {"quality": "pass"}) == 0
