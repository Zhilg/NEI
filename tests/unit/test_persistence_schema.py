from idp.persistence import models  # noqa: F401
from idp.persistence.base import Base


def test_phase_two_control_plane_tables_are_registered() -> None:
    required = {
        "pipeline_profiles",
        "resource_pools",
        "batches",
        "batch_roots",
        "documents",
        "batch_items",
        "jobs",
        "stage_runs",
        "resource_reservations",
        "artifacts",
        "entity_results",
        "audit_samples",
        "events",
    }

    assert required.issubset(Base.metadata.tables)
