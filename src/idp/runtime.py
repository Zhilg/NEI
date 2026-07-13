"""Process factories for the durable controller foundation."""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from prometheus_client import start_http_server
from minio import Minio
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from idp.config import Settings
from idp.metrics import ControllerMetrics
from idp.persistence.repository import SqlAlchemyBatchRepository
from idp.services.controller import Controller
from idp.services.batches import BatchService
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
        artifacts = LocalArtifactStore(settings.release_root / "local-artifacts")
    return BatchService(settings, repository, artifacts, settings.batch_staging_root)


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
        time.sleep(settings.controller_poll_seconds)


def run_idle_worker(settings: Settings) -> None:
    """Run a deployable worker process until stage handlers arrive in later phases."""
    LOGGER.info("worker started without model stage handlers")
    while True:
        time.sleep(settings.controller_poll_seconds)
