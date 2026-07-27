"""Process factories for the durable controller foundation."""

from __future__ import annotations

import logging
import json
import shutil
import threading
import time
import hashlib
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from prometheus_client import start_http_server
from minio import Minio
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from idp.config import Settings
from idp.metrics import ControllerMetrics
from idp.persistence.repository import LeaseOwnershipError, ResourceCapacityError, SqlAlchemyBatchRepository
from idp.domain.models import ArtifactReference, JobClaim
from idp.domain.states import JobState, QualityState, ReservationKind
from idp.services.entities import (
    EntityStageHandler,
    EntityExtractionError,
    EntityExtractor,
    LocalOpenAIQwen3Client,
    entity_manifest_from_payload,
)
from idp.services.mineru import (
    CommandMinerURunner,
    LayoutAdapter,
    MinerUError,
    MinerUStageHandler,
    layout_manifest_from_payload,
)
from idp.services.ocr import (
    OcrError,
    OcrStageHandler,
    ocr_manifest_from_payload,
)
from idp.services.publication import FinalBundlePublisher
from idp.services.qwen_vl import (
    LocalOpenAIQwenVLClient,
    QwenVLStageHandler,
    ReconstructionAssembler,
    ReconstructionError,
    reconstruction_manifest_from_payload,
)
from idp.services.controller import Controller
from idp.services.batches import BatchService
from idp.services.vision import (
    CommandDocxConverter,
    ImageQualityGate,
    PyMuPdfRenderer,
    RenderLimits,
    VisionPreparation,
    VisionPreparationError,
    VisionStageHandler,
    vision_manifest_from_payload,
)
from idp.storage import LocalArtifactStore, MinioArtifactStore

LOGGER = logging.getLogger(__name__)


def create_session_factory(settings: Settings) -> sessionmaker[Session]:
    """Create the synchronous session factory used by controller processes."""
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_batch_service(settings: Settings) -> BatchService:
    """Construct the safe batch API using PostgreSQL and target artifact storage."""
    repository = SqlAlchemyBatchRepository(create_session_factory(settings))
    if settings.minio_access_key and settings.minio_secret_key:
        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        artifacts = MinioArtifactStore(client, settings.minio_bucket)
    else:
        artifacts = LocalArtifactStore(settings.data_root / "local-artifacts")
    return BatchService(settings, repository, artifacts, settings.batch_staging_root)


def register_default_profile(settings: Settings, *, name: str = "default") -> str:
    """Register a stable profile derived from mounted runtime settings for first-run Compose use."""
    payload = {
        "pipeline_profile_version": settings.pipeline_profile_version,
        "render_dpi": settings.render_dpi,
        "render_max_pages": settings.render_max_pages,
        "render_max_pixels_per_page": settings.render_max_pixels_per_page,
        "render_max_total_pixels": settings.render_max_total_pixels,
        "mineru_command": settings.mineru_command,
        "docx_converter_command": settings.docx_converter_command,
        "qwen_vl_model_id": settings.qwen_vl_model_id,
        "qwen_vl_model_revision": settings.qwen_vl_model_revision,
        "qwen_vl_prompt_version": settings.qwen_vl_prompt_version,
        "qwen3_model_id": settings.qwen3_model_id,
        "qwen3_model_revision": settings.qwen3_model_revision,
    }
    profile_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    SqlAlchemyBatchRepository(create_session_factory(settings)).register_profile(
        name=name, profile_hash=profile_hash
    )
    return profile_hash


def create_artifact_store(settings: Settings) -> LocalArtifactStore | MinioArtifactStore:
    """Create one verified artifact adapter shared by batch and stage workers."""
    if settings.minio_access_key and settings.minio_secret_key:
        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        return MinioArtifactStore(client, settings.minio_bucket)
    return LocalArtifactStore(settings.data_root / "local-artifacts")


def run_controller(settings: Settings) -> None:
    """Run the safe phase-two reaper loop until the service is stopped."""
    repository = SqlAlchemyBatchRepository(create_session_factory(settings))
    controller = Controller(
        repository,
        worker_id="controller-reaper",
        lease_duration=timedelta(seconds=settings.controller_poll_seconds * 3),
    )
    metrics = ControllerMetrics()
    start_http_server(settings.metrics_port)
    LOGGER.info("controller started; metrics_port=%s", settings.metrics_port)
    while True:
        metrics.observe_recovery(controller.recover_expired_leases())
        if repository.resume_capacity_paused_batches():
            metrics.clear_capacity_paused()
        _collect_repository_metrics(repository, metrics)
        metrics.set_storage_free_bytes(shutil.disk_usage(settings.data_root).free)
        time.sleep(settings.controller_poll_seconds)


def run_worker(settings: Settings) -> None:
    """Claim and execute the reconstruction, entity-extraction, and publication stages."""
    repository = SqlAlchemyBatchRepository(create_session_factory(settings))
    artifacts = create_artifact_store(settings)
    start_http_server(settings.metrics_port)
    if settings.qwen3_gpu0_slot_unit != settings.qwen_vl_gpu0_slot_unit:
        raise ValueError("Qwen-VL and Qwen3 must share the same GPU0 role unit")
    repository.configure_resource_pool(
        kind=ReservationKind.GPU0, capacity=1, unit=settings.qwen_vl_gpu0_slot_unit
    )
    repository.configure_resource_pool(
        kind=ReservationKind.GPU1, capacity=1, unit=settings.gpu1_slot_unit
    )
    worker_id = f"worker-{uuid4()}"
    qwen_client = LocalOpenAIQwenVLClient(
        endpoint=settings.qwen_vl_endpoint,
        model_id=settings.qwen_vl_model_id,
        timeout_seconds=settings.qwen_vl_timeout_seconds,
    )
    assembler = ReconstructionAssembler(
        client=qwen_client,
        artifacts=artifacts,
        model_id=settings.qwen_vl_model_id,
        model_revision=settings.qwen_vl_model_revision,
        prompt_version=settings.qwen_vl_prompt_version,
        max_blocks_per_request=settings.qwen_vl_max_blocks_per_request,
        max_images_per_request=settings.qwen_vl_max_images_per_request,
    )
    reconstruction_handler = QwenVLStageHandler(
        assembler, artifacts, repository, settings.qwen_vl_gpu0_slot_unit
    )
    entity_handler = EntityStageHandler(
        extractor=EntityExtractor(
            client=LocalOpenAIQwen3Client(
                endpoint=settings.qwen3_endpoint,
                model_id=settings.qwen3_model_id,
                timeout_seconds=settings.qwen3_timeout_seconds,
            )
        ),
        artifacts=artifacts,
        repository=repository,
        gpu0_slot_unit=settings.qwen3_gpu0_slot_unit,
    )
    publisher = FinalBundlePublisher(artifacts, repository)
    vision_handler = VisionStageHandler(
        VisionPreparation(
            renderer=PyMuPdfRenderer(),
            artifacts=artifacts,
            quality_gate=ImageQualityGate(
                settings.upscale_entropy_tolerance, settings.upscale_clipping_tolerance
            ),
            docx_converter=CommandDocxConverter(settings.docx_converter_command)
            if settings.docx_converter_command
            else None,
        ),
        repository,
        RenderLimits(
            settings.render_dpi,
            settings.render_max_pages,
            settings.render_max_pixels_per_page,
            settings.render_max_total_pixels,
        ),
    )
    layout_handler = (
        MinerUStageHandler(
            CommandMinerURunner(settings.mineru_command), LayoutAdapter(), artifacts, repository
        )
        if settings.mineru_command
        else None
    )
    ocr_handler = OcrStageHandler(artifacts, repository)
    if layout_handler is None:
        raise ValueError("active worker profile requires a pinned MinerU command")
    LOGGER.info("worker started; worker_id=%s", worker_id)
    while True:
        claim = repository.claim_next_job(
            worker_id=worker_id,
            lease_duration=timedelta(minutes=20),
            stages=("source_snapshot", "layout", "ocr", "reconstruction", "entity_extract", "publish"),
        )
        if claim is not None:
            _dispatch_worker_claim(
                claim=claim,
                worker_id=worker_id,
                repository=repository,
                artifacts=artifacts,
                vision_handler=vision_handler,
                layout_handler=layout_handler,
                ocr_handler=ocr_handler,
                reconstruction_handler=reconstruction_handler,
                entity_handler=entity_handler,
                publisher=publisher,
                qwen3_model_id=settings.qwen3_model_id,
                qwen3_model_revision=settings.qwen3_model_revision,
                qwen_vl_endpoint=settings.qwen_vl_endpoint,
                qwen3_endpoint=settings.qwen3_endpoint,
                gpu1_slot_unit=settings.gpu1_slot_unit,
            )
        time.sleep(settings.controller_poll_seconds)


def _dispatch_worker_claim(
    *,
    claim: JobClaim,
    worker_id: str,
    repository: SqlAlchemyBatchRepository,
    artifacts: LocalArtifactStore | MinioArtifactStore,
    vision_handler: VisionStageHandler,
    layout_handler: MinerUStageHandler | None,
    ocr_handler: OcrStageHandler | None,
    reconstruction_handler: QwenVLStageHandler,
    entity_handler: EntityStageHandler,
    publisher: FinalBundlePublisher,
    qwen3_model_id: str,
    qwen3_model_revision: str,
    qwen_vl_endpoint: str,
    qwen3_endpoint: str,
    gpu1_slot_unit: str,
) -> None:
    """Load only verified artifacts, dispatch one leased stage, and apply retry policy on failure."""
    started_at = time.monotonic()
    heartbeat = _LeaseHeartbeat(repository, claim.job_id, worker_id)
    heartbeat.start()
    try:
        if claim.stage in {"reconstruction", "entity_extract"}:
            _wait_for_model_endpoint(
                qwen_vl_endpoint if claim.stage == "reconstruction" else qwen3_endpoint
            )
        if claim.stage == "source_snapshot":
            vision_handler.handle(
                job_id=claim.job_id,
                worker_id=worker_id,
                source_object_key=_payload_string(claim.payload, "source_object_key"),
                source_sha256=_payload_string(claim.payload, "source_object_sha256"),
                artifact_prefix=_artifact_prefix(claim),
            )
            ControllerMetrics().observe_stage_duration(
                stage=claim.stage, outcome="succeeded", seconds=time.monotonic() - started_at
            )
            return
        if claim.stage == "layout":
            if layout_handler is None:
                raise RuntimeError("layout worker adapter is not configured in the active profile")
            _reserve_gpu1(repository, claim, worker_id, gpu1_slot_unit)
            render_reference = _reference_from_payload(claim.payload, "render_manifest")
            layout_handler.handle(
                job_id=claim.job_id,
                worker_id=worker_id,
                vision=vision_manifest_from_payload(_load_json_artifact(artifacts, render_reference)),
                artifact_prefix=_artifact_prefix(claim),
            )
            ControllerMetrics().observe_stage_duration(
                stage=claim.stage, outcome="succeeded", seconds=time.monotonic() - started_at
            )
            return
        if claim.stage == "ocr":
            if ocr_handler is None:
                raise RuntimeError("OCR worker adapter is not configured in the active profile")
            layout_reference = _reference_from_payload(claim.payload, "layout_manifest")
            ocr_handler.handle(
                job_id=claim.job_id,
                worker_id=worker_id,
                layout=layout_manifest_from_payload(_load_json_artifact(artifacts, layout_reference)),
                layout_reference=layout_reference,
                artifact_prefix=_artifact_prefix(claim),
            )
            ControllerMetrics().observe_stage_duration(
                stage=claim.stage, outcome="succeeded", seconds=time.monotonic() - started_at
            )
            return
        if claim.stage == "reconstruction":
            layout_reference = _reference_from_payload(claim.payload, "layout_manifest")
            ocr_reference = _reference_from_payload(claim.payload, "ocr_manifest")
            reconstruction_handler.handle(
                job_id=claim.job_id,
                worker_id=worker_id,
                layout=layout_manifest_from_payload(_load_json_artifact(artifacts, layout_reference)),
                layout_reference=layout_reference,
                ocr=ocr_manifest_from_payload(_load_json_artifact(artifacts, ocr_reference)),
                ocr_reference=ocr_reference,
                artifact_prefix=_artifact_prefix(claim),
            )
            ControllerMetrics().observe_stage_duration(
                stage=claim.stage, outcome="succeeded", seconds=time.monotonic() - started_at
            )
            return
        if claim.stage == "entity_extract":
            reconstruction_reference = _reference_from_payload(claim.payload, "reconstruction_manifest")
            reconstruction = reconstruction_manifest_from_payload(
                _load_json_artifact(artifacts, reconstruction_reference)
            )
            entity_handler.handle(
                job_id=claim.job_id,
                worker_id=worker_id,
                reconstruction=reconstruction,
                reconstruction_reference=reconstruction_reference,
                artifact_prefix=_artifact_prefix(claim),
            )
            ControllerMetrics().observe_stage_duration(
                stage=claim.stage, outcome="succeeded", seconds=time.monotonic() - started_at
            )
            return
        if claim.stage == "publish":
            reconstruction_reference = _reference_from_payload(claim.payload, "reconstruction_manifest")
            entity_reference = _reference_from_payload(claim.payload, "entity_manifest")
            reconstruction = reconstruction_manifest_from_payload(
                _load_json_artifact(artifacts, reconstruction_reference)
            )
            entities = entity_manifest_from_payload(_load_json_artifact(artifacts, entity_reference))
            quality = QualityState.WARNING if reconstruction.findings or entities.findings else QualityState.PASS
            publisher.publish(
                item_id=claim.batch_item_id,
                bundle_prefix=f"results/{claim.batch_item_id}/{reconstruction.source_sha256}",
                source_sha256=reconstruction.source_sha256,
                pipeline_profile_hash=repository.get_item_profile_hash(item_id=claim.batch_item_id),
                quality=quality,
                markdown=reconstruction.markdown,
                entities=entities.entities,
                schema_version=entities.schema_version,
                reconstruction=reconstruction_reference,
                model_versions={
                    "qwen_vl": f"{reconstruction.model_id}@{reconstruction.model_revision}",
                    "qwen3": f"{qwen3_model_id}@{qwen3_model_revision}",
                },
                findings=tuple(
                    {
                        "source": "reconstruction",
                        "code": finding.code,
                        "severity": finding.severity,
                        "detail": finding.detail,
                        "block_ids": finding.block_ids,
                        "evidence": finding.evidence,
                        "locations": finding.locations,
                    }
                    for finding in reconstruction.findings
                )
                + tuple(
                    {
                        "source": "entities",
                        "code": finding.code,
                        "detail": finding.detail,
                        "candidate_index": finding.candidate_index,
                    }
                    for finding in entities.findings
                ),
                created_at=claim.created_at,
                job_id=claim.job_id,
                worker_id=worker_id,
            )
            _cleanup_temporary_artifacts(repository, artifacts, claim.batch_item_id)
            ControllerMetrics().observe_stage_duration(
                stage=claim.stage, outcome="succeeded", seconds=time.monotonic() - started_at
            )
            return
        raise RuntimeError(f"worker cannot dispatch unexpected stage: {claim.stage}")
    except ModelEndpointNotReady as error:
        repository.defer_job_for_capacity(
            job_id=claim.job_id,
            worker_id=worker_id,
            error_detail=str(error),
        )
        LOGGER.info("job deferred while local model starts; job_id=%s", claim.job_id)
        ControllerMetrics().mark_capacity_paused()
    except ResourceCapacityError:
        LOGGER.info("job deferred for capacity; job_id=%s", claim.job_id)
        ControllerMetrics().mark_capacity_paused()
    except (
        EntityExtractionError,
        MinerUError,
        OcrError,
        ReconstructionError,
        VisionPreparationError,
        OSError,
        ValueError,
        RuntimeError,
    ) as error:
        LOGGER.exception("worker stage failed; job_id=%s stage=%s", claim.job_id, claim.stage)
        final_state = repository.fail_job(
            job_id=claim.job_id,
            worker_id=worker_id,
            error_code=type(error).__name__,
            error_detail=str(error),
            retryable=True,
        )
        metrics = ControllerMetrics()
        metrics.observe_stage_duration(
            stage=claim.stage, outcome=final_state.value, seconds=time.monotonic() - started_at
        )
        if final_state == JobState.PENDING:
            metrics.record_retry(stage=claim.stage, reason=type(error).__name__)
        else:
            metrics.record_quarantine(stage=claim.stage, reason=type(error).__name__)
    finally:
        heartbeat.stop()


def _reference_from_payload(payload: dict[str, object], field: str) -> ArtifactReference:
    """Validate an immutable artifact reference supplied by the durable job payload."""
    value = payload.get(field)
    if not isinstance(value, dict):
        raise ValueError(f"job payload lacks {field}")
    return ArtifactReference.model_validate(value)


def _payload_string(payload: dict[str, object], field: str) -> str:
    """Read one required string from durable job JSON without implicit conversion."""
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"job payload lacks non-empty {field}")
    return value


def _artifact_prefix(claim: JobClaim) -> str:
    """Make intermediate keys unique across a quarantined-item retry graph."""
    return f"items/{claim.batch_item_id}/jobs/{claim.job_id}/attempt-{claim.attempt}"


class ModelEndpointNotReady(RuntimeError):
    """A local model process is still starting and must not consume stage retries."""


class _LeaseHeartbeat:
    """Keep a long local stage and its reservations owned until its handler returns."""

    def __init__(self, repository: SqlAlchemyBatchRepository, job_id: UUID, worker_id: str) -> None:
        self._repository = repository
        self._job_id = job_id
        self._worker_id = worker_id
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"idp-lease-{job_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)

    def _run(self) -> None:
        while not self._stop.wait(60):
            try:
                self._repository.renew_lease(
                    job_id=self._job_id,
                    worker_id=self._worker_id,
                    lease_duration=timedelta(minutes=20),
                )
            except LeaseOwnershipError:
                return
            except Exception:
                LOGGER.exception("worker lease heartbeat failed; job_id=%s", self._job_id)


def _wait_for_model_endpoint(endpoint: str) -> None:
    """Treat a still-starting local model server as capacity deferral, not a consumed retry."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{endpoint}/models", timeout=5) as response:
            if not response.read():
                raise RuntimeError("local model endpoint returned an empty model list")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ModelEndpointNotReady(f"local model endpoint is not ready: {error}") from error


def _reserve_gpu1(repository: SqlAlchemyBatchRepository, claim: JobClaim, worker_id: str, unit: str) -> None:
    """Reserve the shared GPU1 model-worker slot before MinerU layout begins."""
    from idp.domain.models import ResourceRequest

    try:
        repository.reserve_resources(
            job_id=claim.job_id,
            owner=worker_id,
            requests=(ResourceRequest(kind=ReservationKind.GPU1, amount=1, unit=unit),),
            lease_duration=timedelta(minutes=20),
        )
    except ResourceCapacityError:
        repository.defer_job_for_capacity(
            job_id=claim.job_id,
            worker_id=worker_id,
            error_detail="GPU1 MinerU role slot is unavailable",
        )
        raise


def _load_json_artifact(
    artifacts: LocalArtifactStore | MinioArtifactStore, reference: ArtifactReference
) -> dict[str, object]:
    """Materialize and decode one hash-verified JSON manifest from object storage."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="idp-worker-") as temporary:
        target = Path(temporary) / "manifest.json"
        artifacts.get_file(reference, target)
        payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"artifact is not a JSON object: {reference.object_key}")
    return payload


def _collect_repository_metrics(repository: SqlAlchemyBatchRepository, metrics: ControllerMetrics) -> None:
    """Expose queue, lease, capacity, and quality state from PostgreSQL each poll."""
    snapshot = repository.get_observability_snapshot()
    metrics.reconcile_durable_state(
        queue_depth=snapshot["queue_depth"],
        active_leases=snapshot["active_leases"],
        reservations=snapshot["reservations"],
        quality=snapshot["quality"],
    )


def _cleanup_temporary_artifacts(
    repository: SqlAlchemyBatchRepository,
    artifacts: LocalArtifactStore | MinioArtifactStore,
    item_id: UUID,
) -> None:
    """Best-effort retention cleanup cannot retract an already durable final bundle."""
    for artifact in repository.temporary_artifacts_for_item(item_id=item_id):
        try:
            artifacts.delete(artifact)
        except Exception:
            LOGGER.warning("temporary artifact cleanup failed; key=%s", artifact.reference.object_key)
